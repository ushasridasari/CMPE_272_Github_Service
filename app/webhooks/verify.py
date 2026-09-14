"""Webhooks and security - Done by Ushasri Dasari

Webhook signature verification.

Deliberately dependency-free and framework-free so it can be unit tested in
isolation — this is the security boundary of the service.

Two properties matter:

* The digest is computed over the **raw** request bytes.  Re-serialising the
  parsed JSON would change whitespace and key order and break the digest.
* The comparison is constant time, so an attacker cannot learn the expected
  signature one byte at a time by measuring response latency.
"""

from __future__ import annotations

import hashlib
import hmac

SIGNATURE_HEADER = "X-Hub-Signature-256"
DELIVERY_HEADER = "X-GitHub-Delivery"
EVENT_HEADER = "X-GitHub-Event"
_PREFIX = "sha256="


def compute_signature(secret: str | bytes, body: bytes) -> str:
    """Return the ``sha256=<hexdigest>`` signature GitHub would send."""
    key = secret.encode("utf-8") if isinstance(secret, str) else secret
    digest = hmac.new(key, body, hashlib.sha256).hexdigest()
    return f"{_PREFIX}{digest}"


def verify_signature(secret: str | bytes, body: bytes, signature_header: str | None) -> bool:
    """Constant-time check of GitHub's ``X-Hub-Signature-256`` header.

    Returns ``False`` — never raises — for a missing header, a header with
    the wrong algorithm prefix, a malformed digest, or a tampered body.
    """
    if not signature_header:
        return False
    if not signature_header.startswith(_PREFIX):
        return False

    expected = compute_signature(secret, body)
    # hmac.compare_digest requires both operands to be the same string type
    # and rejects non-ASCII, so normalise defensively.
    try:
        return hmac.compare_digest(expected, signature_header.strip())
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return False
