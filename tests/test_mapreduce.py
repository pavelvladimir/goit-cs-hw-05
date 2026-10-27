import threading
from collections import Counter
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

import mapreduce_words
from mapreduce_words import (
    download_text,
    map_reduce,
    tokenize,
    top_words,
    visualize_top_words,
)


def test_unicode_normalization() -> None:
    """Normalize case, composed accents, and apostrophe variants."""
    text = (
        "Hello, HELLO! П’ять пʼять П'ЯТЬ. Café cafe\u0301 "
        "STRASSE Straße; 123 _ -- data-driven."
    )
    assert list(tokenize(text)) == [
        "hello",
        "hello",
        "п'ять",
        "п'ять",
        "п'ять",
        "café",
        "café",
        "strasse",
        "strasse",
        "data",
        "driven",
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("² Ⅷ ½ ١٢٣ abc²def", ["abc", "def"]),
        ("İSTANBUL i\u0307stanbul", ["i\u0307stanbul", "i\u0307stanbul"]),
        ("नमस्ते नमस्ते", ["नमस्ते", "नमस्ते"]),
        (
            "\u0301 'word' rock''roll word'\u0301next",
            ["word", "rock", "roll", "word", "next"],
        ),
    ],
)
def test_unicode_numbers_marks_and_apostrophe_boundaries(
    text: str, expected: list[str]
) -> None:
    """Preserve letter marks while splitting numbers and invalid apostrophes."""
    assert list(tokenize(text)) == expected


@pytest.mark.parametrize(
    ("workers", "chunk_size"), [(1, 1), (2, 17), (4, 2000), (8, 99999)]
)
def test_mapreduce_matches_independent_counts(workers: int, chunk_size: int) -> None:
    """Match known counts across worker counts and chunk sizes."""
    words = ["python", "дані", "слово", "python", "дані", "mapreduce"] * 750
    assert map_reduce(" ".join(words), workers, chunk_size) == Counter(words)


@pytest.mark.parametrize("text", ["", "123... — !!!"])
def test_empty_text(text: str) -> None:
    """Return no counts and reject charts when the text has no words."""
    assert map_reduce(text) == {}
    with pytest.raises(ValueError, match="не містить слів"):
        visualize_top_words({})


@pytest.mark.parametrize(("workers", "chunk_size"), [(0, 1), (1, 0), (-1, 10)])
def test_bad_pool_settings(workers: int, chunk_size: int) -> None:
    """Reject nonpositive worker counts and chunk sizes."""
    with pytest.raises(ValueError):
        map_reduce("text", workers, chunk_size)


def test_map_and_reduce_really_use_multiple_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use barriers to prove both stages run in multiple worker threads."""
    mapper, reducer = mapreduce_words.map_chunk, mapreduce_words.reduce_function
    map_threads: set[int] = set()
    reduce_threads: set[int] = set()
    map_gate, reduce_gate = (
        threading.Barrier(2, timeout=5),
        threading.Barrier(2, timeout=5),
    )
    lock = threading.Lock()

    def traced_map(chunk: tuple[str, ...]) -> dict[str, int]:
        """Record mapper thread identities before synchronizing two workers."""
        with lock:
            map_threads.add(threading.get_ident())
        map_gate.wait()
        return mapper(chunk)

    def traced_reduce(item: tuple[str, list[int]]) -> tuple[str, int]:
        """Record reducer thread identities before synchronizing two workers."""
        with lock:
            reduce_threads.add(threading.get_ident())
        reduce_gate.wait()
        return reducer(item)

    monkeypatch.setattr(mapreduce_words, "map_chunk", traced_map)
    monkeypatch.setattr(mapreduce_words, "reduce_function", traced_reduce)
    assert map_reduce("alpha beta alpha beta", workers=2, chunk_size=2) == {
        "alpha": 2,
        "beta": 2,
    }
    assert len(map_threads) == len(reduce_threads) == 2
    assert threading.get_ident() not in map_threads | reduce_threads


def test_chart_order_labels_and_png(tmp_path: Path) -> None:
    """Verify descending bars, alphabetical ties, labels, and PNG output."""
    output = tmp_path / "charts/top.png"
    counts = {"beta": 2, "gamma": 1, "alpha": 2, "winner": 9}
    figure = visualize_top_words(counts, 3, output)
    axes = figure.axes[0]
    assert [label.get_text() for label in axes.get_yticklabels()] == [
        "winner",
        "alpha",
        "beta",
    ]
    assert axes.yaxis_inverted()
    assert [patch.get_width() for patch in axes.containers[0]] == [9, 2, 2]
    assert axes.get_xlabel() == "Frequency"
    assert axes.get_ylabel() == "Words"
    assert axes.get_title() == "Top 3 Most Frequent Words"
    assert output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert top_words(counts, 100) == [
        ("winner", 9),
        ("alpha", 2),
        ("beta", 2),
        ("gamma", 1),
    ]
    with pytest.raises(ValueError):
        top_words(counts, 0)


def test_show_closes_figure() -> None:
    """Close the interactive figure after the mocked display returns."""
    import matplotlib.pyplot as plt

    with patch.object(plt, "show") as show:
        visualize_top_words({"word": 2}, show=True)
    show.assert_called_once()
    assert plt.get_fignums() == []


def test_http_download_charsets_redirects_and_limits(http_url: str) -> None:
    """Exercise HTTP decoding, redirects, size boundaries, and failures."""
    assert download_text(f"{http_url}/text") == "Python python, дані! Дані — слова."
    assert download_text(f"{http_url}/redirect") == download_text(f"{http_url}/text")
    assert download_text(f"{http_url}/latin") == "café café"
    assert download_text(f"{http_url}/bom") == "слово"
    assert download_text(f"{http_url}/empty") == ""
    assert len(download_text(f"{http_url}/large", max_bytes=1024)) == 1024
    with pytest.raises(ValueError, match="ліміт"):
        download_text(f"{http_url}/large", max_bytes=1023)
    with pytest.raises(HTTPError):
        download_text(f"{http_url}/missing")
    with pytest.raises(UnicodeDecodeError):
        download_text(f"{http_url}/invalid")


def test_truncated_http_response_is_rejected(http_url: str) -> None:
    """Reject a response shorter than its advertised Content-Length."""
    with pytest.raises(OSError, match="обірвалася"):
        download_text(f"{http_url}/truncated")


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "ftp://example.org/file", "https:///missing", "text"]
)
def test_invalid_urls(url: str) -> None:
    """Reject malformed URLs and schemes other than HTTP or HTTPS."""
    with pytest.raises(ValueError, match="URL"):
        download_text(url)


@pytest.mark.parametrize(
    ("timeout", "limit"), [(0, 1), (float("inf"), 1), (float("nan"), 1), (1, 0)]
)
def test_download_limits(timeout: float, limit: int) -> None:
    """Reject invalid limits before making any HTTP request."""
    with pytest.raises(ValueError):
        download_text("http://localhost/text", timeout=timeout, max_bytes=limit)
