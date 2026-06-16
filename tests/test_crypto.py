"""Offline tests for signing + lock-command encryption (no network/captures)."""
import base64
import json

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from homeaccess import crypto


def test_sign256_roundtrip_verifies():
    payload = {"reqTime": "1700000000000", "esn": "RLTEST"}
    sig = crypto.sign256(payload)
    msg = json.dumps(payload, separators=(",", ":"),
                     ensure_ascii=False, sort_keys=True).encode()
    # Must verify against the public half of the embedded signing key.
    crypto._KEY.public_key().verify(
        base64.b64decode(sig), msg, padding.PKCS1v15(), hashes.SHA256())


def test_sign256_is_deterministic_and_key_order_independent():
    a = crypto.sign256({"a": 1, "b": 2})
    b = crypto.sign256({"b": 2, "a": 1})
    assert a == b  # canonicalized by sort_keys


def test_signed_body_shape():
    body = crypto.signed_body({"esn": "RLTEST"})
    assert set(body) == {"esn", "sign", "reqTime"}
    assert isinstance(body["reqTime"], str)


def test_encrypted_command_body_block_aligned():
    body = crypto.encrypted_command_body({"esn": "RL21243710207", "userNumberId": 0})
    raw = base64.b64decode(body["encryptData"])
    # RSA-1024 -> ciphertext is a whole number of 128-byte blocks (capture = 384).
    assert raw and len(raw) % 128 == 0
