"""Real POSIX permission failures through both public command-line interfaces."""

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from report_validation import validate_report

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def require_unprivileged_posix() -> None:
    """Require real permission enforcement; fail rather than skip in Rocky."""
    if os.name != "posix" or os.geteuid() == 0:
        if os.environ.get("HW05_ROCKY_CHECK") == "1":
            pytest.fail("Rocky must exercise permissions as a non-root user.")
        pytest.skip("Real mode-bit permission tests require a non-root POSIX user.")


@contextmanager
def permissions(path: Path, mode: int) -> Iterator[None]:
    """Temporarily change permission bits and always restore them."""
    previous = path.stat().st_mode & 0o7777
    path.chmod(mode)
    try:
        yield
    finally:
        path.chmod(previous)


def cli(script: str, *arguments: str | Path) -> subprocess.CompletedProcess[str]:
    """Run a real CLI with captured output and a bounded timeout."""
    return subprocess.run(
        [sys.executable, str(ROOT / script), *map(str, arguments)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )


def assert_failed(result: subprocess.CompletedProcess[str], report: Path) -> None:
    """Check that a permission failure reaches stderr and a valid report."""
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "PermissionError" in result.stderr
    data = json.loads(report.read_text())
    validate_report(data)
    assert data["status"] == "failed"
    assert data["exit_code"] == 1
    assert data["statistics"]["errors"] >= 1


@pytest.mark.parametrize("blocked_kind", ["file", "directory"])
def test_unreadable_source_keeps_data_and_copies_accessible_files(
    tmp_path: Path,
    blocked_kind: str,
) -> None:
    """Continue past denied inputs without modifying the source files."""
    source, output = tmp_path / "source", tmp_path / "out"
    source.mkdir()
    good = source / "good.txt"
    good.write_bytes(b"readable")
    nested = source / "nested"
    nested.mkdir()
    hidden = nested / "hidden.bin"
    hidden.write_bytes(b"private data")
    report, log = tmp_path / "report.json", tmp_path / "errors.log"
    with permissions(hidden if blocked_kind == "file" else nested, 0):
        result = cli(
            "async_sorter.py", source, output, "--report", report, "--log-file", log
        )
    assert_failed(result, report)
    assert "PermissionError" in log.read_text()
    assert hidden.read_bytes() == b"private data"
    assert good.read_bytes() == b"readable"
    assert (output / "txt/good.txt").read_bytes() == b"readable"
    assert not (output / "bin/hidden.bin").exists()
    assert not list(tmp_path.rglob(".hw05-*.part"))
    stats = json.loads(report.read_text())["statistics"]
    assert stats["copied"] == 1 and stats["errors"] == 1


@pytest.mark.parametrize("blocked_kind", ["output", "extension"])
def test_unwritable_destination_preserves_existing_files(
    tmp_path: Path,
    blocked_kind: str,
) -> None:
    """Preserve old copies and report denied writes per extension folder."""
    source, output = tmp_path / "source", tmp_path / "out"
    source.mkdir()
    (source / "same.txt").write_bytes(b"new")
    (source / "good.json").write_bytes(b"{}")
    folder = output / "txt"
    folder.mkdir(parents=True)
    existing = folder / "same.txt"
    existing.write_bytes(b"previous")
    report = tmp_path / "report.json"
    # A read-only parent alone does not restrict writes inside existing children.
    with (
        permissions(folder, 0o555),
        permissions(output, 0o555 if blocked_kind == "output" else 0o755),
    ):
        result = cli("async_sorter.py", source, output, "--report", report)
    assert_failed(result, report)
    assert existing.read_bytes() == b"previous"
    assert (source / "same.txt").read_bytes() == b"new"
    assert (source / "good.json").read_bytes() == b"{}"
    assert not (folder / "same_1.txt").exists()
    assert not list(tmp_path.rglob(".hw05-*.part"))
    stats = json.loads(report.read_text())["statistics"]
    expected_copies = int(blocked_kind == "extension")
    assert stats["copied"] == expected_copies
    assert stats["errors"] == 2 - expected_copies
    if expected_copies:
        assert (output / "json/good.json").read_bytes() == b"{}"


def command_arguments(script: str, tmp_path: Path, http_url: str) -> list[str]:
    """Prepare valid input and output arguments for either homework CLI."""
    if script == "async_sorter.py":
        source = tmp_path / "source"
        source.mkdir()
        (source / "input.txt").write_bytes(b"original")
        return [str(source), str(tmp_path / "out")]
    return [f"{http_url}/text", "--output", str(tmp_path / "chart.png")]


@pytest.mark.parametrize("script", ["async_sorter.py", "mapreduce_words.py"])
def test_unwritable_report_directory_preserves_previous_report(
    tmp_path: Path,
    http_url: str,
    script: str,
) -> None:
    """Keep the previous report when new results cannot be published."""
    folder = tmp_path / "reports"
    folder.mkdir()
    report = folder / "run.json"
    arguments = command_arguments(script, tmp_path, http_url)
    # Obtain a real, valid previous report, then deny directory writes.
    first = cli(script, *arguments, "--report", report)
    assert first.returncode == 0, first.stderr
    previous = report.read_bytes()
    validate_report(json.loads(previous))
    with permissions(folder, 0o555):
        result = cli(script, *arguments, "--report", report)
    assert result.returncode == 1
    assert "PermissionError" in result.stderr
    assert "Не вдалося зберегти звіт" in result.stderr
    assert report.read_bytes() == previous
    assert not list(tmp_path.rglob(".hw05-*.part"))
    if script == "async_sorter.py":
        assert (tmp_path / "source/input.txt").read_bytes() == b"original"
        assert (tmp_path / "out/txt/input_1.txt").read_bytes() == b"original"
    else:
        assert (tmp_path / "chart.png").read_bytes().startswith(b"\x89PNG")


def test_unwritable_chart_keeps_previous_image_and_reports_analysis(
    tmp_path: Path,
    http_url: str,
) -> None:
    """Retain the old chart and completed counts when chart writing fails."""
    folder = tmp_path / "charts"
    folder.mkdir()
    chart, report = folder / "chart.png", tmp_path / "run.json"
    arguments = [f"{http_url}/text", "--output", str(chart), "--report", str(report)]
    first = cli("mapreduce_words.py", *arguments)
    assert first.returncode == 0, first.stderr
    previous = chart.read_bytes()
    with permissions(folder, 0o555):
        result = cli("mapreduce_words.py", *arguments)
    assert_failed(result, report)
    assert chart.read_bytes() == previous
    assert not list(tmp_path.rglob(".hw05-*.part"))
    data = json.loads(report.read_text())
    assert data["frequencies"] == {"python": 2, "дані": 2, "слова": 1}
    assert "sha256_bytes" in data["source"]


@pytest.mark.parametrize("script", ["async_sorter.py", "mapreduce_words.py"])
@pytest.mark.parametrize("blocked_kind", ["file", "directory"])
def test_unwritable_log_stops_before_processing(
    tmp_path: Path,
    http_url: str,
    script: str,
    blocked_kind: str,
) -> None:
    """Report denied log access before copying or downloading anything."""
    folder = tmp_path / "logs"
    folder.mkdir()
    log = folder / "run.log"
    if blocked_kind == "file":
        log.write_bytes(b"earlier log\n")
    report = tmp_path / "run.json"
    arguments = command_arguments(script, tmp_path, http_url)
    target = log if blocked_kind == "file" else folder
    with permissions(target, 0o444 if blocked_kind == "file" else 0o555):
        result = cli(script, *arguments, "--log-file", log, "--report", report)
    assert_failed(result, report)
    if blocked_kind == "file":
        assert log.read_bytes() == b"earlier log\n"
    else:
        assert not log.exists()
    assert not list(tmp_path.rglob(".hw05-*.part"))
    assert not (tmp_path / "out").exists()
    assert not (tmp_path / "chart.png").exists()
    if script == "async_sorter.py":
        assert (tmp_path / "source/input.txt").read_bytes() == b"original"
    else:
        assert json.loads(report.read_text())["source"] == {
            "requested_url": f"{http_url}/text"
        }


@pytest.mark.parametrize("script", ["async_sorter.py", "mapreduce_words.py"])
def test_report_publication_error_cleans_real_temporary_file(
    tmp_path: Path,
    http_url: str,
    script: str,
) -> None:
    """Remove a report temporary file when a directory blocks publication."""
    report = tmp_path / "report.json"
    report.mkdir()
    sentinel = report / "keep.txt"
    sentinel.write_bytes(b"keep")
    result = cli(
        script, *command_arguments(script, tmp_path, http_url), "--report", report
    )
    assert result.returncode == 1
    assert "IsADirectoryError" in result.stderr
    assert "Не вдалося зберегти звіт" in result.stderr
    assert sentinel.read_bytes() == b"keep"
    assert not list(tmp_path.rglob(".hw05-*.part"))
