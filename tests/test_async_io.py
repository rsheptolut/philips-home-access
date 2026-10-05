"""Async I/O tests with a faked aiohttp session (no network)."""
import asyncio
import base64
import json
import threading
import time

import pytest

from homeaccess import HomeAccess, state, tokens
from homeaccess.exceptions import (
    AuthError,
    CommandError,
    HomeAccessConnectionError,
    HomeAccessResponseError,
)
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
        self.status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        return self._data

    async def text(self, errors="strict"):
        return json.dumps(self._data)


class _NotJsonResp(_Resp):
    """A non-JSON reply, e.g. the HTML 404 page a host serves for an unknown path."""
    def __init__(self, text, status=404):
        super().__init__(None)
        self._text, self.status = text, status

    async def json(self, content_type=None):
        return json.loads(self._text)   # raises ValueError, like aiohttp/orjson

    async def text(self, errors="strict"):
        return self._text


class _Session:
    """Returns queued JSON bodies for post()/request() in order.

    /datacenters (fetched once per discovery) answers `datacenters` without
    consuming the queue, and isn't recorded in `calls`.
    """
    def __init__(self, responses, datacenters=None):
        self._responses = list(responses)
        self._datacenters = datacenters if datacenters is not None else             {"code": 200, "data": []}
        self.calls = []

    def post(self, url, **kw):
        return self.request("POST", url, **kw)

    def request(self, method, url, **kw):
        if url.endswith("/datacenters"):
            d = self._datacenters
            return d if isinstance(d, _Resp) else _Resp(d)
        self.calls.append((method, url, kw))
        r = self._responses.pop(0)
        return r if isinstance(r, _Resp) else _Resp(r)


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


async def test_mirrored_lock_prefers_its_home_datacenters_copy():
    """Regression: a stale mirror must not win over the lock's home datacenter.

    Reproduces the live account: the lock is homed in north-america but is also
    echoed by Singapore's device/list, which is queried FIRST and serves a stale
    copy (`online=0` while the lock is actually up). Only the home datacenter's
    copy is live, so that's the one that must survive de-duplication -- otherwise
    the mirror's stale `online` marks every entity unavailable.
    """
    def rec(online):
        return {"wifiSN": "RL1", "userNumberId": 0,
                "dataCenter": "north-america", "online": online}

    sess = _Session([_login_3dc(),
                     _devlist(),                                       # Oneness: empty
                     {"code": 200, "data": {"wifiList": [rec("0")]}},  # SG: stale mirror
                     {"code": 200, "data": {"wifiList": [rec("1")]}}]) # NA: home, live
    ha = HomeAccess(_settings(), session=sess)
    locks = await ha.async_discover()

    assert [l.esn for l in locks] == ["RL1"]
    assert locks[0].online is True, "stale mirror copy beat the home datacenter's"
    # ...and the mirror is not worth polling: it only ever loses de-duplication.
    assert ha._active_codes == ["PhilipsNorthAmerica"]


async def test_mirror_only_datacenter_is_polled_when_nothing_hosts():
    """A lock whose home datacenter isn't in our token set stays reachable.

    Degenerate case of the mirror rule: if no queried datacenter claims to host
    the lock, the mirror is all we have, so it must still be polled rather than
    leaving an empty poll set.
    """
    rec = {"wifiSN": "RL1", "userNumberId": 0, "dataCenter": "atlantis"}
    sess = _Session([_login_3dc(), _devlist(),
                     {"code": 200, "data": {"wifiList": [rec]}},  # SG mirrors it
                     _devlist()])                                 # NA: nothing
    ha = HomeAccess(_settings(), session=sess)
    locks = await ha.async_discover()

    assert [l.esn for l in locks] == ["RL1"]
    assert ha._active_codes == ["PhilipsSingapore"]


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


async def test_login_refusal_carries_the_clouds_reason_and_is_logged(caplog):
    # Real reply for an unregistered email; the setup screen shows `reason`.
    reply = {"msg": "Account does not exist", "errDes": "Account does not exist",
             "code": 1004, "errCode": "account_not_find"}
    acct = Account(_settings(), _Session([reply]))
    with caplog.at_level("WARNING"), pytest.raises(AuthError) as ei:
        await acct.async_login()
    assert ei.value.code == 1004
    assert ei.value.reason == "Account does not exist (code 1004)"
    assert "account_not_find" in caplog.text and "pw" not in caplog.text


