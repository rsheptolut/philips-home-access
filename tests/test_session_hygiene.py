"""Session hygiene under the cloud's one-session-per-account rule.

Every login invalidates the previous one's tokens (verified live). So re-logins
must be serialized and shared, background ones rate-limited, and a displaced
session reported -- otherwise concurrent 444s each log in and kill each other's
fresh tokens, and HA and another client sign each other out in a loop.
"""
import asyncio
import base64
import json
import time

import pytest

from homeaccess import session as session_mod
from homeaccess import state
from homeaccess.exceptions import HomeAccessConnectionError
from homeaccess.models import TokenSet
from homeaccess.session import Account
from homeaccess.settings import Settings
from homeaccess.transport import HttpClient


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)


def _token(ttl=3600, tag="x"):
    return base64.urlsafe_b64encode(json.dumps(
        {"uid": "U1", "exp": int(time.time()) + ttl, "t": tag}).encode()
    ).decode().rstrip("=")


OLD, NEW = _token(tag="old"), _token(tag="new")


class _Resp:
    def __init__(self, data):
        self._data, self.status = data, 200

    async def __aenter__(self):
        await asyncio.sleep(0)  # a real round-trip yields: requests overlap
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        return self._data

    async def text(self, errors="strict"):
        return json.dumps(self._data)


class _Cloud:
    """Single-session cloud: only the token from the latest login is live."""

    def __init__(self):
        self.logins = 0
        self.live = None   # someone else has signed in: OLD is dead

    def post(self, url, **kw):
        return self.request("POST", url, **kw)

    def request(self, method, url, headers=None, **kw):
        if "oauth/login" in url:
            self.logins += 1
            self.live = NEW
            return _Resp({"code": 200, "data": {"users": [
                {"uid": "U1", "token": NEW, "code": "PhilipsNorthAmerica"}]}})
        if (headers or {}).get("token") == self.live:
            return _Resp({"code": 200, "msg": "ok"})
        return _Resp({"code": "444", "msg": "Not logged in"})


def _account(cloud):
    acct = Account(Settings(identifier="a@b.com", credential="pw"), cloud)
    acct.tokenset = TokenSet("U1", {"PhilipsNorthAmerica": OLD})
    return acct


def _client(acct, cloud):
    return HttpClient(
        "https://x", session=cloud, reauth=acct.async_relogin,
        token_provider=lambda: acct.async_token_for("PhilipsNorthAmerica"))


async def test_concurrent_rejections_share_one_login():
    cloud = _Cloud()
    acct = _account(cloud)
    http = _client(acct, cloud)
    out = await asyncio.gather(*(http.post("/p") for _ in range(3)))
    assert [o["code"] for o in out] == [200, 200, 200]
    assert cloud.logins == 1, "each 444 logged in again, killing the others' tokens"


async def test_background_relogin_is_rate_limited():
    cloud = _Cloud()
    acct = _account(cloud)
    acct._last_login_attempt = time.monotonic()  # just logged in
    with pytest.raises(HomeAccessConnectionError, match="rate-limited"):
        await _client(acct, cloud).post("/p")
    assert cloud.logins == 0


async def test_user_command_bypasses_the_rate_limit():
    cloud = _Cloud()
    acct = _account(cloud)
    acct._last_login_attempt = time.monotonic()
    out = await _client(acct, cloud).post("/p", user_initiated=True)
    assert out["code"] == 200 and cloud.logins == 1


async def test_background_relogin_allowed_after_the_window(monkeypatch):
    cloud = _Cloud()
    acct = _account(cloud)
    acct._last_login_attempt = time.monotonic() - session_mod.RELOGIN_MIN_INTERVAL - 1
    assert (await _client(acct, cloud).post("/p"))["code"] == 200
    assert cloud.logins == 1


async def test_displaced_session_is_reported_once(caplog):
    cloud = _Cloud()
    acct = _account(cloud)  # OLD has hours left, so a 444 means displacement
    http = _client(acct, cloud)
    with caplog.at_level("WARNING"):
        await http.post("/p")
    assert caplog.text.count("Another client signed in") == 1


async def test_expired_token_rejection_is_not_called_displacement(caplog):
    cloud = _Cloud()
    acct = _account(cloud)
    stale = _token(ttl=-10, tag="stale")
    with caplog.at_level("WARNING"):
        await acct.async_relogin(stale)
    assert "Another client" not in caplog.text


async def test_relogin_for_an_already_replaced_token_is_a_no_op():
    cloud = _Cloud()
    acct = _account(cloud)
    acct.tokenset = TokenSet("U1", {"PhilipsNorthAmerica": NEW})
    await acct.async_relogin(OLD)
    assert cloud.logins == 0


async def test_state_file_can_be_cleared(tmp_path):
    # what async_remove_entry does when the HA config entry is deleted
    await state.async_save("a@b.com", {"tokenset": {"uid": "U1"}})
    assert list(tmp_path.iterdir())
    await state.async_clear("a@b.com")
    assert not list(tmp_path.iterdir())
