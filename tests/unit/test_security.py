"""Unit tests for security helpers: HMAC, PII redaction, ARR gating, sanitization."""

from __future__ import annotations

from src.utils.security import (
    generate_hmac_signature,
    redact_arr,
    redact_pii,
    sanitize_input,
    verify_hmac_signature,
)


def test_hmac_generation_and_verification() -> None:
    body = '{"event_type":"case.escalated"}'
    secret = "super-secret-key"
    sig = generate_hmac_signature(body, secret)
    assert verify_hmac_signature(body, sig, secret) is True
    assert verify_hmac_signature(body, "0" * 64, secret) is False


def test_pii_redaction_preserves_non_pii_numbers() -> None:
    text = "Contact j.doe@acme.com or +1-555-234-5678 regarding ACC-007 and 1850000 calls."
    redacted = redact_pii(text)
    assert "[REDACTED_EMAIL]" in redacted
    assert "[REDACTED_PHONE]" in redacted
    assert "1850000" in redacted


def test_arr_redaction_by_scope() -> None:
    customer = {"id": "ACC-007", "name": "Meridian", "arr_usd": 480000.0}
    without_finance = redact_arr(customer, ["customers:read"])
    assert "arr_usd" not in without_finance

    with_finance = redact_arr(customer, ["customers:read", "finance:read"])
    assert with_finance["arr_usd"] == 480000.0


def test_sanitize_input_strips_xss_without_breaking_english() -> None:
    raw = "<script>alert(1)</script>Please update the config and delete old pods."
    cleaned = sanitize_input(raw)
    assert "<script>" not in cleaned
    assert "Please update the config" in cleaned
