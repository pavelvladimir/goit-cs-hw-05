import json
import subprocess
import sys
from pathlib import Path
from time import perf_counter
from typing import Any
from unittest.mock import patch

import pytest
from jsonschema import Draft202012Validator

import async_sorter
import mapreduce_words
import report_validation
from common import finish_report
from report_validation import SCHEMA_PATH, ReportValidationError, validate_report
from scripts.validate_report import main as validate_main

ROOT = Path(__file__).resolve().parents[1]


def test_schema_is_valid_and_uses_only_internal_references() -> None:
    """Validate the bundled schema without any external references."""
    schema = json.loads(SCHEMA_PATH.read_text())
    Draft202012Validator.check_schema(schema)
    pending = [schema]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            if "$ref" in value:
                assert value["$ref"].startswith("#/")
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)


@pytest.mark.parametrize("task", ["async_sorter", "mapreduce_words"])
@pytest.mark.parametrize(
    ("status", "code"), [("success", 0), ("failed", 1), ("cancelled", 130)]
)
def test_all_outcomes_are_valid(
    task: str,
    status: str,
    code: int,
    valid_sorter_report: dict[str, Any],
    valid_word_report: dict[str, Any],
) -> None:
    """Accept valid success, failure, and cancellation reports for both tasks."""
    report = valid_sorter_report if task == "async_sorter" else valid_word_report
    report.update(status=status, exit_code=code)
    report["statistics"]["errors"] = int(status == "failed")
    if task == "mapreduce_words" and status == "failed":
        report["error"] = "render failed after analysis"
    if task == "async_sorter" and status == "cancelled":
        report["statistics"]["errors"] = 1  # I/O can fail during cancellation.
    validate_report(report)


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_download_metadata_can_be_absent_before_download(
    valid_word_report: dict[str, Any],
    status: str,
) -> None:
    """Allow failure or cancellation before download metadata is available."""
    report = valid_word_report
    report.update(
        status=status,
        exit_code=1 if status == "failed" else 130,
        source={"requested_url": ""},
        frequencies={},
        top_words=[],
        statistics={
            "total_words": 0,
            "unique_words": 0,
            "errors": int(status == "failed"),
        },
    )
    if status == "failed":
        report["error"] = "invalid URL"
    validate_report(report)


@pytest.mark.parametrize(
    "field",
    ["schema_version", "task", "finished_at", "source", "parameters", "statistics"],
)
def test_missing_required_field_is_rejected(
    valid_sorter_report: dict[str, Any], field: str
) -> None:
    """Reject sorter reports missing a required contract field."""
    del valid_sorter_report[field]
    with pytest.raises(ReportValidationError):
        validate_report(valid_sorter_report)


@pytest.mark.parametrize(
    ("keys", "value"),
    [
        (("schema_version",), 2),
        (("schema_version",), True),
        (("task",), "unknown"),
        (("status",), "unknown"),
        (("exit_code",), 130),
        (("statistics", "errors"), 1),
        (("statistics", "copied"), -1),
        (("statistics", "copied"), True),
        (("parameters", "workers"), "4"),
        (("parameters", "workers"), 0),
        (("parameters", "extra"), 1),
        (("statistics", "extra"), 1),
        (("extra",), "unknown"),
        (("frequencies",), {"wrong_task": 1}),
        (("source",), "relative/source"),
        (("elapsed_seconds",), -0.1),
        (("elapsed_seconds",), float("nan")),
        (("elapsed_seconds",), float("inf")),
        (("finished_at",), "2026-02-30T00:00:00Z"),
        (("finished_at",), "2026-10-27T00:00:00"),
        (("finished_at",), "2026-10-27T00:00:00+02:00"),
        (("finished_at",), 123),
        (("error",), "success cannot have a fatal error"),
    ],
)
def test_invalid_common_and_sorter_fields_are_rejected(
    valid_sorter_report: dict[str, Any],
    keys: tuple[str, ...],
    value: object,
) -> None:
    """Reject invalid types, ranges, dates, and unexpected sorter fields."""
    target = valid_sorter_report
    for key in keys[:-1]:
        target = target[key]
    target[keys[-1]] = value
    with pytest.raises(ReportValidationError):
        validate_report(valid_sorter_report)


@pytest.mark.parametrize(
    ("keys", "value"),
    [
        (("source", "sha256_bytes"), "abc"),
        (("source", "sha256_bytes"), "a" * 64 + "\n"),
        (("source", "sha256_text_utf8"), "z" * 64),
        (("source", "bytes_downloaded"), -1),
        (("source", "final_url"), ""),
        (("source", "extra"), 1),
        (("parameters", "timeout"), 0),
        (("parameters", "chunk_size"), 0),
        (("parameters", "top"), 0),
        (("parameters", "max_bytes"), 0),
        (("frequencies", "hello"), 0),
        (("frequencies", ""), 1),
        (("frequencies", "hello"), "2"),
        (("top_words",), [{"word": "hello", "count": -1}]),
        (("top_words",), [{"word": "hello", "count": 2, "extra": True}]),
        (("top_words",), [{"word": "hello"}]),
        (("source",), {"requested_url": "http://localhost/text"}),
        (("frequencies",), {}),
        (("top_words",), []),
    ],
)
def test_invalid_word_fields_are_rejected(
    valid_word_report: dict[str, Any],
    keys: tuple[str, ...],
    value: object,
) -> None:
    """Reject malformed download metadata, counts, and word parameters."""
    target = valid_word_report
    for key in keys[:-1]:
        target = target[key]
    target[keys[-1]] = value
    with pytest.raises(ReportValidationError):
        validate_report(valid_word_report)


