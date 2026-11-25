"""Security utilities — HMAC, hashing, sanitisation, PII redaction.

All cryptographic operations use the standard-library :mod:`hmac` and
:mod:`hashlib` modules with constant-time comparison to avoid timing
side-channels.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import re
from typing import Any


# ---------------------------------------------------------------------------
# HMAC-SHA256 helpers
# ---------------------------------------------------------------------------

def generate_hmac_signature(payload: str | bytes, secret: str | bytes) -> str:
    """Compute an HMAC-SHA256 hex digest for *payload*.

    Parameters
    ----------
    payload:
        The raw request body (string or bytes).
    secret:
        Shared secret between the API and the webhook consumer.

    Returns
    -------
    str
        Lower-case hex digest.
    """
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    if isinstance(secret, str):
        secret = secret.encode("utf-8")

    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def verify_hmac_signature(
    payload: str | bytes,
    signature: str,
    secret: str | bytes,
) -> bool:
    """Verify an HMAC-SHA256 signature using constant-time comparison.

    Parameters
    ----------
    payload:
        The raw request body.
    signature:
        The hex digest supplied by the caller (e.g. ``X-Signature`` header).
    secret:
        Shared HMAC secret.

    Returns
    -------
    bool
        ``True`` if the signature is valid.
    """
    expected = generate_hmac_signature(payload, secret)
    return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------------------
# Idempotency key hashing
# ---------------------------------------------------------------------------

def hash_idempotency_key(key: str) -> str:
    """Return a SHA-256 hex digest of *key*.

    Hashing the client-provided idempotency key before storage prevents
    leaking potentially sensitive tokens and normalises key length.
    """
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Input sanitisation
# ---------------------------------------------------------------------------

# Patterns for common dangerous content.
_SCRIPT_TAG_RE = re.compile(r"<\s*script[^>]*>.*?<\s*/\s*script\s*>", re.IGNORECASE | re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_SQL_INJECTION_RE = re.compile(
    r"(;\s*(DROP|ALTER|INSERT|DELETE|UPDATE|EXEC|UNION)\b|\bUNION\s+SELECT\b|(--\s*$))",
    re.IGNORECASE,
)


def sanitize_input(text: str) -> str:
    """Strip potentially dangerous content from user-supplied text.

    The function:

    1. HTML-escapes the input (neutralises ``<script>`` etc.).
    2. Strips residual HTML tags.
    3. Removes common SQL-injection markers.
    4. Collapses excessive whitespace.

    .. note::

       This is a *defence-in-depth* measure.  Parameterised queries and
       output encoding remain the primary injection defences.
    """
    # Step 1 — escape HTML entities.
    text = html.escape(text, quote=True)
    # Step 2 — remove any surviving tags (belt & suspenders).
    text = _HTML_TAG_RE.sub("", text)
    # Step 3 — strip SQL-injection tokens.
    text = _SQL_INJECTION_RE.sub("", text)
    # Step 4 — normalise whitespace.
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ---------------------------------------------------------------------------
# PII redaction
# ---------------------------------------------------------------------------

# Matches common email patterns.
_EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
)

# Matches phone numbers with separators, country codes, or parenthesized area codes
# (avoids false positives on numeric IDs or large integer counters).
_PHONE_RE = re.compile(
    r"(?:\+\d{1,3}[-.\s]?)?"
    r"(?:\(\d{2,4}\)[-.\s]?|\b\d{3}[-.\s])"
    r"\d{3,4}[-.\s]\d{3,4}\b"
)

_PII_PLACEHOLDER_EMAIL = "[REDACTED_EMAIL]"
_PII_PLACEHOLDER_PHONE = "[REDACTED_PHONE]"


def redact_pii(text: str) -> str:
    """Replace emails and phone numbers with redaction placeholders.

    Returns
    -------
    str
        A copy of *text* with PII tokens replaced.
    """
    text = _EMAIL_RE.sub(_PII_PLACEHOLDER_EMAIL, text)
    text = _PHONE_RE.sub(_PII_PLACEHOLDER_PHONE, text)
    return text


# ---------------------------------------------------------------------------
# Scope-based field redaction
# ---------------------------------------------------------------------------

def redact_arr(customer_dict: dict[str, Any], scopes: set[str] | list[str]) -> dict[str, Any]:
    """Remove ``arr_usd`` from *customer_dict* unless the caller has the
    ``finance:read`` scope.

    Parameters
    ----------
    customer_dict:
        A dictionary representation of a :class:`CustomerSummary`.
    scopes:
        OAuth scopes granted to the current user.

    Returns
    -------
    dict
        A shallow copy with ``arr_usd`` removed when unauthorised.
    """
    scopes_set = set(scopes) if not isinstance(scopes, set) else scopes
    result = dict(customer_dict)  # shallow copy — avoid mutating the caller's dict
    if "finance:read" not in scopes_set:
        result.pop("arr_usd", None)
    return result
