"""The realtime listener must outlive every failure short of cancellation.

Regression cover for a silent, long-lived outage: `listen()` only caught
aiohttp.ClientError/TimeoutError, but the first thing it does each loop is
`async_token_for()`, which raises AuthError/HomeAccessConnectionError when a
re-login fails. Those escaped the loop, the background task died, and realtime
never came back until Home Assistant restarted -- while the safety-net poll
kept working, so nothing looked broken.
"""
import asyncio

import aiohttp
import pytest

from homeaccess import realtime as rt_mod
from homeaccess.exceptions import AuthError, HomeAccessConnectionError
from homeaccess.realtime import Realtime


# --- fakes -----------------------------------------------------------------
class _StubSettings:
    verify_tls = True


class _Account:
    """Stand-in for Account: hands back a token, or raises a chosen error."""

    def __init__(self, exc=None):
        self.settings = _StubSettings()
        self.uid = "U1"
        self._exc = exc
        self.calls = 0

    async def async_token_for(self, code, **kw):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return "tok"


class _Msg:
    def __init__(self, data):
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
        await _REAL_SLEEP(0)  # yield so cancellation can land
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class _WSSession:
    """Only ws_connect is exercised here."""

    def __init__(self, messages=()):
        self._messages = list(messages)
        self.connects = 0

    def ws_connect(self, url, **kw):
        self.connects += 1
        return _WS(list(self._messages))


_REAL_SLEEP = asyncio.sleep


@pytest.fixture
def delays(monkeypatch):
    """Record backoff sleeps instead of serving them, so the loop runs fast."""
    recorded: list[float] = []

    async def fake_sleep(secs, *a, **kw):
        if secs:
            recorded.append(secs)
        await _REAL_SLEEP(0)

    monkeypatch.setattr(rt_mod.asyncio, "sleep", fake_sleep)
    return recorded


async def _spin(turns=40):
    """Let the listener loop run a while without advancing the clock."""
    for _ in range(turns):
        await _REAL_SLEEP(0)


async def _run_briefly(rt, turns=40, **kw):
    task = asyncio.create_task(rt.listen(**kw))
    await _spin(turns)
    still_running = not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    return still_running


# --- the listener survives what used to kill it ----------------------------
@pytest.mark.parametrize("exc", [
    AuthError("login rejected"),
    HomeAccessConnectionError("login request failed"),
    aiohttp.ClientError("socket"),
    TypeError("something nobody predicted"),
])
async def test_listen_retries_instead_of_dying(exc, delays):
    """Any failure fetching the token must retry, never end the task."""
    acct = _Account(exc=exc)
    rt = Realtime(acct, _WSSession(), "PhilipsNorthAmerica")

    assert await _run_briefly(rt), f"listener died on {type(exc).__name__}"
    assert acct.calls > 1, "listener gave up after the first failure"
    assert rt.connected is False


async def test_backoff_grows_and_caps(delays, monkeypatch):
    """A persistent failure must back off, not hammer the cloud every 3s."""
    monkeypatch.setattr(rt_mod, "RECONNECT_DELAY_MAX", 24)
    rt = Realtime(_Account(exc=AuthError("nope")), _WSSession(),
                  "PhilipsNorthAmerica")
    await _run_briefly(rt, turns=60)

    assert delays[:4] == [3, 6, 12, 24], delays
    assert max(delays) == 24, "backoff exceeded its cap"


async def test_stable_session_resets_backoff(delays, monkeypatch):
    """A connection that lasted is healthy: retry promptly, don't back off."""
    monkeypatch.setattr(rt_mod, "STABLE_AFTER", 0)  # any session counts
    rt = Realtime(_Account(), _WSSession(), "PhilipsNorthAmerica")
    await _run_briefly(rt, turns=40)

    assert delays, "no reconnects happened"
    assert set(delays) == {rt_mod.RECONNECT_DELAY}, delays


# --- connection state is observable ---------------------------------------
async def test_connected_flag_tracks_the_socket(delays):
    """coordinator reads .connected to decide if realtime is really covering."""
    rt = Realtime(_Account(), _WSSession(), "PhilipsNorthAmerica")
    seen = []

    async def on_connect():
        seen.append(rt.connected)

    await _run_briefly(rt, turns=10, on_connect=on_connect)
    assert seen and all(seen), "connected should be True inside on_connect"
    assert rt.connected is False, "connected must clear once the socket is gone"


async def test_on_disconnect_fires_when_a_live_socket_drops(delays):
    """The drop is what tells the coordinator to fall back to fast polling."""
    rt = Realtime(_Account(), _WSSession(), "PhilipsNorthAmerica")
    drops = []

    async def on_disconnect():
        drops.append(1)

    await _run_briefly(rt, turns=20, on_disconnect=on_disconnect)
    assert drops, "on_disconnect never fired for a socket that was up"


async def test_no_disconnect_callback_when_connect_never_succeeded(delays):
    """Never-connected is not a drop -- don't report a transition that
    didn't happen (the coordinator already starts out polling fast)."""
    rt = Realtime(_Account(exc=AuthError("nope")), _WSSession(),
                  "PhilipsNorthAmerica")
    drops = []

    async def on_disconnect():
        drops.append(1)

    await _run_briefly(rt, turns=20, on_disconnect=on_disconnect)
    assert not drops, "reported a disconnect without ever being connected"


async def test_events_still_parse_and_dispatch(delays):
    """The happy path still works after the rewrite."""
    frame = '{"func": "setLock", "timestamp": "1", ' \
            '"body": {"wfId": "RL", "params": {"dooropt": 1}}}'
    rt = Realtime(_Account(), _WSSession([_Msg(frame)]), "PhilipsNorthAmerica")
    got = []

    await _run_briefly(rt, turns=10, on_event=got.append)
    assert any(e.kind == "setLock" and e.state == "unlocked" for e in got)
