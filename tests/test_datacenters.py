"""Datacenters the built-in map lacks, learned from the cloud's /datacenters.

Login hands back a token per datacenter -- including PhilipsNorthAmericaNew,
added after this client was written -- and a datacenter with no known host was
silently skipped, along with any locks it holds.
"""
import pytest

from homeaccess import HomeAccess, constants, state
from homeaccess.exceptions import HomeAccessConnectionError

from test_async_io import _NotJsonResp, _Session, _decodable_token, _devlist, _settings

# The live reply's shape (2026-10-05), trimmed.
DATACENTERS_REPLY = {"code": 200, "msg": "success", "data": [
    {"code": "PhilipsNorthAmericaNew", "identitySupports": "EMAIL",
     "apiAddr": "https://api.teeho.com", "wsAddr": "ws://ws.teeho.com:18091",
     "p2pAddr": "", "mqttAddr": "mqtt-app.teeho.com:1883"},
    {"code": "PhilipsNorthAmerica", "identitySupports": "EMAIL",
     "apiAddr": "https://evil.example.com:443/", "wsAddr": "wss://evil.example.com"},
]}


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    monkeypatch.setattr(constants, "DATACENTERS",
                        {k: dict(v) for k, v in constants.DATACENTERS.items()})


def _login_new_and_na():
    return {"code": 200, "data": {"users": [
        {"uid": "U1", "token": "opaque-new", "code": "PhilipsNorthAmericaNew"},
        {"uid": "U1", "token": _decodable_token(), "code": "PhilipsNorthAmerica"}]}}


async def test_a_datacenter_from_the_cloud_gets_scanned():
    sess = _Session([_login_new_and_na(), _devlist("NEW1"), _devlist()],
                    datacenters=DATACENTERS_REPLY)
    ha = HomeAccess(_settings(), session=sess)
    locks = await ha.async_discover()
    urls = [c[1] for c in sess.calls]
    assert "https://api.teeho.com/homeaccess/device/list" in urls
    assert [l.esn for l in locks] == ["NEW1"]


def test_registration_never_repoints_a_built_in_datacenter():
    constants.register_datacenters(DATACENTERS_REPLY["data"])
    assert constants.DATACENTERS["PhilipsNorthAmerica"]["api_base"] == \
        "https://api.idlespacetech.com"


def test_a_new_datacenters_realtime_stays_off_until_verified():
    assert constants.register_datacenters(DATACENTERS_REPLY["data"]) == \
        ["PhilipsNorthAmericaNew"]
    new = constants.DATACENTERS["PhilipsNorthAmericaNew"]
    assert new["api_base"] == "https://api.teeho.com"
    assert new["ws_addr"] == "" and new["ws_addr_advertised"].startswith("ws://")


def test_junk_entries_are_ignored():
    assert constants.register_datacenters(
        [None, {}, {"code": "X"}, {"code": "Y", "apiAddr": "http://plain.example"}]) == []


async def test_fetch_failure_falls_back_to_the_built_in_map():
    sess = _Session([_login_new_and_na(), _devlist("RL")],
                    datacenters=_NotJsonResp("<html>502</html>", 502))
    ha = HomeAccess(_settings(), session=sess)
    assert [l.esn for l in await ha.async_discover()] == ["RL"]
    assert "PhilipsNorthAmericaNew" not in constants.DATACENTERS


async def test_fetch_failure_uses_the_last_good_copy(monkeypatch):
    ok = _Session([_login_new_and_na(), _devlist(), _devlist()],
                  datacenters=DATACENTERS_REPLY)
    await HomeAccess(_settings(), session=ok).async_discover()
    # a fresh process: built-in map only, and the cloud is unreachable
    monkeypatch.setattr(constants, "DATACENTERS",
                        {k: v for k, v in constants.DATACENTERS.items()
                         if k != "PhilipsNorthAmericaNew"})
    down = _Session([_devlist("NEW1"), _devlist()],
                    datacenters=_NotJsonResp("<html>502</html>", 502))
    locks = await HomeAccess(_settings(), session=down).async_discover()
    assert [l.esn for l in locks] == ["NEW1"]


async def test_one_failing_datacenter_does_not_hide_the_others():
    sess = _Session([_login_new_and_na(), _NotJsonResp("<html>oops</html>", 500),
                     _devlist("RL")], datacenters=DATACENTERS_REPLY)
    locks = await HomeAccess(_settings(), session=sess).async_discover()
    assert [l.esn for l in locks] == ["RL"]


async def test_every_datacenter_failing_is_still_an_error():
    sess = _Session([_login_new_and_na(), _NotJsonResp("<html>oops</html>", 500),
                     _NotJsonResp("<html>oops</html>", 500)],
                    datacenters=DATACENTERS_REPLY)
    with pytest.raises(HomeAccessConnectionError):
        await HomeAccess(_settings(), session=sess).async_discover()
