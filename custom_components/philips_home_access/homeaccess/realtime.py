"""Realtime lock events over WebSocket (datacenters that expose ws_addr).

The NA datacenter pushes lock events over a WebSocket; auth is the account token
in the Sec-WebSocket-Protocol handshake header. Keep-alive uses WS ping frames
(aiohttp's `heartbeat`). Events arrive as text frames; see parse_event().

Singapore-style datacenters use MQTT instead (not implemented here).
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable

import aiohttp

from . import constants
from .models import Datacenter, LockEvent
from .session import Account

_LOGGER = logging.getLogger(__name__)

OnEvent = Callable[[LockEvent], None] | Callable[[LockEvent], Awaitable[None]]


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_event(msg: str) -> LockEvent | None:
    try:
        d = json.loads(msg)
    except ValueError:
        return None
    ev = _classify(d)
    if ev is not None:
        ev.msg_id = d.get("msgId")
        ev.timestamp = d.get("timestamp")
    return ev


def _classify(d: dict) -> LockEvent | None:
    func, body = d.get("func"), d.get("body") or {}
    lock_id = body.get("wfId") or body.get("lockId") or ""
    p = body.get("eventparams") or {}

    if func == "setLock":
        opt = (body.get("params") or {}).get("dooropt")
        return LockEvent("setLock", lock_id,
                         state="unlocked" if opt == 1 else "locked",
                         source="remote", raw=d)

    if func == "partsInfo":  # door-sensor accessory report
        return LockEvent("parts", lock_id, battery=_int(p.get("power")), raw=d)

    if func == "wfevent":
        ev = body.get("eventtype")
        if ev == "record":
            etype, code = p.get("eventType"), p.get("eventCode")
            if etype == constants.EVENT_TYPE_DOOR:
                return LockEvent("door", lock_id,
                                 state=constants.DOOR_EVENT_CODE.get(code),
                                 user_id=p.get("userID"), raw=d)
            # lock-bolt record (eventType 1, or anything else by default)
            if p.get("eventSource") == constants.REMOTE_EVENT_SOURCE:
                st, who = constants.EVENT_CODE_REMOTE.get(code), "remote"
            else:
                st, who = constants.EVENT_CODE_MANUAL.get(code), "manual"
            return LockEvent("lock", lock_id, state=st, source=who,
                             user_id=p.get("userID"), raw=d)
        if ev == "action":  # full state snapshot -> bolt state + battery
            return LockEvent("action", lock_id,
                             state=constants.OPEN_STATUS.get(p.get("openStatus")),
                             battery=_int(p.get("power")), raw=d)
        return LockEvent(f"wfevent/{ev}", lock_id, raw=d)
    return LockEvent(func or "?", lock_id, raw=d)


class Realtime:
    def __init__(self, account: Account, session: aiohttp.ClientSession,
                 datacenter_code: str = constants.DEFAULT_DATACENTER) -> None:
        self.account = account
        self._session = session
        self.dc = Datacenter.by_code(datacenter_code)
        if not self.dc.ws_addr:
            raise RuntimeError(
                f"Datacenter {datacenter_code} has no WebSocket "
                f"(mqtt_addr={self.dc.mqtt_addr!r}); MQTT is not implemented.")
        self._ssl = None if account.settings.verify_tls else False

    async def listen(self, on_event: OnEvent | None = None) -> None:
        """Stream events, calling on_event(LockEvent) for each. Auto-reconnects.

        Runs until cancelled. on_event may be a sync function or a coroutine
        function. Cancel-safe: cancelling the task closes the socket cleanly.
        (For a time-boxed run, wrap in asyncio.wait_for or cancel the task.)
        """
        is_coro = on_event is not None and asyncio.iscoroutinefunction(on_event)
        while True:
            try:
                token = await self.account.async_token_for(self.dc.code)
                url = f"{self.dc.ws_addr}/?client_id=app:{self.account.uid}"
                async with self._session.ws_connect(
                    url, protocols=(token,), ssl=self._ssl, heartbeat=5,
                ) as ws:
                    _LOGGER.debug("ws connected to %s", self.dc.code)
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        _LOGGER.debug("ws ← %s", msg.data[:400])
                        ev = parse_event(msg.data)
                        if ev and on_event:
                            await on_event(ev) if is_coro else on_event(ev)
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                _LOGGER.debug("ws dropped (%s); reconnecting in 3s", e)
                await asyncio.sleep(3)
