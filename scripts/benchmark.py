"""Reproducible, verified warm-cache benchmarks for both homework tasks."""

import argparse
import asyncio
import csv
import hashlib
import logging
import os
import platform
import random
import shutil
import statistics
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp
from time import perf_counter
from uuid import uuid4

from anyio import Path as AsyncPath
from matplotlib.figure import Figure

from async_sorter import CHUNK_SIZE, sort_files
from common import (
    atomic_output,
    configure_logging,
    nonnegative_int,
    positive_int,
    write_json,
)
from mapreduce_words import map_reduce, tokenize

ROOT = Path(__file__).resolve().parents[1]
WORKERS = (1, 4, 8, 16)


@dataclass(frozen=True)
class Config:
    small_files: int = 300
    small_bytes: int = 4096
    large_files: int = 3
    large_mib: int = 16
    words: int = 150_000
    repeats: int = 3
    warmups: int = 1
    seed: int = 42


@dataclass(frozen=True)
class Trial:
    dataset: str
    method: str
    workers: int
    repetition: int
    warmup: bool
    seconds: float
    units: int
    bytes: int


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inventory(folder: Path, *, source: bool = False) -> dict[str, str]:
    """Hash every file outside the timer, including unexpected leftover files."""
    result: dict[str, str] = {}
    for path in folder.rglob("*"):
        if path.is_file():
            extension = path.suffix.lstrip(".").lower() or "no_extension"
            key = (
                f"{extension}/{path.name}" if source else str(path.relative_to(folder))
            )
            if key in result:
                raise ValueError(
                    "Benchmark source filenames must be unique per extension."
                )
            result[key] = digest(path)
    return result


def generate_files(folder: Path, count: int, size: int, rng: random.Random) -> None:
    """Generate seeded bytes without sparse files or copy-on-write clones."""
    for index in range(count):
        path = folder / f"nested-{index % 8}" / f"file-{index}.bin"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as stream:
            remaining = size
            while remaining:
                block = rng.randbytes(min(CHUNK_SIZE, remaining))
                stream.write(block)
                remaining -= len(block)


def sequential_copy(source: Path, output: Path) -> None:
    """Sequential reference: the same chunk size and no-overwrite publication."""
    output.mkdir(parents=True, exist_ok=True)
    for path in source.iterdir():
        if path.is_dir():
            sequential_copy(path, output)
        else:
            folder = output / (path.suffix.lstrip(".").lower() or "no_extension")
            folder.mkdir(exist_ok=True)
            temporary = folder / f".hw05-{uuid4().hex}.part"
            try:
                with path.open("rb") as reader, temporary.open("xb") as writer:
                    shutil.copyfileobj(reader, writer, length=CHUNK_SIZE)
                os.link(temporary, folder / path.name)
            finally:
                temporary.unlink(missing_ok=True)


def benchmark_files(
    source: Path, work: Path, dataset: str, config: Config, rng: random.Random
) -> tuple[list[Trial], dict[str, str]]:
    expected = inventory(source, source=True)
    byte_count = sum(
        path.stat().st_size for path in source.rglob("*") if path.is_file()
    )
    trials: list[Trial] = []
    for repetition in range(config.warmups + config.repeats):
        variants = [0, *WORKERS]
        rng.shuffle(variants)
        for workers in variants:
            with TemporaryDirectory(prefix="copy-", dir=work) as raw:
                output = Path(raw) / "sorted"
                stats = None
                started = perf_counter()
                if workers:
                    stats = asyncio.run(
                        sort_files(AsyncPath(source), AsyncPath(output), workers)
                    )
                else:
                    sequential_copy(source, output)
                seconds = perf_counter() - started
                if stats is not None and (
                    stats.errors
                    or stats.copied != len(expected)
                    or stats.bytes_copied != byte_count
                ):
                    raise RuntimeError(
                        "Async benchmark did not copy every source file."
                    )
                if inventory(output) != expected:
                    raise RuntimeError("Copy benchmark failed SHA-256 verification.")
            trials.append(
                Trial(
                    dataset,
                    "async" if workers else "sequential",
                    workers,
                    repetition,
                    repetition < config.warmups,
                    seconds,
                    len(expected),
                    byte_count,
                )
            )
    if inventory(source, source=True) != expected:
        raise RuntimeError("Benchmark changed its source files.")
    return trials, expected


def benchmark_words(text: str, config: Config, rng: random.Random) -> list[Trial]:
    expected = dict(Counter(tokenize(text)))
    trials: list[Trial] = []
    for repetition in range(config.warmups + config.repeats):
        variants = [0, *WORKERS]
        rng.shuffle(variants)
        for workers in variants:
            started = perf_counter()
            actual = (
                map_reduce(text, workers) if workers else dict(Counter(tokenize(text)))
            )
            seconds = perf_counter() - started
            if actual != expected:
                raise RuntimeError("MapReduce benchmark differs from Counter.")
            trials.append(
                Trial(
                    "words",
                    "mapreduce" if workers else "counter",
                    workers,
                    repetition,
                    repetition < config.warmups,
                    seconds,
                    sum(expected.values()),
                    len(text.encode("utf-8")),
                )
            )
    return trials


