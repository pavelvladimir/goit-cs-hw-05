"""A real local HTTP endpoint; tests never depend on a public website."""

import hashlib
import os
from collections.abc import Iterator
from copy import deepcopy
from http.client import HTTPResponse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, cast
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import pytest

os.environ.setdefault("MPLBACKEND", "Agg")
os.environ.setdefault(
    "MPLCONFIGDIR", str(Path(__file__).parents[1] / ".cache/matplotlib")
)


@pytest.fixture(autouse=True)
def local_http_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail accidental public-HTTP requests before they leave the test process."""
    import mapreduce_words

    def open_local(request: Request, *, timeout: float) -> HTTPResponse:
        """Allow initial HTTP requests only to the local test server."""
        assert urlsplit(request.full_url).hostname in {"127.0.0.1", "localhost"}
        return cast(HTTPResponse, urlopen(request, timeout=timeout))

    monkeypatch.setattr(mapreduce_words, "urlopen", open_local)


class TextHandler(BaseHTTPRequestHandler):
    """Serve fixed text, redirects, and malformed responses for tests."""

    def do_GET(self) -> None:
        """Return the fixture response selected by the request path."""
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/text")
            self.end_headers()
            return
        bodies = {
            "/text": ("Python python, дані! Дані — слова.".encode(), "utf-8"),
            "/latin": ("café café".encode("latin-1"), "iso-8859-1"),
            "/bom": ("слово".encode("utf-8-sig"), None),
            "/empty": (b"", "utf-8"),
            "/invalid": (b"\xff\xfe", "utf-8"),
            "/large": (b"x" * 1024, "utf-8"),
            "/truncated": (b"partial", "utf-8"),
        }
        if self.path not in bodies:
            self.send_error(404)
            return
        body, charset = bodies[self.path]
        self.send_response(200)
        content_type = f"text/plain; charset={charset}" if charset else "text/plain"
        self.send_header("Content-Type", content_type)
        length = len(body) + 10 if self.path == "/truncated" else len(body)
        self.send_header("Content-Length", str(length))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """Suppress access logs during HTTP tests."""
        pass


@pytest.fixture(scope="session")
def http_url() -> Iterator[str]:
    """Yield a loopback server URL and stop its thread after the tests."""
    with ThreadingHTTPServer(("127.0.0.1", 0), TextHandler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join()


@pytest.fixture
def valid_sorter_report() -> dict[str, Any]:
    """Synthetic contract example; paths need not exist to validate a report."""
    return {
        "schema_version": 1,
        "task": "async_sorter",
        "started_at": "2026-10-27T00:00:00+00:00",
        "finished_at": "2026-10-27T00:00:01Z",
        "elapsed_seconds": 1.0,
        "status": "success",
        "exit_code": 0,
        "source": "/tmp/source",
        "output": "/tmp/output",
        "parameters": {"workers": 4, "chunk_bytes": 1048576},
        "statistics": {
            "copied": 1,
            "skipped": 0,
            "errors": 0,
            "bytes_copied": 8,
            "collisions": 0,
        },
    }


@pytest.fixture
def valid_word_report(valid_sorter_report: dict[str, Any]) -> dict[str, Any]:
    """Build a complete word report with matching hashes and counts."""
    report = deepcopy(valid_sorter_report)
    body = b"hello hello"
    digest = hashlib.sha256(body).hexdigest()
    report.update(
        task="mapreduce_words",
        source={
            "requested_url": "http://localhost/text",
            "final_url": "http://localhost/text",
            "charset": "utf-8",
            "bytes_downloaded": len(body),
            "sha256_bytes": digest,
            "sha256_text_utf8": digest,
        },
        output="/tmp/top.png",
        parameters={
            "workers": 4,
            "chunk_size": 2000,
            "top": 10,
            "timeout": 20.0,
            "max_bytes": 1024,
        },
        statistics={"total_words": 2, "unique_words": 1, "errors": 0},
        frequencies={"hello": 2},
        top_words=[{"word": "hello", "count": 2}],
    )
    return report
