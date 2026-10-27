import asyncio
import hashlib
import json
from pathlib import Path
from time import perf_counter
from typing import Any
from unittest.mock import patch

import pytest
from anyio import Path as AsyncPath
from matplotlib.figure import Figure

import async_sorter
import mapreduce_words
from common import (
    atomic_output,
    finish_report,
    validate_output_paths,
    write_json,
)
from report_validation import validate_report


def test_atomic_output_retains_previous_file_on_failure(tmp_path: Path) -> None:
    output = tmp_path / "output.txt"
    output.write_text("old")
    with pytest.raises(OSError, match="failed"):
        with atomic_output(output) as temporary:
            temporary.write_text("partial")
            assert output.read_text() == "old"
            raise OSError("failed")
    assert output.read_text() == "old"
    assert list(tmp_path.iterdir()) == [output]
    with atomic_output(output) as temporary:
        temporary.write_text("complete")
    assert output.read_text() == "complete"


def test_json_serialization_failure_preserves_existing_report(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    write_json(report, {"old": True})
    with pytest.raises(TypeError):
        write_json(report, {"not_json": object()})
    assert json.loads(report.read_text()) == {"old": True}
    assert not list(tmp_path.glob(".hw05-*"))


def test_failed_chart_preserves_previous_png(tmp_path: Path) -> None:
    output = tmp_path / "plot.png"
    output.write_bytes(b"previous PNG")

    def broken_save(self: Figure, path: Path, **kwargs: object) -> None:
        path.write_bytes(b"partial PNG")
        raise OSError("disk full")

    with patch.object(Figure, "savefig", broken_save), pytest.raises(OSError):
        mapreduce_words.visualize_top_words({"word": 3}, output=output)
    assert output.read_bytes() == b"previous PNG"
    assert not list(tmp_path.glob(".hw05-*"))


def test_failed_publication_never_exposes_partial_copy(tmp_path: Path) -> None:
    source = tmp_path / "file.txt"
    source.write_text("complete data")
    output = tmp_path / "out"
    with patch.object(AsyncPath, "hardlink_to", side_effect=OSError("unsupported")):
        with pytest.raises(OSError, match="unsupported"):
            asyncio.run(async_sorter.copy_file(AsyncPath(source), AsyncPath(output)))
    assert list((output / "txt").iterdir()) == []


def test_sorter_success_failure_and_collision_reports(tmp_path: Path) -> None:
    source, output, report = tmp_path / "src", tmp_path / "out", tmp_path / "run.json"
    source.mkdir()
    (source / "file.txt").write_bytes(b"12345")
    (output / "txt").mkdir(parents=True)
    (output / "txt/file.txt").write_bytes(b"existing")
    with patch.object(async_sorter, "configure_logging"):
        assert (
            async_sorter.main([str(source), str(output), "--report", str(report)]) == 0
        )
        data = json.loads(report.read_text())
        validate_report(data)
        assert data["schema_version"] == 1
        assert data["status"] == "success"
        assert data["elapsed_seconds"] > 0
        assert data["statistics"] == {
            "copied": 1,
            "skipped": 0,
            "errors": 0,
            "bytes_copied": 5,
            "collisions": 1,
        }
        assert (
            async_sorter.main(
                [str(source / "missing"), str(output), "--report", str(report)]
            )
            == 1
        )
        assert json.loads(report.read_text())["status"] == "failed"
        validate_report(json.loads(report.read_text()))


def test_word_report_has_exact_input_hashes_and_frequencies(
    http_url: str, tmp_path: Path
) -> None:
    report, chart = tmp_path / "report.json", tmp_path / "chart.png"
    with patch.object(mapreduce_words, "configure_logging"):
        assert (
            mapreduce_words.main(
                [f"{http_url}/latin", "--output", str(chart), "--report", str(report)]
            )
            == 0
        )
        data = json.loads(report.read_text())
        validate_report(data)
        assert data["source"]["bytes_downloaded"] == len("café café".encode("latin-1"))
        assert (
            data["source"]["sha256_bytes"]
            == hashlib.sha256("café café".encode("latin-1")).hexdigest()
        )
        assert (
            data["source"]["sha256_text_utf8"]
            == hashlib.sha256("café café".encode()).hexdigest()
        )
        assert data["frequencies"] == {"café": 2}
        assert data["top_words"] == [{"word": "café", "count": 2}]
        assert data["source"]["final_url"] == f"{http_url}/latin"
        assert (
            mapreduce_words.main(
                [f"{http_url}/missing", "--output", str(chart), "--report", str(report)]
            )
            == 1
        )
        assert json.loads(report.read_text())["status"] == "failed"
        validate_report(json.loads(report.read_text()))
        with patch.object(
            mapreduce_words, "download_text", side_effect=KeyboardInterrupt
        ):
            assert mapreduce_words.main(["--report", str(report)]) == 130
        assert json.loads(report.read_text())["status"] == "cancelled"
        validate_report(json.loads(report.read_text()))


def test_unsafe_report_paths_rejected_before_touching_data(tmp_path: Path) -> None:
    source, output = tmp_path / "src", tmp_path / "out"
    source.mkdir()
    original = source / "original.json"
    original.write_text("original")
    for candidate in (original, output / "run.json"):
        assert (
            async_sorter.main([str(source), str(output), "--report", str(candidate)])
            == 1
        )
    assert original.read_text() == "original"
    assert (
        mapreduce_words.main(["--output", str(original), "--report", str(original)])
        == 1
    )
    assert original.read_text() == "original"
    alias = tmp_path / "alias.json"
    alias.hardlink_to(original)
    with pytest.raises(ValueError):
        validate_output_paths(alias, original)
    with pytest.raises(ValueError):
        validate_output_paths(original, original / "nested")


@pytest.mark.parametrize("code", [0, 130])
def test_report_write_failure_is_visible(
    tmp_path: Path, code: int, valid_sorter_report: dict[str, Any]
) -> None:
    with patch("common.write_json", side_effect=OSError("disk full")) as write:
        result = finish_report(
            tmp_path / "report.json", valid_sorter_report, perf_counter(), code
        )
        write.assert_called_once()
    assert result == (130 if code == 130 else 1)


def test_source_symlink_loop_is_logged_and_reported(
    tmp_path: Path,
) -> None:
    first, second, report = (
        tmp_path / "first",
        tmp_path / "second",
        tmp_path / "report.json",
    )
    first.symlink_to(second)
    second.symlink_to(first)
    assert (
        async_sorter.main([str(first), str(tmp_path / "out"), "--report", str(report)])
        == 1
    )
    data = json.loads(report.read_text())
    assert data["status"] == "failed"
    assert data["statistics"]["errors"] == 1
    assert first.is_symlink() and second.is_symlink()


def test_hardlinked_log_cannot_modify_source(tmp_path: Path) -> None:
    source, output = tmp_path / "source", tmp_path / "out"
    source.mkdir()
    original = source / "original.txt"
    original.write_bytes(b"original")
    (source / "link").symlink_to(original)
    log = tmp_path / "run.log"
    log.hardlink_to(original)
    report = tmp_path / "run.json"
    assert (
        async_sorter.main(
            [str(source), str(output), "--log-file", str(log), "--report", str(report)]
        )
        == 1
    )
    assert original.read_bytes() == b"original"
    assert not output.exists()
    assert json.loads(report.read_text())["status"] == "failed"


def test_case_only_output_aliases_are_rejected_before_creation(tmp_path: Path) -> None:
    image = tmp_path / "Chart.png"
    report = tmp_path / "chart.PNG"
    assert mapreduce_words.main(["--output", str(image), "--report", str(report)]) == 1
    assert not image.exists() and not report.exists()
    source = tmp_path / "source"
    source.mkdir()
    assert (
        async_sorter.main(
            [
                str(source),
                str(tmp_path / "out"),
                "--report",
                str(tmp_path / "SOURCE" / "run.json"),
            ]
        )
        == 1
    )
    assert list(source.iterdir()) == []