def summarize(trials: list[Trial]) -> list[dict[str, str | int | float]]:
    rows: list[dict[str, str | int | float]] = []
    for dataset in dict.fromkeys(trial.dataset for trial in trials):
        baseline = statistics.median(
            t.seconds
            for t in trials
            if t.dataset == dataset and t.workers == 0 and not t.warmup
        )
        for workers in (0, *WORKERS):
            selected = [
                t
                for t in trials
                if t.dataset == dataset and t.workers == workers and not t.warmup
            ]
            times = [trial.seconds for trial in selected]
            median = statistics.median(times)
            rows.append(
                {
                    "dataset": dataset,
                    "method": selected[0].method,
                    "workers": workers,
                    "samples": len(times),
                    "median_seconds": median,
                    "min_seconds": min(times),
                    "max_seconds": max(times),
                    "speedup_vs_baseline": baseline / median,
                    "mib_per_second": selected[0].bytes / 1024**2 / median,
                }
            )
    return rows


def plot_results(rows: list[dict[str, str | int | float]], output: Path) -> None:
    figure = Figure(figsize=(13, 5), layout="constrained")
    for index, dataset in enumerate(("many_small", "few_large", "words"), start=1):
        selected = [row for row in rows if row["dataset"] == dataset]
        axes = figure.add_subplot(1, 3, index)
        labels = [
            str(row["method"]) if not row["workers"] else f"workers = {row['workers']}"
            for row in selected
        ]
        medians = [float(row["median_seconds"]) for row in selected]
        errors = [
            [
                mid - float(row["min_seconds"])
                for mid, row in zip(medians, selected, strict=True)
            ],
            [
                float(row["max_seconds"]) - mid
                for mid, row in zip(medians, selected, strict=True)
            ],
        ]
        axes.barh(
            labels, medians, xerr=errors, color=["#64748b", *["skyblue"] * 4], capsize=3
        )
        axes.invert_yaxis()
        axes.set_title(dataset.replace("_", " ").title())
        axes.set_xlabel("Seconds (median; range min–max)")
    figure.suptitle("Verified warm-cache benchmark — lower is faster")
    with atomic_output(output) as temporary:
        figure.savefig(temporary, format="png", dpi=150)


def run_benchmark(config: Config, output_root: Path) -> Path:
    """Create a unique result bundle; clean up only this run's disposable data."""
    if (
        any(
            value <= 0
            for name, value in asdict(config).items()
            if name not in {"seed", "warmups"}
        )
        or config.warmups < 0
    ):
        raise ValueError(
            "Benchmark sizes/repeats must be positive; warmups cannot be negative."
        )
    output_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S-")
    destination = Path(mkdtemp(prefix=stamp, dir=output_root))
    data_rng, order_rng = random.Random(config.seed), random.Random(config.seed + 1)
    trials: list[Trial] = []
    manifests: dict[str, dict[str, str]] = {}
    with TemporaryDirectory(prefix="data-", dir=destination) as raw:
        work = Path(raw)
        for dataset, count, size in (
            ("many_small", config.small_files, config.small_bytes),
            ("few_large", config.large_files, config.large_mib * 1024**2),
        ):
            source = work / dataset
            generate_files(source, count, size, data_rng)
            runs, manifest = benchmark_files(source, work, dataset, config, order_rng)
            trials.extend(runs)
            manifests[dataset] = manifest
        vocabulary = (
            "python",
            "data",
            "word",
            "mapreduce",
            "async",
            "файли",
            "дані",
            "слово",
        )
        text = " ".join(data_rng.choices(vocabulary, k=config.words))
        trials.extend(benchmark_words(text, config, order_rng))
    rows = summarize(trials)
    result = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "config": asdict(config),
        "environment": {
            "python": sys.version,
            "platform": platform.platform(),
            "machine": platform.machine(),
            "cpu_count": os.cpu_count(),
            "gil_enabled": getattr(sys, "_is_gil_enabled", lambda: None)(),
            "filesystem_location": destination.name,
        },
        "methodology": {
            "cache": "warm/uncontrolled OS cache; no cache flush",
            "order": "seeded shuffle per round; raw trials preserve order",
            "timed": "copy + event loop setup; or tokenization + counting",
            "excluded": "generation, hashes, download, plot, report, cleanup",
            "durability": "buffered close, no fsync; not durable storage timing",
        },
        "source_sha256": {
            name: digest(ROOT / name)
            for name in (
                "async_sorter.py",
                "mapreduce_words.py",
                "common.py",
                "scripts/benchmark.py",
                "uv.lock",
            )
        },
        "inputs": {
            "files_sha256": manifests,
            "text_sha256_utf8": hashlib.sha256(text.encode()).hexdigest(),
        },
        "trials": [asdict(trial) for trial in trials],
        "summary": rows,
    }
    write_json(destination / "results.json", result)
    with atomic_output(destination / "trials.csv") as temporary:
        with temporary.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(asdict(trials[0])))
            writer.writeheader()
            writer.writerows(asdict(trial) for trial in trials)
    plot_results(rows, destination / "comparison.png")
    for row in rows:
        print(
            f"{row['dataset']:12} {row['method']:10} workers={row['workers']:2} "
            f"median={row['median_seconds']:.6f}s "
            f"speedup={row['speedup_vs_baseline']:.2f}x"
        )
    print(f"Результати: {destination.name}")
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name, default in asdict(Config()).items():
        parser.add_argument(
            f"--{name.replace('_', '-')}",
            type=int
            if name == "seed"
            else nonnegative_int
            if name == "warmups"
            else positive_int,
            default=default,
        )
    parser.add_argument("--output", type=Path, default=Path("artifacts/benchmarks"))
    args = parser.parse_args(argv)
    configure_logging()
    try:
        run_benchmark(
            Config(**{name: getattr(args, name) for name in asdict(Config())}),
            args.output,
        )
    except KeyboardInterrupt:
        logging.warning("Benchmark перервано.")
        return 130
    except Exception:
        logging.exception("Benchmark не завершено.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
