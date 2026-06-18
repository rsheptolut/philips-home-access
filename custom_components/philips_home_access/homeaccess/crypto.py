"""Request signing and lock-command encryption.

Reverse-engineered from the app (DEX class ld5 / Hermes bundle):
  - sign256: RSASSA-PKCS1-v1.5 over SHA-256 of compact, key-sorted JSON, base64.
    Signed with the static embedded app private key. Verified against every
    signed request in the capture (21/21).
  - encrypt_for_server: RSA/ECB/PKCS1 (117-byte blocks) with the embedded server
    public key, base64. Used for open/close command bodies. Randomized padding,
    so output differs each call; the server still decrypts it.
"""
from __future__ import annotations

import base64
import json
import time
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from . import constants

_KEY: rsa.RSAPrivateKey = serialization.load_der_private_key(
    base64.b64decode(constants.APP_PRIVATE_KEY_DER_B64), password=None)
_PUB_B = serialization.load_der_public_key(
    base64.b64decode(constants.SERVER_PUB_B_B64))


def now_ms() -> str:
    """reqTime as the app sends it: millisecond epoch, as a STRING."""
    return str(int(time.time() * 1000))


def sign256(payload: dict[str, Any]) -> str:
    """RSA-PKCS1v1.5/SHA-256 signature of compact, key-sorted JSON, base64."""
    message = json.dumps(payload, separators=(",", ":"),
                         ensure_ascii=False, sort_keys=True)
    sig = _KEY.sign(message.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(sig).decode("ascii")


def signed_body(params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return {**params, sign, reqTime} for any signed endpoint.

    The server re-sorts to verify, so body field order is irrelevant.
    """
    params = dict(params or {})
    req_time = now_ms()
    sign = sign256({**params, "reqTime": req_time})
    return {**params, "sign": sign, "reqTime": req_time}


def encrypt_for_server(plaintext: str) -> str:
    """RSA/ECB/PKCS1 encrypt with server key B, block-wise, base64."""
    data = plaintext.encode("utf-8")
    block = _PUB_B.key_size // 8 - 11  # 117 for RSA-1024
    out = b"".join(
        _PUB_B.encrypt(data[i:i + block], padding.PKCS1v15())
        for i in range(0, len(data), block)
    )
    return base64.b64encode(out).decode("ascii")


def encrypted_command_body(params: dict[str, Any]) -> dict[str, Any]:
    """Sign `params`, encrypt the JSON, and wrap as {"encryptData": ...}."""
    signed = signed_body(params)
    plaintext = json.dumps(signed, separators=(",", ":"), ensure_ascii=False)
    return {"encryptData": encrypt_for_server(plaintext)}
