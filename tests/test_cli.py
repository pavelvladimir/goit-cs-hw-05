import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import async_sorter
import mapreduce_words
from common import configure_logging, positive_float, positive_int

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("value", ["0", "-1", "abc", "1.5"])
def test_bad_integer(value: str) -> None:
    """Reject nonpositive and noninteger worker arguments."""
    with pytest.raises(argparse.ArgumentTypeError):
        positive_int(value)


@pytest.mark.parametrize("value", ["0", "-1", "abc", "nan", "inf", "-inf"])
def test_bad_float(value: str) -> None:
    """Reject invalid, nonpositive, and nonfinite timeout arguments."""
    with pytest.raises(argparse.ArgumentTypeError):
        positive_float(value)


def test_valid_numbers() -> None:
    """Accept positive integers and fractional timeouts."""
    assert positive_int("3") == 3
    assert positive_float("0.25") == 0.25


@pytest.mark.parametrize("existing", [False, True])
def test_logging_to_file_and_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], existing: bool
) -> None:
    """Log Unicode errors to both destinations without truncating logs."""
    logger = logging.getLogger()
    previous_handlers, previous_level = logger.handlers[:], logger.level
    logfile = tmp_path / "logs/errors.log"
    if existing:
        logfile.parent.mkdir()
        logfile.write_text("earlier\n")
    try:
        configure_logging(logfile)
        logging.error("перевірка помилки")
        assert "перевірка помилки" in logfile.read_text()
        assert "перевірка помилки" in capsys.readouterr().err
        if existing:
            assert logfile.read_text().startswith("earlier\n")
    finally:
        for handler in logger.handlers:
            handler.close()
        logger.handlers = previous_handlers
        logger.setLevel(previous_level)


def test_log_must_be_a_regular_file(tmp_path: Path) -> None:
    """Reject a directory as the log destination."""
    with pytest.raises(ValueError, match="звичайним файлом"):
        configure_logging(tmp_path)


def test_sorter_main_statuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Check sorter exit codes for success, errors, and interruption."""
    source, output = tmp_path / "src", tmp_path / "out"
    source.mkdir()
    (source / "file.txt").write_text("text")
    with patch.object(async_sorter, "configure_logging"):
        assert async_sorter.main([str(source), str(output)]) == 0
        assert "Скопійовано: 1" in capsys.readouterr().out
        assert async_sorter.main([str(source / "missing"), str(output)]) == 1
        with patch.object(async_sorter, "sort_files", side_effect=KeyboardInterrupt):
            assert async_sorter.main([str(source), str(output)]) == 130
        with patch.object(
            async_sorter, "sort_files", return_value=async_sorter.SortStats(errors=1)
        ):
            assert async_sorter.main([str(source), str(output)]) == 1


def test_word_main_statuses(
    http_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Check word CLI output and download, chart, and interrupt failures."""
    output = tmp_path / "top.png"
    with patch.object(mapreduce_words, "configure_logging"):
        assert (
            mapreduce_words.main(
                [f"{http_url}/text", "--output", str(output), "--top", "2"]
            )
            == 0
        )
        assert "python\t2" in capsys.readouterr().out
        assert output.is_file()
        assert mapreduce_words.main([f"{http_url}/empty"]) == 1
        assert mapreduce_words.main([f"{http_url}/missing"]) == 1
        with patch.object(
            mapreduce_words, "download_text", side_effect=TimeoutError("timed out")
        ):
            assert mapreduce_words.main([]) == 1
        with patch.object(
            mapreduce_words, "download_text", side_effect=KeyboardInterrupt
        ):
            assert mapreduce_words.main([]) == 130
        blocked = tmp_path / "blocked"
        blocked.write_text("file")
        assert (
            mapreduce_words.main(
                [f"{http_url}/text", "--output", str(blocked / "plot.png")]
            )
            == 1
        )


@pytest.mark.parametrize("script", ["async_sorter.py", "mapreduce_words.py"])
def test_real_cli_help_and_validation(script: str) -> None:
    """Verify help and argument rejection in fresh CLI processes."""
    help_result = subprocess.run(
        [sys.executable, str(ROOT / script), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert help_result.returncode == 0
    assert "usage:" in help_result.stdout
    invalid = subprocess.run(
        [sys.executable, str(ROOT / script), "--workers", "0"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert invalid.returncode == 2


def test_real_cli_end_to_end(tmp_path: Path, http_url: str) -> None:
    """Run both standalone scripts and verify their binary outputs."""
    source, output = tmp_path / "source", tmp_path / "sorted"
    source.mkdir()
    (source / "data.txt").write_bytes(b"\x00\xffcontent")
    commands = [
        ["async_sorter.py", str(source), str(output)],
        [
            "mapreduce_words.py",
            f"{http_url}/text",
            "--output",
            str(tmp_path / "top.png"),
        ],
    ]
    for command in commands:
        result = subprocess.run(
            [sys.executable, str(ROOT / command[0]), *command[1:]],
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "MPLBACKEND": "Agg"},
        )
        assert result.returncode == 0, result.stderr
    assert (output / "txt/data.txt").read_bytes() == b"\x00\xffcontent"
    assert (tmp_path / "top.png").read_bytes().startswith(b"\x89PNG")
