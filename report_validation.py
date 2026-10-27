"""Validate versioned CLI reports against a bundled, offline JSON Schema."""

import json
from datetime import datetime, timedelta
from functools import cache
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError

SCHEMA_PATH = Path(__file__).resolve().parent / "schemas/run-report-v1.schema.json"


class ReportValidationError(ValueError):
    """The report or its local schema violates the published contract."""


def _is_utc_datetime(value: object) -> bool:
    """Check actual calendar dates without optional format-checker dependencies."""
    return not isinstance(value, str) or datetime.fromisoformat(
        value
    ).utcoffset() == timedelta(0)


@cache
def _validator() -> Draft202012Validator:
    """Load and verify the bundled schema once per process; never fetch a URL."""
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        raise ReportValidationError(f"Некоректна схема: {error.message}") from error
    checker = FormatChecker(formats=())
    checker.checks("date-time", raises=ValueError)(_is_utc_datetime)
    return Draft202012Validator(schema, format_checker=checker)


def validate_report(report: object) -> None:
    """Reject malformed reports, including Python's non-JSON NaN and infinity."""
    try:
        json.dumps(report, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ReportValidationError(f"Некоректні JSON-дані: {error}") from error
    try:
        _validator().validate(report)
    except ValidationError as error:
        raise ReportValidationError(f"{error.json_path}: {error.message}") from error
