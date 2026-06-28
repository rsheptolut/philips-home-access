"""Async I/O tests with a faked aiohttp session (no network)."""
import asyncio
import base64
import json
import threading
import time

import pytest

from homeaccess import HomeAccess, state
from homeaccess.exceptions import AuthError
from homeaccess.models import TokenSet
from homeaccess.realtime import Realtime
from homeaccess.session import Account
from homeaccess.settings import Settings
from homeaccess.transport import HttpClient


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)


# --- fake aiohttp pieces ---------------------------------------------------
class _Resp:
    def __init__(self, data):
        self._data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        return self._data


class _Session:
    """Returns queued JSON bodies for post()/request() in order."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return _Resp(self._responses.pop(0))

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return _Resp(self._responses.pop(0))


def _settings():
    return Settings(identifier="a@b.com", credential="pw")


async def test_async_login_builds_tokenset():
    resp = {"code": 200, "data": {"users": [
        {"uid": "U1", "token": "tNA", "code": "PhilipsNorthAmerica"},
        {"uid": "U1", "token": "tSG", "code": "PhilipsSingapore"}]}}
    acct = Account(_settings(), _Session([resp]))
    ts = await acct.async_login()
    assert ts.uid == "U1"
    assert ts.token_for("PhilipsNorthAmerica") == "tNA"


async def test_state_io_runs_off_the_event_loop(monkeypatch):
    """Regression: state.load/save must never run on the event-loop thread.

    Home Assistant's watchdog flags blocking disk I/O on the loop; the state
    helpers offload to a worker thread, so a full login + discover (which reads
    the device cache, persists the tokenset, and rewrites the cache) must do all
    its file I/O off-loop.
    """
    loop_thread = threading.get_ident()
    on_loop: list = []
    real_load, real_save = state.load, state.save

    def spy_load(identifier):
        if threading.get_ident() == loop_thread:
            on_loop.append(("load", identifier))
        return real_load(identifier)

    def spy_save(identifier, data):
        if threading.get_ident() == loop_thread:
            on_loop.append(("save", identifier))
        return real_save(identifier, data)

    monkeypatch.setattr(state, "load", spy_load)
    monkeypatch.setattr(state, "save", spy_save)

    # A valid-shaped token (future exp) so async_token_for doesn't re-login and
    # consume the device-list response.
    token = base64.urlsafe_b64encode(
        json.dumps({"uid": "U1", "exp": int(time.time()) + 3600}).encode()
    ).decode().rstrip("=")
    login = {"code": 200, "data": {"users": [
        {"uid": "U1", "token": token, "code": "PhilipsNorthAmerica"}]}}
    devices = {"code": 200, "data": {"wifiList": []}}
    ha = HomeAccess(_settings(), session=_Session([login, devices]))
    await ha.async_discover()

    assert on_loop == [], f"blocking state I/O on the event loop: {on_loop}"


async def test_async_login_bad_credentials_raises():
    acct = Account(_settings(), _Session([{"code": "444", "msg": "Not logged in"}]))
    with pytest.raises(AuthError):
        await acct.async_login()


async def test_transport_reauths_once_on_444():
    reauths = []

    async def token_provider():
        return "tok"

    async def reauth():
        reauths.append(1)

    sess = _Session([{"code": "444", "msg": "Not logged in"}, {"code": 200, "msg": "ok"}])
    http = HttpClient("https://x", token_provider=token_provider, reauth=reauth, session=sess)
    out = await http.post_signed("/p", {"esn": "RL"})
    assert out["code"] == 200 and reauths == [1]


# --- WebSocket -------------------------------------------------------------
class _Msg:
    def __init__(self, data):
        import aiohttp
        self.type = aiohttp.WSMsgType.TEXT
        self.data = data


class _WS:
    def __init__(self, messages):
        self._messages = messages

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __aiter__(self):
        self._it = iter(self._messages)
        return self

    async def __anext__(self):
        await asyncio.sleep(0)  # yield control so the loop can process cancel
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class _WSSession(_Session):
    def __init__(self, messages):
        super().__init__([])
        self._ws = _WS(messages)

    def ws_connect(self, url, **kw):
        return self._ws


async def test_ws_listen_parses_events():
    frame = json.dumps({"func": "setLock", "timestamp": "1",
                        "body": {"wfId": "RL", "params": {"dooropt": 1}}})
    # a valid-shaped token (future exp) so async_token_for doesn't try to log in
    claims = base64.urlsafe_b64encode(
        json.dumps({"uid": "U1", "exp": int(time.time()) + 3600}).encode()
    ).decode().rstrip("=")
    sess = _WSSession([_Msg(frame)])
    acct = Account(_settings(), sess)
    acct.tokenset = TokenSet("U1", {"PhilipsNorthAmerica": claims})
    rt = Realtime(acct, sess, "PhilipsNorthAmerica")

    got = []
    task = asyncio.create_task(rt.listen(on_event=got.append))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert any(e.kind == "setLock" and e.state == "unlocked" for e in got)
