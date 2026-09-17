"""Signature verification for update manifests.

The update endpoint returns a small signed envelope.  Its ``payload`` field is
base64url-encoded JSON, and the RS256 signature covers those decoded bytes
exactly.  Verification intentionally uses only the standard library so the
same code works in the Windows bundle and in Chaquopy on Android.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from pathlib import Path


PUBLIC_KEY_FILE = Path(__file__).with_name("update_public_key.json")
_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


class UpdateSignatureError(ValueError):
    """Raised when an update manifest is malformed or untrusted."""


def _b64url_decode(value: str) -> bytes:
    if not isinstance(value, str) or not value or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise UpdateSignatureError("missing base64url value")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:
        raise UpdateSignatureError("invalid base64url value") from exc


def _load_public_key(path: Path = PUBLIC_KEY_FILE) -> dict:
    try:
        key = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise UpdateSignatureError("update public key is unavailable") from exc
    if not isinstance(key, dict):
        raise UpdateSignatureError("invalid update public key")
    return key


def _verify_rs256(message: bytes, signature: bytes, modulus: int, exponent: int) -> bool:
    """Verify RSASSA-PKCS1-v1_5 with SHA-256 (RFC 8017)."""
    key_bytes = (modulus.bit_length() + 7) // 8
    if len(signature) != key_bytes or key_bytes < len(_SHA256_DIGEST_INFO) + 35:
        return False
    signature_number = int.from_bytes(signature, "big")
    if signature_number >= modulus:
        return False
    encoded = pow(signature_number, exponent, modulus).to_bytes(
        key_bytes, "big"
    )
    digest_info = _SHA256_DIGEST_INFO + hashlib.sha256(message).digest()
    expected = b"\x00\x01" + b"\xff" * (key_bytes - len(digest_info) - 3)
    expected += b"\x00" + digest_info
    return hmac.compare_digest(encoded, expected)


def verify_manifest_envelope(raw: bytes | str, public_key: dict | None = None) -> dict:
    """Verify a signed manifest envelope and return its decoded JSON payload."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    try:
        envelope = json.loads(raw)
    except Exception as exc:
        raise UpdateSignatureError("update manifest is not valid JSON") from exc
    if not isinstance(envelope, dict) or envelope.get("schema") != 1:
        raise UpdateSignatureError("unsupported update envelope schema")

    key = public_key or _load_public_key()
    if envelope.get("algorithm") != "RS256" or key.get("algorithm") != "RS256":
        raise UpdateSignatureError("unsupported update signature algorithm")
    if envelope.get("keyId") != key.get("keyId"):
        raise UpdateSignatureError("unknown update signing key")

    payload_bytes = _b64url_decode(envelope.get("payload"))
    signature = _b64url_decode(envelope.get("signature"))
    modulus = int.from_bytes(_b64url_decode(key.get("modulus")), "big")
    exponent = int.from_bytes(_b64url_decode(key.get("exponent")), "big")
    if not _verify_rs256(payload_bytes, signature, modulus, exponent):
        raise UpdateSignatureError("update manifest signature is invalid")

    try:
        payload = json.loads(payload_bytes)
    except Exception as exc:
        raise UpdateSignatureError("signed update payload is not valid JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema") != 1:
        raise UpdateSignatureError("unsupported signed payload schema")
    return payload
