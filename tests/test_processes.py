import hashlib
import json
import os
import selectors
import signal
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from report_validation import validate_report

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="POSIX signals and pipe selectors"
)


@contextmanager
def probe(
    mode: str, source: Path, output: Path, report: Path
) -> Iterator[subprocess.Popen[str]]:
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tests.process_probe",
            mode,
            str(source),
            str(output),
            "--workers",
            "1",
            "--report",
            str(report),
        ],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


def expect_line(process: subprocess.Popen[str], expected: str) -> None:
    assert process.stdout is not None
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        assert selector.select(timeout=15), f"No {expected} handshake from child"
        assert process.stdout.readline().strip() == expected


def release(process: subprocess.Popen[str]) -> None:
    assert process.stdin is not None
    process.stdin.write("continue\n")
    process.stdin.flush()


@pytest.mark.parametrize("fail_write", [False, True])
def test_real_sigint_waits_for_io_cleans_partial_and_reports(
    tmp_path: Path, fail_write: bool
) -> None:
    source, output, report = (
        tmp_path / "source",
        tmp_path / "output",
        tmp_path / "run.json",
    )
    source.mkdir()
    payload = b"original-content" * 150_000
    original = source / "large.bin"
    original.write_bytes(payload)
    before = hashlib.sha256(payload).hexdigest()
    mode = "interrupt-error" if fail_write else "interrupt"
    with probe(mode, source, output, report) as process:
        expect_line(process, "READY")
        assert list(output.rglob(".hw05-*.part"))
        assert not (output / "bin/large.bin").exists()
        process.send_signal(signal.SIGINT)
        expect_line(process, "STOP")
        release(process)
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 130, (stdout, stderr)
    assert not list(output.rglob(".hw05-*.part"))
    assert not (output / "bin/large.bin").exists()
    assert hashlib.sha256(original.read_bytes()).hexdigest() == before
    result = json.loads(report.read_text())
    validate_report(result)
    assert result["status"] == "cancelled"
    assert result["exit_code"] == 130
    assert result["statistics"]["copied"] == result["statistics"]["bytes_copied"] == 0
    assert result["statistics"]["errors"] == int(fail_write)
    if fail_write:
        assert "disk failed during shutdown" in stderr


def test_two_real_processes_preserve_existing_and_colliding_files(
    tmp_path: Path,
) -> None:
    first, second, output = (tmp_path / name for name in ("first", "second", "out"))
    first.mkdir()
    second.mkdir()
    (first / "same.bin").write_bytes(b"first" * 300_000)
    (second / "same.bin").write_bytes(b"second" * 300_000)
    (output / "bin").mkdir(parents=True)
    (output / "bin/same.bin").write_bytes(b"existing")
    reports = [tmp_path / "first.json", tmp_path / "second.json"]
    with (
        probe("race", first, output, reports[0]) as one,
        probe("race", second, output, reports[1]) as two,
    ):
        expect_line(one, "READY")
        expect_line(two, "READY")
        assert (output / "bin/same.bin").read_bytes() == b"existing"
        assert not (output / "bin/same_1.bin").exists()
        release(one)
        release(two)
        for process in (one, two):
            stdout, stderr = process.communicate(timeout=15)
            assert process.returncode == 0, (stdout, stderr)
    assert {p.read_bytes() for p in (output / "bin").iterdir()} == {
        b"existing",
        b"first" * 300_000,
        b"second" * 300_000,
    }
    assert not list(output.rglob(".hw05-*.part"))
    results = [json.loads(path.read_text()) for path in reports]
    for result in results:
        validate_report(result)
    assert all(
        r["status"] == "success" and r["statistics"]["copied"] == 1 for r in results
    )
    assert sorted(r["statistics"]["collisions"] for r in results) == [1, 2]
    assert sum(r["statistics"]["bytes_copied"] for r in results) == 3_300_000


def test_sigint_during_publication_accounts_for_completed_file(tmp_path: Path) -> None:
    source = tmp_path / "src"
    output, report = tmp_path / "out", tmp_path / "run.json"
    source.mkdir()
    (source / "file.bin").write_bytes(b"complete")
    with probe("commit", source, output, report) as process:
        expect_line(process, "READY")
        assert (output / "bin/file.bin").read_bytes() == b"complete"
        process.send_signal(signal.SIGINT)
        expect_line(process, "STOP")
        release(process)
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 130, (stdout, stderr)
    result = json.loads(report.read_text())
    validate_report(result)
    assert result["status"] == "cancelled"
    assert result["statistics"]["copied"] == 1
    assert result["statistics"]["bytes_copied"] == 8
    assert not list(output.rglob(".hw05-*.part"))
