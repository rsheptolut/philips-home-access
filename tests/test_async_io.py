"""Async I/O tests with a faked aiohttp session (no network)."""
import asyncio
import base64
import json
import threading
import time

import pytest

from homeaccess import HomeAccess, state, tokens
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


def _decodable_token(ttl=7200):
    """A base64url-JSON session token (the PhilipsNorthAmerica format)."""
    return base64.urlsafe_b64encode(
        json.dumps({"uid": "U1", "exp": int(time.time()) + ttl}).encode()
    ).decode().rstrip("=")


def _login_3dc():
    """Login response for a 3-datacenter account (Oneness/Singapore opaque, NA real)."""
    return {"code": 200, "data": {"users": [
        {"uid": "U1", "token": "opaque-oneness", "code": "PhilipsOneness"},
        {"uid": "U1", "token": "opaque-singapore", "code": "PhilipsSingapore"},
        {"uid": "U1", "token": _decodable_token(), "code": "PhilipsNorthAmerica"}]}}


def _devlist(esn=None):
    """A device/list body: one lock (no dataCenter -> homed at queried DC) or none."""
    wifi = [] if esn is None else [{"wifiSN": esn, "userNumberId": 0}]
    return {"code": 200, "data": {"wifiList": wifi}}


async def test_async_login_builds_tokenset():
    resp = {"code": 200, "data": {"users": [
        {"uid": "U1", "token": "tNA", "code": "PhilipsNorthAmerica"},
        {"uid": "U1", "token": "tSG", "code": "PhilipsSingapore"}]}}
    acct = Account(_settings(), _Session([resp]))
    ts = await acct.async_login()
    assert ts.uid == "U1"
    assert ts.token_for("PhilipsNorthAmerica") == "tNA"


def test_is_expired_only_when_provable():
    """Undecodable/opaque tokens are NOT treated as expired (no futile relogin).

    Missing -> expired; decodable-and-past -> expired; decodable-and-future ->
    not expired; opaque (no readable exp) -> not expired.
    """
    assert tokens.is_expired(None) is True
    assert tokens.is_expired(_decodable_token(ttl=-10)) is True
    assert tokens.is_expired(_decodable_token(ttl=7200)) is False
    assert tokens.is_expired("opaque~not~base64url~json") is False


async def test_discover_does_not_relogin_for_opaque_tokens():
    """Regression: opaque datacenter tokens must not cause a per-poll relogin.

    Reproduces the real account (Oneness/Singapore/NorthAmerica): two opaque
    tokens + one decodable NA token. A discover must log in exactly once (the
    initial login) and still query every datacenter -- no relogin storm, and no
    datacenter dropped (so the lock can be found wherever its list lives).
    """
    sess = _Session([_login_3dc(), _devlist(), _devlist(), _devlist()])
    ha = HomeAccess(_settings(), session=sess)
    await ha.async_discover()

    logins = [c for c in sess.calls if "oauth/login" in c[1]]
    dlist = [c for c in sess.calls if "device/list" in c[1]]
    assert len(logins) == 1, f"expected 1 login (no relogin storm), got {len(logins)}"
    assert len(dlist) == 3, f"expected all 3 datacenters queried, got {len(dlist)}"


async def test_discover_pins_to_productive_datacenter():
    """After a find, subsequent discovers poll only the datacenter(s) with locks."""
    # 1st discover scans all 3 (Oneness/Singapore empty, NA has the lock).
    sess = _Session([_login_3dc(), _devlist(), _devlist(), _devlist("RL1"),
                     _devlist("RL1")])  # 2nd discover: NA only
    ha = HomeAccess(_settings(), session=sess)

    locks = await ha.async_discover()
    assert [l.esn for l in locks] == ["RL1"]
    assert ha._active_codes == ["PhilipsNorthAmerica"]
    assert len([c for c in sess.calls if "device/list" in c[1]]) == 3

    sess.calls.clear()
    locks = await ha.async_discover()
    assert [l.esn for l in locks] == ["RL1"]
    dlist = [c for c in sess.calls if "device/list" in c[1]]
    assert len(dlist) == 1, f"expected NA-only poll, got {len(dlist)}"
    assert "idlespacetech" in dlist[0][1]
    assert not [c for c in sess.calls if "oauth/login" in c[1]]  # no relogin


async def test_discover_rescans_when_pinned_datacenter_goes_dry():
    """If the pinned datacenter returns nothing, fall back to scanning all."""
    sess = _Session([_login_3dc(),
                     _devlist(), _devlist(), _devlist("RL1"),   # 1st: pin NA
                     _devlist(),                                # 2nd: NA dry
                     _devlist(), _devlist(), _devlist("RL1")])  # 2nd: re-scan all
    ha = HomeAccess(_settings(), session=sess)

    await ha.async_discover()
    assert ha._active_codes == ["PhilipsNorthAmerica"]

    sess.calls.clear()
    locks = await ha.async_discover()
    assert [l.esn for l in locks] == ["RL1"]  # recovered via re-scan
    dlist = [c for c in sess.calls if "device/list" in c[1]]
    assert len(dlist) == 4, f"expected 1 (dry NA) + 3 (re-scan), got {len(dlist)}"
    assert ha._active_codes == ["PhilipsNorthAmerica"]  # re-pinned


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