async def test_login_success_log_redacts_tokens(caplog):
    secret = _decodable_token()
    login = {"code": 200, "data": {"users": [
        {"uid": "U1", "token": secret, "code": "PhilipsNorthAmerica"}]}}
    acct = Account(_settings(), _Session([login]))
    with caplog.at_level("DEBUG"):
        await acct.async_login()
    assert "Login: HTTP 200" in caplog.text
    assert secret not in caplog.text and "'token': '***'" in caplog.text


async def test_login_non_json_reply_is_a_connection_error():
    acct = Account(_settings(), _Session([_NotJsonResp("<html>502 Bad Gateway</html>", 502)]))
    with pytest.raises(HomeAccessResponseError, match="HTTP 502"):
        await acct.async_login()


async def test_login_without_users_explains_instead_of_crashing():
    # e.g. status 1 + confirmCode, which needs the (unimplemented) loginConfirm step
    reply = {"code": 200, "data": {"status": 1, "confirmCode": "c0de", "users": None}}
    acct = Account(_settings(), _Session([reply]))
    with pytest.raises(AuthError) as ei:
        await acct.async_login()
    assert "extra sign-in step" in ei.value.reason and "c0de" not in str(ei.value)


async def test_transport_reauths_once_on_444():
    reauths = []

    async def token_provider():
        return "tok"

    async def reauth(rejected_token, user_initiated=False):
        reauths.append(rejected_token)

    sess = _Session([{"code": "444", "msg": "Not logged in"}, {"code": 200, "msg": "ok"}])
    http = HttpClient("https://x", token_provider=token_provider, reauth=reauth, session=sess)
    out = await http.post_signed("/p", {"esn": "RL"})
    assert out["code"] == 200 and reauths == ["tok"]


async def _tok():
    return "tok"


async def test_transport_non_json_reply_raises_typed_error():
    # Issue #1: a command host answered open-device with a non-JSON 404 page,
    # which escaped as a raw orjson.JSONDecodeError.
    sess = _Session([_NotJsonResp("<html>404 Not Found</html>")])
    http = HttpClient("https://x", token_provider=_tok, session=sess)
    with pytest.raises(HomeAccessResponseError, match=r"HTTP 404.*404 Not Found"):
        await http.post("/v3/device/open-device")
    # still a connection error, so the coordinator's poll handling is unchanged
    assert issubclass(HomeAccessResponseError, HomeAccessConnectionError)


async def test_transport_empty_or_non_object_reply_raises_typed_error():
    for body in (None, ["not", "an", "object"]):
        http = HttpClient("https://x", token_provider=_tok, session=_Session([_Resp(body)]))
        with pytest.raises(HomeAccessResponseError):
            await http.post("/p")


def _ha_with_lock(command_response):
    login = {"code": 200, "data": {"users": [
        {"uid": "U1", "token": _decodable_token(), "code": "PhilipsNorthAmerica"}]}}
    sess = _Session([login, _devlist("RL"), command_response])
    return HomeAccess(_settings(), session=sess), sess


async def test_command_accepted_on_code_200():
    ha, sess = _ha_with_lock({"code": 200, "msg": "success"})
    await ha.async_discover()
    assert (await ha.async_unlock("RL"))["code"] == 200
    assert sess.calls[-1][1].endswith("/v3/device/open-device")


async def test_command_refused_raises_instead_of_passing_for_success():
    ha, _ = _ha_with_lock({"code": 500, "msg": "device not support"})
    await ha.async_discover()
    with pytest.raises(CommandError, match="device not support"):
        await ha.async_lock("RL")


def _ha_with_gateway_lock(command_response):
    login = {"code": 200, "data": {"users": [
        {"uid": "U1", "token": _decodable_token(), "code": "PhilipsNorthAmerica"}]}}
    devices = {"code": 200, "data": {"wifiList": [
        {"wifiSN": "GW1", "deviceType": "GATEWAY"},
        {"wifiSN": "BL1", "deviceType": "LOCK", "masterSn": "GW1",
         "mac": "aabbccddeeff", "openStatus": 1, "userNumberId": 3}]}}
    sess = _Session([login, devices, command_response])
    return HomeAccess(_settings(), session=sess), sess


@pytest.fixture
def sent_params(monkeypatch):
    """What went into the encrypted command body (encrypted with the server's
    key, so the wire bytes can't be read back)."""
    from homeaccess import crypto
    captured = []
    real = crypto.encrypted_command_body

    def spy(params):
        captured.append(dict(params))
        return real(params)

    monkeypatch.setattr(crypto, "encrypted_command_body", spy)
    return captured


