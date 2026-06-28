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


def is_expired(token: str | None, margin: float = 60) -> bool:
    """True only if we can prove the token is expired (or about to be).

    A missing token counts as expired. A token we can't decode (no readable
    `exp`) is treated as NOT expired: re-logging in wouldn't help, since some
    datacenters hand back an opaque token in a format we can't parse (see
    research/FINDINGS.md). Proactively re-authing on those would re-login on
    every poll; instead we use the token as-is and let the server reject it
    with a 444, which the transport handles with a one-shot reauth + retry.
    """
    if not token:
        return True
    exp = token_exp(token)
    if exp is None:
        return False
    return (exp - time.time()) <= margin
