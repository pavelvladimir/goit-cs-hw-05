"""Task 2: download text, count words with threaded MapReduce, and plot the top."""

import argparse
import hashlib
import itertools
import logging
import math
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import perf_counter
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from matplotlib.figure import Figure
from matplotlib.ticker import MaxNLocator

from common import (
    atomic_output,
    configure_logging,
    finish_report,
    new_report,
    positive_float,
    positive_int,
    validate_output_paths,
)

LOGGER = logging.getLogger(__name__)
DEFAULT_URL = "https://www.gutenberg.org/files/1342/1342-0.txt"
MAX_BYTES = 20 * 1024 * 1024


def download_text(
    url: str,
    timeout: float = 20,
    max_bytes: int = MAX_BYTES,
    *,
    metadata: dict[str, object] | None = None,
) -> str:
    """Fetch HTTP(S) text, honoring a declared charset or falling back to UTF-8."""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Потрібна повна HTTP або HTTPS URL-адреса.")
    if not math.isfinite(timeout) or timeout <= 0 or max_bytes < 1:
        raise ValueError("Timeout і ліміт розміру мають бути додатними.")
    request = Request(url, headers={"User-Agent": "goit-cs-hw-05/0.1"})
    try:
        response = urlopen(request, timeout=timeout)
    except HTTPError as error:
        # HTTPError owns the error response body even though urlopen raised.
        error.close()
        raise
    with response:
        data: bytes = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError(f"Текст перевищує ліміт {max_bytes} байтів.")
        expected_length = response.headers.get("Content-Length")
        if expected_length is not None and len(data) != int(expected_length):
            raise OSError("HTTP-відповідь обірвалася до завершення завантаження.")
        charset: str = response.headers.get_content_charset() or "utf-8-sig"
        final_url: str = response.geturl()
    text = data.decode(charset)
    if metadata is not None:
        metadata.update(
            requested_url=url,
            final_url=final_url,
            charset=charset,
            bytes_downloaded=len(data),
            sha256_bytes=hashlib.sha256(data).hexdigest(),
            sha256_text_utf8=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )
    return text


def tokenize(text: str) -> Iterator[str]:
    """Normalize Unicode/case/apostrophes; keep words, not numbers or punctuation."""
    normalized = unicodedata.normalize("NFC", text).casefold()
    normalized = unicodedata.normalize("NFC", normalized)
    normalized = normalized.replace("’", "'").replace("ʼ", "'")
    word: list[str] = []
    for character in normalized:
        if character.isalpha():
            word.append(character)
        elif (
            word
            and word[-1] != "'"
            and (character == "'" or unicodedata.category(character).startswith("M"))
        ):
            # Keep combining marks attached to letters (e.g. i + dot or Hindi).
            word.append(character)
        elif word:
            yield "".join(word).rstrip("'")
            word.clear()
    if word:
        yield "".join(word).rstrip("'")


def map_function(word: str) -> tuple[str, int]:
    """Map: emit the textbook (word, 1) pair."""
    return word, 1


def map_chunk(words: tuple[str, ...]) -> dict[str, int]:
    """Combine a batch locally to avoid one future and one stored value per word."""
    counts: dict[str, int] = defaultdict(int)
    for word in words:
        key, value = map_function(word)
        counts[key] += value
    return dict(counts)


def shuffle_function(mapped: Iterable[dict[str, int]]) -> dict[str, list[int]]:
    """Shuffle: group partial counts by their word key."""
    grouped: dict[str, list[int]] = defaultdict(list)
    for partial in mapped:
        for word, count in partial.items():
            grouped[word].append(count)
    return dict(grouped)


def reduce_function(item: tuple[str, list[int]]) -> tuple[str, int]:
    """Reduce: sum every partial count for one word."""
    word, counts = item
    return word, sum(counts)


def map_reduce(text: str, workers: int = 4, chunk_size: int = 2000) -> dict[str, int]:
    """Run both Map and Reduce in a thread pool with bounded pending submissions."""
    if workers < 1 or chunk_size < 1:
        raise ValueError("Workers і chunk_size мають бути більше нуля.")
    chunks = itertools.batched(tokenize(text), chunk_size, strict=False)
    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="mapreduce"
    ) as pool:
        mapped = pool.map(map_chunk, chunks, buffersize=workers * 2)
        grouped = shuffle_function(mapped)
        reduced = pool.map(reduce_function, grouped.items(), buffersize=workers * 2)
        return dict(reduced)