async def test_gateway_lock_commands_go_through_the_gateway(sent_params):
    ha, sess = _ha_with_gateway_lock({"code": 200, "msg": "success"})
    await ha.async_discover()
    await ha.async_unlock("BL1")
    assert sess.calls[-1][1].endswith("/v3/gateway/set-lock-open")
    assert sent_params[-1] == {"esn": "BL1", "mac": "AA:BB:CC:DD:EE:FF",
                               "masterSn": "GW1", "userNumberId": 3}


async def test_gateway_lock_close_path(sent_params):
    ha, sess = _ha_with_gateway_lock({"code": 200, "msg": "success"})
    await ha.async_discover()
    await ha.async_lock("BL1")
    assert sess.calls[-1][1].endswith("/v3/gateway/set-lock-close")


async def test_direct_lock_commands_are_unchanged(sent_params):
    ha, sess = _ha_with_lock({"code": 200, "msg": "success"})
    await ha.async_discover()
    await ha.async_lock("RL")
    assert sess.calls[-1][1].endswith("/v3/device/close-device")
    assert sent_params[-1] == {"esn": "RL", "userNumberId": 0}


def _ha_with_sg_lock(*command_responses):
    """Issue #1's shape: a lock homed in Singapore ("southeast-asia")."""
    login = {"code": 200, "data": {"users": [
        {"uid": "U1", "token": "opaque-sg", "code": "PhilipsSingapore"},
        {"uid": "U1", "token": _decodable_token(), "code": "PhilipsNorthAmerica"}]}}
    sg_list = {"code": 200, "data": {"wifiList": [
        {"wifiSN": "SG1", "dataCenter": "southeast-asia", "openStatus": 1}]}}
    sess = _Session([login, sg_list, _devlist(), *command_responses])
    return HomeAccess(_settings(), session=sess), sess


_HTML_404 = "<html>404 Not Found</html>"


async def test_command_falls_back_to_the_na_host_when_home_serves_html():
    ha, sess = _ha_with_sg_lock(_NotJsonResp(_HTML_404), {"code": 200, "msg": "ok"},
                                {"code": 200, "msg": "ok"})
    await ha.async_discover()
    await ha.async_unlock("SG1")
    home, fallback = sess.calls[-2], sess.calls[-1]
    assert home[1].startswith("https://app-sg.cone-x.com/")
    assert fallback[1].startswith("https://api.idlespacetech.com/")
    assert fallback[2]["headers"]["token"] == "opaque-sg"
    # learned: the next command goes straight to the route that worked
    await ha.async_lock("SG1")
    assert sess.calls[-1][1].startswith("https://api.idlespacetech.com/")
    assert len(sess.calls) == 6


async def test_command_tries_the_na_token_last():
    ha, sess = _ha_with_sg_lock(_NotJsonResp(_HTML_404),
                                {"code": 500, "msg": "unknown_error"},
                                {"code": 200, "msg": "ok"})
    await ha.async_discover()
    await ha.async_unlock("SG1")
    assert sess.calls[-1][2]["headers"]["token"] != "opaque-sg"


async def test_command_failing_everywhere_names_every_host():
    ha, _ = _ha_with_sg_lock(_NotJsonResp(_HTML_404), _NotJsonResp(_HTML_404),
                             {"code": 500, "msg": "unknown_error"})
    await ha.async_discover()
    with pytest.raises(CommandError) as ei:
        await ha.async_unlock("SG1")
    assert "app-sg.cone-x.com" in str(ei.value)
    assert "api.idlespacetech.com" in str(ei.value)


async def test_a_json_refusal_from_home_is_final():
    ha, sess = _ha_with_sg_lock({"code": 501, "msg": "device offline"})
    await ha.async_discover()
    with pytest.raises(CommandError, match="device offline"):
        await ha.async_unlock("SG1")
    assert sess.calls[-1][1].startswith("https://app-sg.cone-x.com/")


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


async def test_ws_awaits_on_connect():
    """on_connect fires after (re)connect so the coordinator can resync state."""
    claims = base64.urlsafe_b64encode(
        json.dumps({"uid": "U1", "exp": int(time.time()) + 3600}).encode()
    ).decode().rstrip("=")
    sess = _WSSession([])
    acct = Account(_settings(), sess)
    acct.tokenset = TokenSet("U1", {"PhilipsNorthAmerica": claims})
    rt = Realtime(acct, sess, "PhilipsNorthAmerica")

    connects = []

    async def on_connect():
        connects.append(1)

    task = asyncio.create_task(rt.listen(on_connect=on_connect))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert connects, "on_connect should fire on connect"
