import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from anyio import Path as AsyncPath

import async_sorter
from async_sorter import SortStats, copy_file, read_folder, sort_files


def test_recursive_binary_copy_and_source_preservation(tmp_path: Path) -> None:
    """Sort nested and unusual filenames while preserving every source byte."""
    source, output = tmp_path / "джерело з пробілами", tmp_path / "output"
    files = {
        "report.txt": b"first",
        "nested/report.txt": b"second",
        "nested/MORE.TXT": b"third",
        "nested/deeper/image.bin": bytes(range(256)) * 5000,
        "empty.bin": b"",
        "README": b"no extension",
        ".settings": b"hidden",
        "archive.tar.gz": b"suffix example",
        "trailing.": b"dot",
    }
    for name, content in files.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    stats = asyncio.run(sort_files(AsyncPath(source), AsyncPath(output), workers=3))
    assert stats == SortStats(
        copied=9, bytes_copied=sum(map(len, files.values())), collisions=1
    )
    assert {p.read_bytes() for p in (output / "txt").iterdir()} == {
        b"first",
        b"second",
        b"third",
    }
    assert (output / "bin/image.bin").read_bytes() == files["nested/deeper/image.bin"]
    assert (output / "bin/empty.bin").read_bytes() == b""
    assert (output / "gz/archive.tar.gz").read_bytes() == b"suffix example"
    assert {p.name for p in (output / "no_extension").iterdir()} == {
        "README",
        ".settings",
        "trailing.",
    }
    assert {
        str(p.relative_to(source)): p.read_bytes()
        for p in source.rglob("*")
        if p.is_file()
    } == files


def test_concurrent_collisions_and_repeat_preserve_all_contents(tmp_path: Path) -> None:
    """Keep every copy across concurrent name collisions and repeated runs."""
    source, output = tmp_path / "source", tmp_path / "output"
    for number in range(24):
        folder = source / str(number)
        folder.mkdir(parents=True)
        (folder / "same.txt").write_text(str(number))
    (output / "txt").mkdir(parents=True)
    (output / "txt/same.txt").write_text("existing")
    for _ in range(2):
        stats = asyncio.run(sort_files(AsyncPath(source), AsyncPath(output), workers=8))
        assert stats.copied == 24
        assert stats.errors == 0
    copies = list((output / "txt").iterdir())
    assert len(copies) == 49
    assert (output / "txt/same.txt").read_text() == "existing"
    assert {p.read_text() for p in copies} == {str(n) for n in range(24)} | {"existing"}


@pytest.mark.parametrize("relation", ["same", "inside", "outside", "alias"])
def test_overlapping_paths_are_rejected(tmp_path: Path, relation: str) -> None:
    """Reject equal, nested, and symlink-aliased source and output paths."""
    source = tmp_path / "source"
    source.mkdir()
    outputs = {"same": source, "inside": source / "out", "outside": tmp_path}
    alias = tmp_path / "alias"
    alias.symlink_to(source, target_is_directory=True)
    outputs["alias"] = alias
    with pytest.raises(ValueError, match="вкладатися"):
        asyncio.run(sort_files(AsyncPath(source), AsyncPath(outputs[relation])))
    assert not (source / "out").exists()


def test_invalid_source_and_worker_count(tmp_path: Path) -> None:
    """Reject missing or nondirectory sources and nonpositive worker counts."""
    source = tmp_path / "source"
    output = AsyncPath(tmp_path / "output")
    with pytest.raises(FileNotFoundError):
        asyncio.run(sort_files(AsyncPath(source), output))
    source.write_text("not a directory")
    with pytest.raises(ValueError, match="каталогом"):
        asyncio.run(sort_files(AsyncPath(source), output))
    with pytest.raises(ValueError, match="workers"):
        asyncio.run(sort_files(AsyncPath(source), output, workers=0))


def test_empty_source(tmp_path: Path) -> None:
    """Return zero statistics for an empty source directory."""
    source = tmp_path / "source"
    source.mkdir()
    assert (
        asyncio.run(sort_files(AsyncPath(source), AsyncPath(tmp_path / "out")))
        == SortStats()
    )


def test_links_skipped_and_destination_link_rejected(tmp_path: Path) -> None:
    """Skip source links and prevent writes through an extension-directory link."""
    source, output, outside = (tmp_path / name for name in ("source", "out", "outside"))
    source.mkdir()
    output.mkdir()
    outside.mkdir()
    (source / "file.txt").write_text("data")
    (source / "file-link").symlink_to(source / "file.txt")
    (source / "loop").symlink_to(source, target_is_directory=True)
    (source / "broken").symlink_to(source / "missing")
    (output / "txt").symlink_to(outside, target_is_directory=True)
    stats = asyncio.run(sort_files(AsyncPath(source), AsyncPath(output)))
    assert stats == SortStats(skipped=3, errors=1)
    assert list(outside.iterdir()) == []


