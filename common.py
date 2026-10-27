"""Small, shared CLI helpers for the two standalone scripts."""

import argparse
import json
import logging
import math
import os
import stat
import tempfile
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

LOGGER = logging.getLogger(__name__)


@contextmanager
def atomic_output(path: Path) -> Iterator[Path]:
    """Replace a complete output; retain the previous file on failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=".hw05-", suffix=".part", dir=path.parent
    )
    os.close(descriptor)
    temporary = Path(name)
    try:
        yield temporary
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            LOGGER.exception("Не вдалося прибрати тимчасовий файл %s", temporary)


def write_json(path: Path, data: object) -> None:
    """Write UTF-8 JSON atomically, rejecting non-finite numbers."""
    with atomic_output(path) as temporary:
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )


def new_report(task: str) -> dict[str, object]:
    """Stable common envelope; task-specific fields are added by each CLI."""
    return {
        "schema_version": 1,
        "task": task,
        "started_at": datetime.now(UTC).isoformat(),
    }


def finish_report(
    path: Path | None, report: dict[str, object], started: float, exit_code: int
) -> int:
    """Persist success, failure, or cancellation without hiding a report-write error."""
    if path is None:
        return exit_code
    report.update(
        finished_at=datetime.now(UTC).isoformat(),
        elapsed_seconds=perf_counter() - started,
        exit_code=exit_code,
        status="cancelled"
        if exit_code == 130
        else "success"
        if exit_code == 0
        else "failed",
    )
    try:
        # Only --report needs the validator; ordinary runs keep their startup cost.
        from report_validation import validate_report

        validate_report(report)
        write_json(path, report)
    except OSError, ValueError, TypeError:
        LOGGER.exception("Не вдалося зберегти звіт %s", path)
        return 130 if exit_code == 130 else 1
    return exit_code


def paths_overlap(left: Path, right: Path) -> bool:
    """Compare resolved paths conservatively, including case/Unicode aliases.

    Reject case-only distinctions even on case-sensitive filesystems so the
    same CLI arguments remain safe when moved between Linux and macOS.
    """
    left, right = (
        Path(unicodedata.normalize("NFC", str(path)).casefold())
        for path in (left, right)
    )
    return left.is_relative_to(right) or right.is_relative_to(left)


def validate_output_paths(*paths: Path | None) -> None:
    """Prevent report, image, and log paths from overwriting one another."""
    present = [path.resolve() for path in paths if path is not None]
    for index, left in enumerate(present):
        for right in present[index + 1 :]:
            if paths_overlap(left, right) or (
                left.exists() and right.exists() and left.samefile(right)
            ):
                raise ValueError(
                    "Шляхи результатів, журналу та звіту мають бути окремими."
                )


def nonnegative_int(value: str) -> int:
    """Accept an integer argument that may be zero."""
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Потрібне ціле невід'ємне число.") from error
    if number < 0:
        raise argparse.ArgumentTypeError("Число не може бути від'ємним.")
    return number


def positive_int(value: str) -> int:
    """Accept a strictly positive integer argument."""
    number = nonnegative_int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("Число має бути більше нуля.")
    return number


def positive_float(value: str) -> float:
    """Accept a finite positive timeout."""
    try:
        number = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Потрібне додатне число.") from error
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("Потрібне скінченне число більше нуля.")
    return number


def configure_logging(log_file: Path | None = None) -> None:
    """Log to stderr and optionally to a UTF-8 file."""
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file is not None:
        try:
            info = log_file.stat()
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                raise ValueError(
                    "Журнал має бути звичайним файлом без додаткових hard links."
                )
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
        handlers=handlers,
        force=True,
    )
