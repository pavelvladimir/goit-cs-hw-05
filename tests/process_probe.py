"""Subprocess-only synchronization gates; production CLI and I/O remain unchanged."""

import asyncio
import sys
from contextlib import suppress
from unittest.mock import patch

from anyio import AsyncFile
from anyio import Path as AsyncPath

import async_sorter


async def release_gate() -> None:
    print("READY", flush=True)
    await asyncio.to_thread(sys.stdin.readline)


def install_interrupt_gate(
    *, pause_write: bool = True, fail_write: bool = False
) -> None:
    original_write = AsyncFile.write
    original_copy = async_sorter._copy_file
    paused = False

    async def write(self: AsyncFile[bytes], data: bytes) -> int:
        nonlocal paused
        result = await original_write(self, data)
        if not paused:
            paused = True
            await release_gate()
            if fail_write:
                raise OSError("disk failed during shutdown")
        return result

    async def copy(
        source: AsyncPath,
        output: AsyncPath,
        stats: async_sorter.SortStats,
        stop: asyncio.Event,
    ) -> AsyncPath:
        async def notify() -> None:
            await stop.wait()
            print("STOP", flush=True)

        watcher = asyncio.create_task(notify())
        try:
            return await original_copy(source, output, stats, stop)
        finally:
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher

    if pause_write:
        patch.object(AsyncFile, "write", write).start()
    async_sorter._copy_file = copy


def install_publication_gate(*, after: bool = False) -> None:
    original = AsyncPath.hardlink_to
    paused = False

    async def link(self: AsyncPath, target: AsyncPath) -> None:
        nonlocal paused
        if after:
            await original(self, target)
        if not paused:
            paused = True
            await release_gate()
        if not after:
            await original(self, target)

    patch.object(AsyncPath, "hardlink_to", link).start()


if __name__ == "__main__":
    if sys.argv[1] in {"interrupt", "interrupt-error"}:
        install_interrupt_gate(fail_write=sys.argv[1] == "interrupt-error")
    elif sys.argv[1] == "commit":
        install_interrupt_gate(pause_write=False)
        install_publication_gate(after=True)
    else:
        install_publication_gate()
    raise SystemExit(async_sorter.main(sys.argv[2:]))