def test_copy_error_does_not_stop_other_files(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Log one blocked copy while allowing an independent file to finish."""
    source, output = tmp_path / "source", tmp_path / "output"
    source.mkdir()
    output.mkdir()
    (source / "bad.txt").write_text("bad")
    (source / "good.bin").write_bytes(b"good")
    (output / "txt").write_text("a file blocks the extension directory")
    stats = asyncio.run(sort_files(AsyncPath(source), AsyncPath(output), workers=1))
    assert stats == SortStats(copied=1, errors=1, bytes_copied=4)
    assert (output / "bin/good.bin").read_bytes() == b"good"
    assert "bad.txt" in caplog.text


def test_read_errors_are_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Count and log both directory traversal and entry inspection failures."""

    async def run() -> SortStats:
        """Exercise missing-directory and denied-entry errors in one event loop."""
        stats = SortStats()
        queue: asyncio.Queue[AsyncPath | None] = asyncio.Queue()
        await read_folder(AsyncPath(tmp_path / "missing"), queue, stats)
        (tmp_path / "file").touch()
        with patch.object(AsyncPath, "lstat", side_effect=PermissionError("denied")):
            await read_folder(AsyncPath(tmp_path), queue, stats)
        return stats

    assert asyncio.run(run()).errors == 2
    assert "missing" in caplog.text
    assert "denied" in caplog.text


@pytest.mark.parametrize("cancel", [False, True])
def test_partial_copy_removed_on_error_or_cancellation(
    tmp_path: Path, cancel: bool
) -> None:
    """Remove incomplete copies after write failure or task cancellation."""
    source = tmp_path / "file.txt"
    source.write_text("original")
    output = tmp_path / "out"
    error = asyncio.CancelledError() if cancel else OSError("disk full")
    with patch("anyio.AsyncFile.write", new_callable=AsyncMock, side_effect=error):
        with pytest.raises(type(error)):
            asyncio.run(copy_file(AsyncPath(source), AsyncPath(output)))
    assert list((output / "txt").iterdir()) == []
    assert source.read_text() == "original"


def test_cleanup_failure_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Log cleanup failure while preserving the original write exception."""
    source = tmp_path / "file.txt"
    source.write_text("original")
    with (
        patch(
            "anyio.AsyncFile.write", new_callable=AsyncMock, side_effect=OSError("disk")
        ),
        patch.object(AsyncPath, "unlink", side_effect=OSError("cleanup")),
        pytest.raises(OSError, match="disk"),
    ):
        asyncio.run(copy_file(AsyncPath(source), AsyncPath(tmp_path / "out")))
    assert "неповну копію" in caplog.text


def test_workers_are_concurrent_and_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate real copies to verify overlap without exceeding two workers."""
    source = tmp_path / "source"
    source.mkdir()
    for number in range(12):
        (source / f"{number}.txt").write_text(str(number))
    original = copy_file

    async def run() -> None:
        """Coordinate worker overlap and check completion within time limits."""
        active = 0
        peak = 0
        entered, release = asyncio.Event(), asyncio.Event()

        async def gated_copy(
            path: AsyncPath, output: AsyncPath, stats: SortStats | None = None
        ) -> AsyncPath:
            """Track active copies and pause them until the test releases the gate."""
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 2:
                entered.set()
            try:
                await release.wait()
                return await original(path, output, stats)
            finally:
                active -= 1

        monkeypatch.setattr(async_sorter, "copy_file", gated_copy)
        task = asyncio.create_task(
            sort_files(AsyncPath(source), AsyncPath(tmp_path / "out"), 2)
        )
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
        finally:
            release.set()
        stats = await asyncio.wait_for(task, timeout=10)
        assert stats == SortStats(copied=12, bytes_copied=14)
        assert peak == 2

    asyncio.run(run())


def test_io_error_during_cancellation_does_not_keep_worker_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Require a cancelled worker to exit even if its active transfer fails."""

    async def run() -> None:
        """Cancel an active worker and verify its queue item is acknowledged."""
        entered = asyncio.Event()

        async def failing_transfer(
            source: AsyncPath, output: AsyncPath, stats: SortStats, stop: asyncio.Event
        ) -> AsyncPath:
            """Raise an I/O error only after the copy receives its stop signal."""
            entered.set()
            await stop.wait()
            raise OSError("disk failed during shutdown")

        monkeypatch.setattr(async_sorter, "_copy_file", failing_transfer)
        queue: asyncio.Queue[AsyncPath | None] = asyncio.Queue()
        await queue.put(AsyncPath(tmp_path / "source.txt"))
        stats = SortStats()
        worker = asyncio.create_task(
            async_sorter.copy_worker(queue, AsyncPath(tmp_path / "out"), stats)
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(worker, timeout=1)
        await asyncio.wait_for(queue.join(), timeout=1)
        assert stats.errors == 1

    asyncio.run(run())
    assert "disk failed during shutdown" in caplog.text
    assert "source.txt" in caplog.text


@pytest.mark.parametrize("nested", [False, True])
def test_case_only_overlapping_paths_are_rejected(tmp_path: Path, nested: bool) -> None:
    """Reject case-only source aliases consistently across filesystems."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "original.txt").write_text("original")
    output = tmp_path / "SOURCE"
    if nested:
        output /= "nested"
    with pytest.raises(ValueError, match="вкладатися"):
        asyncio.run(sort_files(AsyncPath(source), AsyncPath(output)))
    assert list(source.iterdir()) == [source / "original.txt"]
