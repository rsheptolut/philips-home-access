"""Offline tests for token decoding / expiry."""
import base64
import json
import time

from homeaccess import tokens


def _make_token(claims: dict) -> str:
    raw = json.dumps(claims).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def test_decode_token_reads_claims():
    tok = _make_token({"uid": "abc", "sub": "x@y.com", "exp": 123})
    claims = tokens.decode_token(tok)
    assert claims["uid"] == "abc"
    assert claims["sub"] == "x@y.com"


def test_is_expired_respects_expiry_and_margin():
    fresh = _make_token({"exp": int(time.time()) + 3600})
    expired = _make_token({"exp": int(time.time()) - 10})
    assert not tokens.is_expired(fresh)
    assert tokens.is_expired(expired)
    assert tokens.is_expired(None)               # missing -> expired
    # within the safety margin -> treated as expired
    soon = _make_token({"exp": int(time.time()) + 30})
    assert tokens.is_expired(soon, margin=60)
    # opaque/undecodable token -> NOT provably expired (don't relogin)
    assert not tokens.is_expired("not-a-decodable-token")
