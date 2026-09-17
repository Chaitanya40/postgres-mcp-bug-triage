"""Last-line PII scrubbing for anything the server returns to the model."""

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

EMAIL_RE = re.compile(r"\b([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
PHONE_RE = re.compile(r"\+\d[\d\s().-]{7,}\d")
MAX_TEXT_CHARS = 300


def mask_text(value: str) -> str:
    value = EMAIL_RE.sub(r"\1***@\2", value)
    value = PHONE_RE.sub("[phone]", value)
    if len(value) > MAX_TEXT_CHARS:
        value = value[:MAX_TEXT_CHARS] + "...[truncated]"
    return value


def to_safe_json(value: Any) -> Any:
    """Convert a database value to JSON-friendly types, masking PII in every string."""
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, dict):
        return {key: to_safe_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_safe_json(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value