def test_partial_download_metadata_is_rejected_on_failure(
    valid_word_report: dict[str, Any],
) -> None:
    """Require complete download metadata whenever any of it is supplied."""
    report = valid_word_report
    report.update(status="failed", exit_code=1, error="download failed")
    report["statistics"]["errors"] = 1
    del report["source"]["sha256_bytes"]
    with pytest.raises(ReportValidationError):
        validate_report(report)


@pytest.mark.parametrize(
    ("status", "code", "errors"),
    [("failed", 0, 1), ("failed", 1, 0), ("cancelled", 1, 0)],
)
def test_failed_and_cancelled_codes_match_status(
    valid_sorter_report: dict[str, Any],
    status: str,
    code: int,
    errors: int,
) -> None:
    """Reject inconsistent status, exit code, and error counts."""
    valid_sorter_report.update(status=status, exit_code=code)
    valid_sorter_report["statistics"]["errors"] = errors
    with pytest.raises(ReportValidationError):
        validate_report(valid_sorter_report)


@pytest.mark.parametrize("code", [0, 1, 130])
def test_invalid_new_report_never_overwrites_previous_file(
    tmp_path: Path,
    valid_sorter_report: dict[str, Any],
    code: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Validate before writing so malformed reports preserve the old file."""
    output = tmp_path / "report.json"
    original = json.dumps(valid_sorter_report).encode()
    output.write_bytes(original)
    valid_sorter_report["statistics"]["copied"] = -1
    with patch("common.write_json") as write:
        assert finish_report(output, valid_sorter_report, perf_counter(), code) == (
            130 if code == 130 else 1
        )
        write.assert_not_called()
    assert output.read_bytes() == original
    assert not list(tmp_path.glob(".hw05-*.part"))
    assert "statistics.copied" in caplog.text


def test_non_json_objects_are_rejected(valid_sorter_report: dict[str, Any]) -> None:
    """Reject Python values that cannot be represented in JSON."""
    valid_sorter_report["source"] = object()
    with pytest.raises(ReportValidationError, match="JSON"):
        validate_report(valid_sorter_report)


def test_invalid_bundled_schema_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Surface schema errors and clear the validator cache afterward."""
    broken = tmp_path / "schema.json"
    broken.write_text('{"type": "not-a-type"}')
    monkeypatch.setattr(report_validation, "SCHEMA_PATH", broken)
    report_validation._validator.cache_clear()
    try:
        with pytest.raises(ReportValidationError, match="схема"):
            validate_report({})
    finally:
        report_validation._validator.cache_clear()


def test_partial_sort_failure_is_schema_valid(tmp_path: Path) -> None:
    """Accept partial copy counts with per-file errors recorded in the log."""
    source, output, report = (
        tmp_path / "src",
        tmp_path / "out",
        tmp_path / "report.json",
    )
    source.mkdir()
    output.mkdir()
    (source / "good.bin").write_bytes(b"data")
    (source / "bad.txt").write_text("blocked")
    (output / "txt").touch()
    with patch.object(async_sorter, "configure_logging"):
        assert (
            async_sorter.main([str(source), str(output), "--report", str(report)]) == 1
        )
    data = json.loads(report.read_text())
    validate_report(data)
    assert data["status"] == "failed"
    assert data["statistics"]["copied"] == data["statistics"]["errors"] == 1
    assert "error" not in data  # Per-file errors live in the log.


@pytest.mark.parametrize("interrupt", [False, True])
def test_word_report_valid_after_analysis_but_before_chart(
    tmp_path: Path,
    http_url: str,
    interrupt: bool,
) -> None:
    """Retain valid counts and hashes when charting fails or is interrupted."""
    output, report = tmp_path / "plot.png", tmp_path / "run.json"
    error = KeyboardInterrupt() if interrupt else OSError("disk full")
    with (
        patch.object(mapreduce_words, "configure_logging"),
        patch.object(mapreduce_words, "visualize_top_words", side_effect=error),
    ):
        assert mapreduce_words.main(
            [f"{http_url}/text", "--output", str(output), "--report", str(report)]
        ) == (130 if interrupt else 1)
    data = json.loads(report.read_text())
    validate_report(data)
    assert data["statistics"]["total_words"] > 0
    assert "sha256_bytes" in data["source"]
    assert data["status"] == ("cancelled" if interrupt else "failed")


def test_validator_cli_success_and_errors(
    tmp_path: Path,
    valid_sorter_report: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Validate every input file, report each failure, and leave files intact."""
    valid = tmp_path / "valid.json"
    valid.write_text(json.dumps(valid_sorter_report))
    original = valid.read_bytes()
    assert validate_main([str(valid)]) == 0
    assert "OK:" in capsys.readouterr().out
    malformed = tmp_path / "malformed.json"
    malformed.write_text('{"unfinished":')
    incompatible = tmp_path / "incompatible.json"
    incompatible.write_text('{"schema_version": 2}')
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.validate_report",
            str(malformed),
            str(valid),
            str(incompatible),
            str(tmp_path / "missing.json"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 1
    assert f"OK: {valid}" in result.stdout
    assert result.stderr.count("ERROR:") == 3
    assert valid.read_bytes() == original
