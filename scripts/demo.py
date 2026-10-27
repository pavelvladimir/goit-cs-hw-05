"""Demonstrate both CLIs and their reports using files and a local HTTP server."""

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

from async_sorter import main as sorter_main
from mapreduce_words import main as words_main

ROOT = Path(__file__).resolve().parents[1]


class QuietHandler(SimpleHTTPRequestHandler):
    """Keep demo output focused on the homework results."""

    def log_message(self, format: str, *args: object) -> None:
        pass


def main() -> None:
    """Run the real CLIs; leave their JSON reports and chart in artifacts/demo."""
    artifacts = ROOT / "artifacts/demo"
    with TemporaryDirectory(prefix="hw05-demo-") as temporary:
        output = Path(temporary) / "sorted"
        code = sorter_main(
            [
                str(ROOT / "examples/source"),
                str(output),
                "--report",
                str(artifacts / "sorter.json"),
            ]
        )
        if code:
            raise RuntimeError("Демонстраційне копіювання завершилося з помилками.")
        for path in sorted(output.rglob("*")):
            if path.is_file():
                print(f"  {path.relative_to(output)}")
    handler = partial(QuietHandler, directory=str(ROOT / "examples"))
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            code = words_main(
                [
                    f"http://127.0.0.1:{server.server_port}/text.txt",
                    "--chunk-size",
                    "5",
                    "--output",
                    str(artifacts / "top_words.png"),
                    "--report",
                    str(artifacts / "words.json"),
                ]
            )
            if code:
                raise RuntimeError("Демонстраційний аналіз завершився з помилками.")
        finally:
            server.shutdown()
            thread.join()
    print("Звіти: artifacts/demo/sorter.json, artifacts/demo/words.json")


if __name__ == "__main__":
    main()
