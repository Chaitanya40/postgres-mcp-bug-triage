from datetime import datetime, timezone
from decimal import Decimal

from triage_mcp.masking import mask_text, to_safe_json


def test_masks_emails_and_phone_numbers_in_free_text():
    text = "Contact maya.lindqvist@example.com or +46 70 123 45 67 about invoice 2026-09-03"
    assert mask_text(text) == "Contact m***@example.com or [phone] about invoice 2026-09-03"


def test_masks_nested_json_and_converts_types():
    row = {
        "payload": {"billing_email": "customer7@example.com", "attempts": [1, 2]},
        "created_at": datetime(2026, 9, 3, 2, 0, tzinfo=timezone.utc),
        "amount": Decimal("29.00"),
    }
    assert to_safe_json(row) == {
        "payload": {"billing_email": "c***@example.com", "attempts": [1, 2]},
        "created_at": "2026-09-03T02:00:00+00:00",
        "amount": 29.0,
    }


def test_truncates_long_text():
    assert mask_text("x" * 1000).endswith("...[truncated]")
