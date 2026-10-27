"""Run the complete offline check in Rocky and retain inspectable evidence."""

import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from importlib.metadata import distributions
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Any

from PIL import Image

from report_validation import validate_report

ROOT = Path(__file__).resolve().parents[1]


def source_hashes(root: Path) -> dict[str, str]:
    """Fingerprint code, configuration and fixtures, excluding narrative docs."""
    paths = [
        *root.glob("*.py"),
        *(
            root / name
            for name in ("Makefile", "pyproject.toml", "uv.lock", ".python-version")
        ),
    ]
    for directory in ("scripts", "tests", "schemas", "examples", "docker"):
        paths.extend(
            path
            for path in (root / directory).rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        )
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(paths)
    }


def verify_demo(folder: Path) -> dict[str, Any]:
    """Check independent expected counts, the exact downloaded bytes, and PNG."""
    sorter = json.loads((folder / "sorter.json").read_text(encoding="utf-8"))
    words = json.loads((folder / "words.json").read_text(encoding="utf-8"))
    for report in (sorter, words):
        validate_report(report)
        if report["status"] != "success":
            raise ValueError("Demo report does not describe a successful run.")
    expected = {
        "copied": 6,
        "skipped": 0,
        "errors": 0,
        "bytes_copied": 329,
        "collisions": 1,
    }
    if sorter["statistics"] != expected:
        raise ValueError("Unexpected demo copy statistics.")
    if words["statistics"] != {"total_words": 42, "unique_words": 29, "errors": 0}:
        raise ValueError("Unexpected demo word statistics.")
    if words["top_words"][:3] != [
        {"word": "data", "count": 4},
        {"word": "words", "count": 4},
        {"word": "python", "count": 3},
    ]:
        raise ValueError("Unexpected demo word ranking.")
    body = (ROOT / "examples/text.txt").read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    if words["source"]["sha256_bytes"] != digest:
        raise ValueError("Demo downloaded different text.")
    with Image.open(folder / "top_words.png") as chart:
        if chart.format != "PNG" or min(chart.size) <= 0:
            raise ValueError("Demo did not produce a PNG chart.")
        size = chart.size
        chart.verify()
    return {
        "sorter": sorter["statistics"],
        "words": words["statistics"],
        "sha256_bytes": digest,
        "png_size": list(size),
    }


def main() -> int:
    """Called only inside the test image; the host mounts a fresh /results."""
    release = platform.freedesktop_os_release()
    if release["ID"] != "rocky" or os.geteuid() == 0:
        raise RuntimeError("This check requires Rocky Linux and a non-root user.")
    # Some kernels also create DOWN tunnel interfaces in a fresh network namespace.
    interfaces = {
        name: int((Path("/sys/class/net") / name / "flags").read_text(), 16)
        for _, name in sorted(socket.if_nameindex())
    }
    if "lo" not in interfaces or any(
        flags & 1 for name, flags in interfaces.items() if name != "lo"
    ):
        raise RuntimeError("Run with --network none; only loopback may be UP.")
    results = Path("/results")
    evidence: dict[str, Any] = {
        "started_at": datetime.now(UTC).isoformat(),
        "status": "failed",
        "environment": {
            "os_release": release,
            "architecture": platform.machine(),
            "kernel": platform.release(),
            "libc": list(platform.libc_ver()),
            "python": sys.version,
            "uv": subprocess.check_output(["uv", "--version"], text=True).strip(),
            "make": subprocess.check_output(
                ["make", "--version"], text=True
            ).splitlines()[0],
            "uid": os.geteuid(),
            "gid": os.getegid(),
            "interfaces": interfaces,
            "image_id": os.environ["HW05_TEST_IMAGE_ID"],
            "packages": dict(
                sorted(
                    (item.metadata["Name"], item.version) for item in distributions()
                )
            ),
        },
        "source_sha256": source_hashes(ROOT),
        "steps": [],
    }
    code = 1
    with TemporaryDirectory(prefix="hw05-rocky-") as temporary:
        work = Path(temporary) / "project"
        shutil.copytree(ROOT, work)
        environment = {
            **os.environ,
            "COVERAGE_FILE": str(results / ".coverage"),
            "PYTEST_ADDOPTS": f"--junitxml={results / 'pytest.xml'}",
        }
        try:
            commands = [
                ["make", "check"],
                [
                    sys.executable,
                    "-m",
                    "coverage",
                    "json",
                    "-o",
                    str(results / "coverage.json"),
                ],
                ["make", "demo", "validate-reports"],
            ]
            for command in commands:
                print(f"Running: {' '.join(command)}", flush=True)
                started = perf_counter()
                completed = subprocess.run(
                    command, cwd=work, env=environment, check=False
                )
                evidence["steps"].append(
                    {
                        "command": command,
                        "exit_code": completed.returncode,
                        "elapsed_seconds": perf_counter() - started,
                    }
                )
                if completed.returncode:
                    raise RuntimeError(f"Command failed: {' '.join(command)}")
            suites = ET.parse(results / "pytest.xml").iter("testsuite")
            totals = dict.fromkeys(("tests", "failures", "errors", "skipped"), 0)
            for suite in suites:
                for field in totals:
                    totals[field] += int(suite.attrib[field])
            evidence["pytest"] = totals
            if not totals["tests"] or any(
                totals[key] for key in ("failures", "errors", "skipped")
            ):
                raise RuntimeError(
                    "Rocky requires a passing suite with no skipped tests."
                )
            coverage = json.loads((results / "coverage.json").read_text())
            evidence["coverage"] = coverage["totals"]
            evidence["demo"] = verify_demo(work / "artifacts/demo")
            evidence["status"] = "success"
            code = 0
        except Exception as error:
            evidence["error"] = str(error)
            print(f"Rocky check failed: {error}", file=sys.stderr)
        finally:
            for name in ("htmlcov", "coverage.xml", "artifacts/demo"):
                source = work / name
                destination = results / source.name
                if source.is_dir():
                    shutil.copytree(source, destination)
                elif source.is_file():
                    shutil.copy2(source, destination)
            evidence["finished_at"] = datetime.now(UTC).isoformat()
            evidence["exit_code"] = code
            (results / "results.json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
