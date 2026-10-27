"""Task 1: recursively copy files into extension folders using asynchronous I/O."""

import argparse
import asyncio
import logging
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from anyio import Path as AsyncPath

from common import (
    configure_logging,
    finish_report,
    new_report,
    paths_overlap,
    positive_int,
    validate_output_paths,
)

LOGGER = logging.getLogger(__name__)
CHUNK_SIZE = 1024 * 1024


@dataclass
class SortStats:
    """Counters updated on the event-loop thread only."""

    copied: int = 0
    skipped: int = 0
    errors: int = 0
    bytes_copied: int = 0
    collisions: int = 0


async def _copy_file(
    source: AsyncPath, output: AsyncPath, stats: SortStats, stop: asyncio.Event
) -> AsyncPath:
    """Write privately, close, then publish with an atomic no-overwrite hard link."""
    extension = source.suffix.lstrip(".").lower() or "no_extension"
    folder = output / extension
    if await folder.is_symlink():
        raise OSError(f"Цільова підпапка є символічним посиланням: {folder}")
    await folder.mkdir(parents=True, exist_ok=True)
    temporary = folder / f".hw05-{uuid4().hex}.part"
    created = False
    size = 0
    try:
        async with await source.open("rb") as reader:
            writer = await temporary.open("xb")
            created = True
            async with writer:
                while True:
                    if stop.is_set():
                        raise asyncio.CancelledError
                    chunk = await reader.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    await writer.write(chunk)
                    size += len(chunk)
        target = folder / source.name
        number = 0
        while True:
            if stop.is_set():
                raise asyncio.CancelledError
            try:
                await target.hardlink_to(temporary)
                break
            except FileExistsError:
                number += 1
                target = folder / f"{source.stem}_{number}{source.suffix}"
        # No await between publication and accounting: cancelled reports stay accurate.
        stats.copied += 1
        stats.bytes_copied += size
        stats.collisions += number
        return target
    finally:
        if created:
            try:
                await temporary.unlink(missing_ok=True)
            except OSError:
                stats.errors += 1
                LOGGER.exception("Не вдалося видалити неповну копію %s", temporary)


async def copy_file(
    source: AsyncPath, output: AsyncPath, stats: SortStats | None = None
) -> AsyncPath:
    """Cancel between chunks, waiting for in-flight filesystem I/O and cleanup."""
    stats = stats if stats is not None else SortStats()
    stop = asyncio.Event()
    transfer = asyncio.create_task(_copy_file(source, output, stats, stop))
    try:
        return await asyncio.shield(transfer)
    except asyncio.CancelledError:
        stop.set()
        # A worker thread must finish its current operation before we close/unlink.
        try:
            await transfer
        except asyncio.CancelledError:
            pass
        except Exception:
            # Preserve cancellation: otherwise copy_worker could resume its queue
            # after the producer was cancelled and wait forever for a sentinel.
            stats.errors += 1
            LOGGER.exception("Помилка копіювання %s під час скасування", source)
        raise


async def read_folder(
    source: AsyncPath,
    queue: asyncio.Queue[AsyncPath | None],
    stats: SortStats,
) -> None:
    """Recursively discover regular files and feed a bounded worker queue."""
    try:
        async for entry in source.iterdir():
            try:
                mode = (await entry.lstat()).st_mode
                if stat.S_ISDIR(mode):
                    await read_folder(entry, queue, stats)
                elif stat.S_ISREG(mode):
                    await queue.put(entry)
                else:
                    stats.skipped += 1
                    LOGGER.warning(
                        "Пропущено посилання або спеціальний файл: %s", entry
                    )
            except OSError:
                stats.errors += 1
                LOGGER.exception("Не вдалося прочитати %s", entry)
    except OSError:
        stats.errors += 1
        LOGGER.exception("Не вдалося прочитати каталог %s", source)


async def copy_worker(
    queue: asyncio.Queue[AsyncPath | None], output: AsyncPath, stats: SortStats
) -> None:
    """Continue copying other files after a recoverable filesystem error."""
    while True:
        source = await queue.get()
        try:
            if source is None:
                return
            try:
                await copy_file(source, output, stats)
            except OSError:
                stats.errors += 1
                LOGGER.exception("Не вдалося скопіювати %s", source)
        finally:
            queue.task_done()


async def sort_files(
    source: AsyncPath,
    output: AsyncPath,
    workers: int = 8,
    *,
    stats: SortStats | None = None,
) -> SortStats:
    """Validate disjoint paths, then run recursive discovery and fixed workers."""
    if workers < 1:
        raise ValueError("Кількість workers має бути більше нуля.")
    source = await source.resolve(strict=True)
    output = await output.resolve()
    if not stat.S_ISDIR((await source.stat()).st_mode):
        raise ValueError("Вихідний шлях має бути каталогом.")
    if paths_overlap(Path(source), Path(output)):
        raise ValueError(
            "Вихідний і цільовий каталоги не можуть вкладатися один в одного."
        )
    await output.mkdir(parents=True, exist_ok=True)
    stats = stats if stats is not None else SortStats()
    queue: asyncio.Queue[AsyncPath | None] = asyncio.Queue(maxsize=workers * 2)
    async with asyncio.TaskGroup() as group:
        for _ in range(workers):
            group.create_task(copy_worker(queue, output, stats))
        await read_folder(source, queue, stats)
        for _ in range(workers):
            await queue.put(None)
    return stats


def build_parser() -> argparse.ArgumentParser:
    """Define source, destination, concurrency, and optional persistent logging."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Вихідний каталог")
    parser.add_argument("output", type=Path, help="Цільовий каталог")
    parser.add_argument("--workers", type=positive_int, default=8)
    parser.add_argument("--log-file", type=Path, help="Додатковий UTF-8 журнал")
    parser.add_argument("--report", type=Path, help="JSON-звіт поза source та output")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the asynchronous pipeline and return a useful shell exit status."""
    args = build_parser().parse_args(argv)
    started = perf_counter()
    report = new_report("async_sorter")
    stats = SortStats()
    report_allowed = False
    exit_code = 0
    try:
        validate_output_paths(args.report, args.log_file)
        for path in (args.report, args.log_file):
            if path is not None and any(
                paths_overlap(path.resolve(), root.resolve())
                for root in (args.source, args.output)
            ):
                raise ValueError("Журнал і звіт мають бути поза source та output.")
        report_allowed = True
        configure_logging(args.log_file)
        result = asyncio.run(
            sort_files(
                AsyncPath(args.source),
                AsyncPath(args.output),
                args.workers,
                stats=stats,
            )
        )
        stats = result
        exit_code = 1 if stats.errors else 0
    except KeyboardInterrupt:
        LOGGER.warning("Сортування перервано користувачем.")
        exit_code = 130
    except Exception as error:
        LOGGER.exception("Сортування не завершено.")
        stats.errors += 1
        report["error"] = str(error)
        exit_code = 1
    print(
        f"Скопійовано: {stats.copied}; пропущено: {stats.skipped}; "
        f"помилок: {stats.errors}."
    )
    report.update(
        source=str(args.source.absolute()),
        output=str(args.output.absolute()),
        parameters={"workers": args.workers, "chunk_bytes": CHUNK_SIZE},
        statistics=asdict(stats),
    )
    return finish_report(
        args.report if report_allowed else None, report, started, exit_code
    )


if __name__ == "__main__":
    raise SystemExit(main())
