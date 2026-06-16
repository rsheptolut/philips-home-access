"""Helpers for the opaque account token (a base64url JSON claims blob)."""
from __future__ import annotations

import base64
import json
import time
from typing import Any


def decode_token(token: str) -> dict[str, Any]:
    """Decode the token's claims (uid, sub, exp, nbf, iat, ...).

    The token is a base64url-encoded JSON object (not a signed JWT); we take the
    first dot-segment defensively in case a signature is ever appended.
    """
    seg = token.split(".")[0]
    seg += "=" * (-len(seg) % 4)
    return json.loads(base64.urlsafe_b64decode(seg))


def token_exp(token: str) -> int | None:
    try:
        return decode_token(token).get("exp")
    except Exception:  # noqa: BLE001
        return None


def seconds_left(token: str) -> float:
    exp = token_exp(token)
    return (exp - time.time()) if exp else -1.0


def is_valid(token: str | None, margin: float = 60) -> bool:
    """True if the token exists and won't expire within `margin` seconds."""
    return bool(token) and seconds_left(token) > margin
