"""MQTT listener (Singapore): wire protocol against a local fake broker, and parsing.

The broker side mirrors what was verified live against mqtt-sg-app.cone-x.com:
it authenticates username=uid / password=token, refuses anything else with
CONNACK 5, and grants both subscriptions.
"""
import asyncio
import json
import struct

import pytest

from homeaccess import constants
from homeaccess import mqtt as M


# --- fake broker -------------------------------------------------------------
class _Broker:
    def __init__(self, token="tok", messages=()):
        self.token = token
        self.messages = list(messages)    # (topic, payload, qos)
        self.connects = []                # (client_id, username, password)
        self.subscribed = []
        self.acks = []                    # (packet type, packet id)
        self.server = None

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *a):
        self.server.close()
        await self.server.wait_closed()

    @property
    def addr(self):
        return f"127.0.0.1:{self.server.sockets[0].getsockname()[1]}"

    async def _handle(self, reader, writer):
        try:
            ptype, _, body = await M._read_packet(reader)
            assert ptype == M.CONNECT
            pos = 2 + 4 + 1 + 1 + 2  # "MQTT", level, flags, keepalive
            fields = []
            while pos < len(body):
                n = struct.unpack("!H", body[pos:pos + 2])[0]
                fields.append(body[pos + 2:pos + 2 + n].decode())
                pos += 2 + n
            self.connects.append(tuple(fields))
            ok = fields[2] == self.token
            writer.write(M._packet(M.CONNACK, 0, bytes([0, 0 if ok else 5])))
            if not ok:
                await writer.drain()
                writer.close()
                return
            ptype, _, body = await M._read_packet(reader)
            assert ptype == M.SUBSCRIBE
            pid, pos, topics = body[:2], 2, []
            while pos < len(body):
                n = struct.unpack("!H", body[pos:pos + 2])[0]
                topics.append(body[pos + 2:pos + 2 + n].decode())
                pos += 2 + n + 1
            self.subscribed.append(topics)
            writer.write(M._packet(M.SUBACK, 0, pid + bytes([1] * len(topics))))
            for i, (topic, payload, qos) in enumerate(self.messages, start=10):
                vh = M._s(topic) + (struct.pack("!H", i) if qos else b"")
                writer.write(M._packet(M.PUBLISH, qos << 1, vh + payload))
                if qos == 2:
                    writer.write(M._packet(M.PUBREL, 0x02, struct.pack("!H", i)))
            await writer.drain()
            while True:  # collect acks until the client goes away
                ptype, _, body = await M._read_packet(reader)
                self.acks.append((ptype, struct.unpack("!H", body[:2])[0] if body else None))
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


class _TokenSet:
    def __init__(self, token):
        self.tokens = {"PhilipsSingapore": token}


class _Account:
    def __init__(self, token="tok", uid="U1"):
        self.tokenset = _TokenSet(token)
        self.uid = uid
        self.relogins = []

    async def async_token_for(self, code, **kw):
        return self.tokenset.tokens[code]

    def uid_for(self, code):
        return self.uid

    async def async_relogin(self, rejected_token=None, **kw):
        self.relogins.append(rejected_token)
        self.tokenset.tokens["PhilipsSingapore"] = "tok"


@pytest.fixture
def broker_at(monkeypatch):
    def point(broker):
        dc = dict(constants.DATACENTERS["PhilipsSingapore"], mqtt_addr=broker.addr)
        monkeypatch.setitem(constants.DATACENTERS, "PhilipsSingapore", dc)
    return point


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(M, "RECONNECT_DELAY", 0.01)


async def _run_until(cond, coro, timeout=3):
    task = asyncio.create_task(coro)
    try:
        async with asyncio.timeout(timeout):
            while not cond():
                await asyncio.sleep(0.01)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# --- wire --------------------------------------------------------------------
WFEVENT = {"func": "wfevent", "msgtype": "event", "wfId": "W1", "devtype": "kdswflock",
           "eventtype": "record", "msgId": 7, "timestamp": "1700000000",
           "eventparams": {"eventType": 1, "eventSource": 8, "eventCode": 1}}


async def test_connects_like_the_app_and_delivers_events(broker_at, fast):
    msgs = [("/U1/rpc/reply", json.dumps(WFEVENT).encode(), 1),
            ("/kiot/U1/app/down", b'{"cmd":"report","did":"D1","body":{"properties":'
                                  b'[{"name":"p_lock_status","value":0}]}}', 2),
            ("/U1/rpc/reply", b"not json", 0)]
    async with _Broker(messages=msgs) as broker:
        broker_at(broker)
        events, raws, connected = [], [], []
        listener = M.MqttRealtime(_Account())

        async def on_connect():
            connected.append(listener.connected)

        await _run_until(lambda: len(raws) == 3 and len(broker.acks) >= 2,
                         listener.listen(on_event=events.append, on_connect=on_connect,
                                         on_raw=lambda t, p: raws.append(t)))
    assert broker.connects == [("app:U1", "U1", "tok")]
    assert broker.subscribed == [["/U1/rpc/reply", "/kiot/U1/app/down"]]
    assert connected == [True]
    assert [(e.kind, e.lock_id, e.state) for e in events] == [
        ("lock", "W1", "locked"), ("action", "D1", "unlocked")]
    assert events[0].msg_id == 7 and events[0].timestamp == "1700000000"
    # QoS 1 -> PUBACK, QoS 2 -> PUBREC then PUBCOMP
    assert (M.PUBACK, 10) in broker.acks
    assert (M.PUBREC, 11) in broker.acks and (M.PUBCOMP, 11) in broker.acks


async def test_refused_token_triggers_relogin_then_reconnects(broker_at, fast):
    async with _Broker() as broker:
        broker_at(broker)
        acct = _Account(token="displaced")
        listener = M.MqttRealtime(acct)
        await _run_until(lambda: listener.connected, listener.listen())
    assert acct.relogins == ["displaced"]
    assert [c[2] for c in broker.connects] == ["displaced", "tok"]


async def test_cancel_closes_the_connection(broker_at):
    async with _Broker() as broker:
        broker_at(broker)
        listener = M.MqttRealtime(_Account())
        await _run_until(lambda: listener.connected, listener.listen())
        assert listener.connected is False


# --- parsing -----------------------------------------------------------------
def test_flat_wfevent_parses_like_the_websocket_frame():
    ev = M.parse_mqtt("/U1/rpc/reply", json.dumps(WFEVENT).encode())
    assert (ev.kind, ev.lock_id, ev.state, ev.source) == ("lock", "W1", "locked", "remote")
    assert ev.raw["body"]["wfId"] == "W1"  # wrapped, so the tracker's dedupe key works


def test_thing_report_with_battery():
    payload = {"cmd": "report", "did": "D1", "body": {"properties": [
        {"name": "p_lock_status", "value": 1},
        {"name": "p_battery_info", "value": [[{"name": "p_battery_electricity", "value": 64}]]},
    ]}}
    ev = M.parse_mqtt("/kiot/U1/app/down", json.dumps(payload).encode())
    assert (ev.kind, ev.lock_id, ev.state, ev.battery) == ("action", "D1", "locked", 64)


def test_thing_device_state():
    ev = M.parse_mqtt("/kiot/U1/app/down",
                      b'{"cmd":"device_state","did":"D1","body":{"connectState":"Offline"}}')
    assert (ev.kind, ev.state) == ("thing/device_state", "offline")


def test_non_json_is_ignored():
    assert M.parse_mqtt("/U1/rpc/reply", b"\x00\x01") is None
    assert M.parse_mqtt("/U1/rpc/reply", b"[1,2]") is None
