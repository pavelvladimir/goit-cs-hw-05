"""Validate existing CLI reports locally without modifying their contents."""

import argparse
import json
import sys
from pathlib import Path

from report_validation import validate_report


def main(argv: list[str] | None = None) -> int:
    """Check every supplied file; return 1 if any report is invalid or unreadable."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "reports", type=Path, nargs="+", help="JSON-звіти для перевірки"
    )
    args = parser.parse_args(argv)
    failed = False
    for path in args.reports:
        try:
            validate_report(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as error:
            print(f"ERROR: {path}: {error}", file=sys.stderr)
            failed = True
        else:
            print(f"OK: {path}")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