def top_words(counts: Mapping[str, int], top_n: int = 10) -> list[tuple[str, int]]:
    """Use descending counts and alphabetical tie-breaking for reproducibility."""
    if top_n < 1:
        raise ValueError("Кількість top-слів має бути більше нуля.")
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:top_n]


def visualize_top_words(
    counts: Mapping[str, int],
    top_n: int = 10,
    output: Path | None = None,
    *,
    show: bool = False,
) -> Figure:
    """Draw a horizontal chart like the assignment example; save without a GUI."""
    selected = top_words(counts, top_n)
    if not selected:
        raise ValueError("Текст не містить слів для візуалізації.")
    size = (10, max(4, len(selected) * 0.45 + 1.5))
    if show:
        import matplotlib.pyplot as plt

        figure = plt.figure(figsize=size, layout="constrained")
    else:
        figure = Figure(figsize=size, layout="constrained")
    axes = figure.subplots()
    words, frequencies = zip(*selected, strict=True)
    axes.barh(words, frequencies, color="skyblue")
    axes.invert_yaxis()
    axes.set_title(f"Top {len(selected)} Most Frequent Words")
    axes.set_xlabel("Frequency")
    axes.set_ylabel("Words")
    axes.xaxis.set_major_locator(MaxNLocator(integer=True))
    try:
        if output is not None:
            with atomic_output(output) as temporary:
                figure.savefig(
                    temporary, format=output.suffix.lstrip(".") or "png", dpi=150
                )
        if show:
            plt.show()
    finally:
        if show:
            plt.close(figure)
    return figure


def build_parser() -> argparse.ArgumentParser:
    """Define the URL, pool settings, chart destination, and download limits."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "url", nargs="?", default=DEFAULT_URL, help="URL звичайного тексту"
    )
    parser.add_argument("--top", type=positive_int, default=10)
    parser.add_argument("--workers", type=positive_int, default=4)
    parser.add_argument("--chunk-size", type=positive_int, default=2000)
    parser.add_argument("--timeout", type=positive_float, default=20.0)
    parser.add_argument("--max-bytes", type=positive_int, default=MAX_BYTES)
    parser.add_argument("--output", type=Path, default=Path("artifacts/top_words.png"))
    parser.add_argument(
        "--show", action="store_true", help="Також відкрити вікно графіка"
    )
    parser.add_argument("--log-file", type=Path)
    parser.add_argument("--report", type=Path, help="JSON-звіт із SHA-256 та частотами")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Download, analyze, render, and print the actual counts."""
    args = build_parser().parse_args(argv)
    started = perf_counter()
    report = new_report("mapreduce_words")
    source: dict[str, object] = {"requested_url": args.url}
    counts: dict[str, int] = {}
    report_allowed = False
    exit_code = 0
    try:
        validate_output_paths(args.output, args.report, args.log_file)
        report_allowed = True
        configure_logging(args.log_file)
        text = download_text(args.url, args.timeout, args.max_bytes, metadata=source)
        counts = map_reduce(text, args.workers, args.chunk_size)
        visualize_top_words(counts, args.top, args.output, show=args.show)
    except KeyboardInterrupt:
        LOGGER.warning("Аналіз перервано користувачем.")
        exit_code = 130
    except Exception as error:
        LOGGER.exception("Аналіз не завершено.")
        report["error"] = str(error)
        exit_code = 1
    if exit_code == 0:
        for word, count in top_words(counts, args.top):
            print(f"{word}\t{count}")
        print(f"Усього слів: {sum(counts.values())}; унікальних: {len(counts)}.")
        print(f"Графік: {args.output}")
    report.update(
        source=source,
        output=str(args.output.absolute()),
        parameters={
            "workers": args.workers,
            "chunk_size": args.chunk_size,
            "top": args.top,
            "timeout": args.timeout,
            "max_bytes": args.max_bytes,
        },
        statistics={
            "total_words": sum(counts.values()),
            "unique_words": len(counts),
            "errors": int(exit_code == 1),
        },
        frequencies=dict(sorted(counts.items())),
        top_words=[
            {"word": word, "count": count}
            for word, count in top_words(counts, args.top)
        ],
    )
    return finish_report(
        args.report if report_allowed else None, report, started, exit_code
    )


if __name__ == "__main__":
    raise SystemExit(main())
