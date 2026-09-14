"""Signature verification — the security boundary, tested in isolation."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from app.webhooks.verify import compute_signature, verify_signature

SECRET = "s3cr3t-value"
BODY = b'{"action":"opened","issue":{"number":42}}'


def test_compute_signature_matches_reference_implementation():
    expected = "sha256=" + hmac.new(SECRET.encode(), BODY, hashlib.sha256).hexdigest()
    assert compute_signature(SECRET, BODY) == expected


def test_valid_signature_accepted():
    assert verify_signature(SECRET, BODY, compute_signature(SECRET, BODY)) is True


def test_secret_accepted_as_bytes_or_str():
    signature = compute_signature(SECRET, BODY)
    assert verify_signature(SECRET.encode(), BODY, signature) is True


def test_tampered_body_rejected():
    """The classic attack: valid signature, body changed in flight."""
    signature = compute_signature(SECRET, BODY)
    assert verify_signature(SECRET, BODY + b" ", signature) is False


def test_reserialised_body_rejected():
    """Guards the 'must hash raw bytes' rule.

    Re-encoding the parsed JSON changes whitespace, so a handler that hashes
    a re-serialised body would silently reject every real delivery. This test
    documents why the route reads `await request.body()`.
    """
    reserialised = b'{"action": "opened", "issue": {"number": 42}}'
    assert reserialised != BODY
    assert verify_signature(SECRET, reserialised, compute_signature(SECRET, BODY)) is False


def test_wrong_secret_rejected():
    assert verify_signature("different-secret", BODY, compute_signature(SECRET, BODY)) is False


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "sha1=aaaabbbbccccddddeeeeffff0000111122223333",  # deprecated algorithm
        "aaaabbbbccccdddd",  # no prefix
        "sha256=",  # empty digest
        "sha256=not-hex",
        "sha256=" + "a" * 63,  # one nibble short
        "Bearer sometoken",
    ],
)
def test_malformed_headers_rejected(header):
    assert verify_signature(SECRET, BODY, header) is False


def test_empty_body_still_verifiable():
    """A zero-length body has a well-defined HMAC and must not crash."""
    assert verify_signature(SECRET, b"", compute_signature(SECRET, b"")) is True


def test_surrounding_whitespace_tolerated():
    signature = compute_signature(SECRET, BODY)
    assert verify_signature(SECRET, BODY, f"  {signature}  ".replace("  sha256", "sha256")) is True


def test_case_difference_in_digest_rejected():
    """compare_digest is exact; GitHub always sends lowercase hex."""
    signature = compute_signature(SECRET, BODY)
    upper = "sha256=" + signature.split("=", 1)[1].upper()
    assert verify_signature(SECRET, BODY, upper) is False
