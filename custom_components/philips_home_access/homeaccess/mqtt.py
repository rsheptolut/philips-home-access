"""Realtime lock events over MQTT (datacenters with an mqtt_addr: Singapore).

What the official app does (decompiled MqttService and the RN bundle):

- broker ``tcp://<mqtt_addr>``: plain TCP, MQTT 3.1.1, no TLS;
- client id ``app:<uid>``, username ``<uid>``, password ``<token>``, where uid
  and token are what login returned for this datacenter (the app keeps them as
  SoutheastUid / SoutheastToken). Nothing beyond the login, as on the WebSocket;
- subscribes to ``/<uid>/rpc/reply``: classic Wi-Fi lock events, the same
  ``wfevent`` records as the WebSocket but flat (no ``body`` wrapper); and
  ``/kiot/<uid>/app/down``: "thing model" devices, ``{"cmd": "report", "did":
  ..., "body": {"properties": [{"name": "p_lock_status", "value": 1}, ...]}}``
  and ``{"cmd": "device_state", "body": {"connectState": "online"}}``.

Proof of concept: the protocol comes from the app's code, not yet from captured
traffic, so on_raw hands every message over untouched for checking the parse.

The client is a minimal MQTT 3.1.1 subscriber on asyncio streams -- connect,
subscribe, receive (QoS 0/1/2), keepalive -- so there's no extra dependency.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import struct
import time
from typing import Awaitable, Callable

from . import constants
from .exceptions import HomeAccessError
from .models import Datacenter, LockEvent
from .realtime import RECONNECT_DELAY, RECONNECT_DELAY_MAX, STABLE_AFTER, OnEvent, _classify, _int
from .session import Account

_LOGGER = logging.getLogger(__name__)

OnRaw = Callable[[str, bytes], None]

KEEPALIVE = 60         # seconds, as the app
CONNECT_TIMEOUT = 15
SUBSCRIBE_QOS = 1      # the app asks for 2; 1 gives the same delivery for a listener

# MQTT 3.1.1 packet types (high nibble of the first byte)
CONNECT, CONNACK, PUBLISH, PUBACK, PUBREC, PUBREL, PUBCOMP = 1, 2, 3, 4, 5, 6, 7
SUBSCRIBE, SUBACK, PINGREQ, PINGRESP, DISCONNECT = 8, 9, 12, 13, 14

CONNACK_CODES = {
    1: "unacceptable protocol version", 2: "client id rejected",
    3: "server unavailable", 4: "bad username or password", 5: "not authorized",
}


class MqttError(HomeAccessError):
    """The broker refused us or broke the protocol."""


class MqttRefused(MqttError):
    def __init__(self, code: int) -> None:
        super().__init__(f"connection refused: {CONNACK_CODES.get(code, code)} ({code})")
        self.code = code


def topics_for(uid: str) -> list[str]:
    return [f"/{uid}/rpc/reply", f"/kiot/{uid}/app/down"]


# -- parsing ------------------------------------------------------------------
def parse_mqtt(topic: str, payload: bytes) -> LockEvent | None:
    """A broker message -> LockEvent (None if it isn't JSON)."""
    try:
        d = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    if d.get("cmd"):
        return _thing(d)
    if "body" not in d and (d.get("wfId") or d.get("lockId")):
        # classic event, flat: wrap it like a WebSocket frame so one parser
        # (and the tracker's dedupe on timestamp+body) serves both
        d = {"func": d.get("func") or ("wfevent" if d.get("eventtype") else None),
             "body": d, "msgId": d.get("msgId"), "timestamp": d.get("timestamp")}
    ev = _classify(d)
    if ev is not None:
        ev.msg_id = d.get("msgId")
        ev.timestamp = d.get("timestamp")
    return ev


def _thing(d: dict) -> LockEvent:
    """A thing-model message (cmd/did/body), as the app's RN code reads them."""
    did, cmd, body = d.get("did") or "", d.get("cmd"), d.get("body") or {}
    if cmd == "device_state":
        st = str(body.get("connectState") or "").lower() or None
        return LockEvent("thing/device_state", did, state=st, raw=d)
    if cmd == "report":
        props = {p.get("name"): p.get("value")
                 for p in body.get("properties") or [] if isinstance(p, dict)}
        state = None
        if "p_lock_status" in props:
            state = "locked" if props["p_lock_status"] == 1 else "unlocked"
        return LockEvent("action", did, state=state,
                         battery=_battery(props.get("p_battery_info")), raw=d)
    return LockEvent(f"thing/{cmd}", did, raw=d)


def _battery(info) -> int | None:
    # [[{"name": "p_battery_electricity", "value": 80}, ...], ...]
    if isinstance(info, list) and info and isinstance(info[0], list):
        for p in info[0]:
            if isinstance(p, dict) and p.get("name") == "p_battery_electricity":
                return _int(p.get("value"))
    return None


# -- wire format --------------------------------------------------------------
def _s(v: str) -> bytes:
    b = v.encode()
    return struct.pack("!H", len(b)) + b


def _packet(ptype: int, flags: int, body: bytes) -> bytes:
    n, length = len(body), bytearray()
    while True:
        n, digit = divmod(n, 128)
        length.append(digit | (0x80 if n else 0))
        if not n:
            break
    return bytes([ptype << 4 | flags]) + bytes(length) + body


async def _read_packet(reader: asyncio.StreamReader) -> tuple[int, int, bytes]:
    first = (await reader.readexactly(1))[0]
    n, mult = 0, 1
    for _ in range(4):
        b = (await reader.readexactly(1))[0]
        n += (b & 0x7F) * mult
        if not b & 0x80:
            break
        mult *= 128
    else:
        raise MqttError("malformed remaining length")
    return first >> 4, first & 0x0F, await reader.readexactly(n)


class MqttRealtime:
    """Listener for one MQTT datacenter; the same contract as Realtime."""

    def __init__(self, account: Account, datacenter_code: str = "PhilipsSingapore", *,
                 client_id: str | None = None,
                 extra_topics: list[str] | None = None) -> None:
        self.account = account
        self._token: str | None = None
        self.dc = Datacenter.by_code(datacenter_code)
        if not self.dc.mqtt_addr:
            raise RuntimeError(f"Datacenter {datacenter_code} has no MQTT broker")
        host, _, port = self.dc.mqtt_addr.rpartition(":")
        self._host, self._port = host, int(port)
        self._client_id = client_id
        self._extra_topics = extra_topics or []
        self.connected = False
        self._writer: asyncio.StreamWriter | None = None

    async def listen(self, on_event: OnEvent | None = None,
                     on_connect: Callable[[], Awaitable[None]] | None = None,
                     on_disconnect: Callable[[], Awaitable[None]] | None = None,
                     on_raw: OnRaw | None = None) -> None:
        """Stream events, calling on_event(LockEvent) for each. Auto-reconnects.

        Runs until cancelled. on_raw(topic, payload) sees every message before
        parsing (the PoC's capture hook).
        """
        is_coro = on_event is not None and asyncio.iscoroutinefunction(on_event)
        delay = RECONNECT_DELAY
        while True:
            connected_at: float | None = None
            refused = False
            try:
                async with contextlib.aclosing(self._session()) as messages:
                    async for topic, payload in messages:
                        if topic is None:  # (None, None) marks "subscribed"
                            connected_at = time.monotonic()
                            if on_connect is not None:
                                await on_connect()
                            continue
                        _LOGGER.debug("mqtt ← %s %s", topic, payload[:400])
                        if on_raw is not None:
                            on_raw(topic, payload)
                        ev = parse_mqtt(topic, payload)
                        if ev and on_event:
                            await on_event(ev) if is_coro else on_event(ev)
                _LOGGER.info("mqtt %s closed by server", self.dc.code)
            except asyncio.CancelledError:
                self.connected = False
                raise
            except (OSError, asyncio.IncompleteReadError, asyncio.TimeoutError) as e:
                _LOGGER.info("mqtt %s dropped (%s)", self.dc.code, e or type(e).__name__)
            except MqttRefused as e:
                _LOGGER.warning("mqtt %s: %s", self.dc.code, e)
                refused = e.code in (4, 5)
            except HomeAccessError as e:
                _LOGGER.warning("mqtt %s: %s; will retry", self.dc.code, e)
            except Exception:  # noqa: BLE001 - the listener must outlive anything
                _LOGGER.exception("mqtt %s unexpected error; will retry", self.dc.code)

            if self.connected:
                self.connected = False
                _LOGGER.info("mqtt disconnected from %s", self.dc.code)
                if on_disconnect is not None:
                    await on_disconnect()

            if connected_at is not None and time.monotonic() - connected_at >= STABLE_AFTER:
                delay = RECONNECT_DELAY
            elif refused:
                # The broker rejects a token another login displaced (a live
                # connection survives that; a reconnect does not) -- which is
                # the proof a re-login needs, so no token check first.
                try:
                    await self.account.async_relogin(self._token)
                except HomeAccessError as e:
                    _LOGGER.info("mqtt %s re-login: %s", self.dc.code, e)
            _LOGGER.debug("mqtt %s reconnecting in %ss", self.dc.code, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_DELAY_MAX)

    async def _session(self):
        """One connection: yields (None, None) once subscribed, then messages."""
        token = self._token = await self.account.async_token_for(self.dc.code)
        uid = self.account.uid_for(self.dc.code)
        client_id = self._client_id or f"app:{uid}"
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self._host, self._port), CONNECT_TIMEOUT)
        self._writer = writer
        try:
            flags = 0x80 | 0x40 | 0x02  # username, password, clean session
            writer.write(_packet(CONNECT, 0,
                                 _s("MQTT") + bytes([4, flags]) + struct.pack("!H", KEEPALIVE)
                                 + _s(client_id) + _s(uid) + _s(token)))
            await writer.drain()
            ptype, _, body = await asyncio.wait_for(_read_packet(reader), CONNECT_TIMEOUT)
            if ptype != CONNACK or len(body) < 2:
                raise MqttError(f"expected CONNACK, got packet type {ptype}")
            if body[1]:
                raise MqttRefused(body[1])
            _LOGGER.info("mqtt connected to %s (%s:%s)", self.dc.code, self._host, self._port)

            topics = topics_for(uid) + self._extra_topics
            payload = b"".join(_s(t) + bytes([SUBSCRIBE_QOS]) for t in topics)
            writer.write(_packet(SUBSCRIBE, 0x02, struct.pack("!H", 1) + payload))
            await writer.drain()

            pinger = asyncio.create_task(self._ping(writer))
            try:
                subscribed = False
                while True:
                    # the broker answers our pings, so silence this long is a dead link
                    ptype, flags, body = await asyncio.wait_for(
                        _read_packet(reader), KEEPALIVE * 1.5)
                    if ptype == SUBACK:
                        granted = list(body[2:])
                        for t, g in zip(topics, granted):
                            (_LOGGER.warning if g == 0x80 else _LOGGER.info)(
                                "mqtt subscribe %s -> %s", t,
                                "REFUSED" if g == 0x80 else f"qos {g}")
                        if not subscribed:
                            subscribed = True
                            self.connected = True
                            yield None, None
                    elif ptype == PUBLISH:
                        qos = (flags >> 1) & 0x03
                        tlen = struct.unpack("!H", body[:2])[0]
                        topic = body[2:2 + tlen].decode(errors="replace")
                        pos = 2 + tlen
                        if qos:
                            pid = body[pos:pos + 2]
                            pos += 2
                            ack = PUBACK if qos == 1 else PUBREC
                            writer.write(_packet(ack, 0, pid))
                            await writer.drain()
                        yield topic, body[pos:]
                    elif ptype == PUBREL:
                        writer.write(_packet(PUBCOMP, 0, body[:2]))
                        await writer.drain()
                    elif ptype == PINGRESP:
                        pass
                    else:
                        _LOGGER.debug("mqtt %s: ignoring packet type %s", self.dc.code, ptype)
            finally:
                pinger.cancel()
        finally:
            self._writer = None
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001 - already broken
                pass

    @staticmethod
    async def _ping(writer: asyncio.StreamWriter) -> None:
        while True:
            await asyncio.sleep(KEEPALIVE / 2)
            writer.write(_packet(PINGREQ, 0, b""))
            await writer.drain()
