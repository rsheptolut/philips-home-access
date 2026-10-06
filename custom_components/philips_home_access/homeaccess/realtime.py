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
import time
from typing import Awaitable, Callable

import aiohttp

from . import constants
from .exceptions import HomeAccessError
from .models import Datacenter, LockEvent
from .session import Account

_LOGGER = logging.getLogger(__name__)

OnEvent = Callable[[LockEvent], None] | Callable[[LockEvent], Awaitable[None]]

# Reconnect backoff. A failing re-login must not hammer the cloud, so the delay
# doubles up to RECONNECT_DELAY_MAX; a session that stayed up for at least
# STABLE_AFTER seconds counts as healthy and resets it.
RECONNECT_DELAY = 3
RECONNECT_DELAY_MAX = 300
STABLE_AFTER = 60


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
        # lockId names the lock that relays it; the report (battery included)
        # is the accessory's own, whose serial is eventparams.sn
        return LockEvent("parts", p.get("sn") or lock_id,
                         battery=_int(p.get("power")), raw=d)

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
        if ev == "wifiState":  # device's WiFi link to the cloud went up/down
            return LockEvent("wifiState", lock_id, state=body.get("state"), raw=d)
        return LockEvent(f"wfevent/{ev}", lock_id, raw=d)
    return LockEvent(func or "?", lock_id, raw=d)


class Realtime:
    def __init__(self, account: Account, session: aiohttp.ClientSession,
                 datacenter_code: str = constants.DEFAULT_DATACENTER, *,
                 check_token: Callable[[], Awaitable[None]] | None = None) -> None:
        self.account = account
        self._session = session
        # Called after a failed handshake or a short-lived session. The socket
        # can't tell us why it was refused (a displaced token gets a bare 502),
        # and async_token_for only renews a token past its expiry, so without
        # this a token another login replaced kept realtime down for up to its
        # full ~2h lifetime.
        self._check_token = check_token
        self.dc = Datacenter.by_code(datacenter_code)
        if not self.dc.ws_addr:
            raise RuntimeError(
                f"Datacenter {datacenter_code} has no WebSocket "
                f"(mqtt_addr={self.dc.mqtt_addr!r}); MQTT is not implemented.")
        self._ssl = None if account.settings.verify_tls else False
        # True only while a socket is open; the coordinator reads this to decide
        # whether realtime is actually covering the locks.
        self.connected = False

    async def listen(self, on_event: OnEvent | None = None,
                     on_connect: Callable[[], Awaitable[None]] | None = None,
                     on_disconnect: Callable[[], Awaitable[None]] | None = None,
                     ) -> None:
        """Stream events, calling on_event(LockEvent) for each. Auto-reconnects.

        Runs until cancelled -- no failure short of cancellation ends the loop.
        on_event may be a sync function or a coroutine function. on_connect and
        on_disconnect (coroutine functions) are awaited after each (re)connect
        and on each drop of a socket that was up -- use them to resync state
        that changed while the socket was down and to fall back to polling
        while it is. Cancel-safe: cancelling the task closes the socket cleanly.
        (For a time-boxed run, wrap in asyncio.wait_for or cancel the task.)
        """
        is_coro = on_event is not None and asyncio.iscoroutinefunction(on_event)
        delay = RECONNECT_DELAY
        while True:
            connected_at: float | None = None
            try:
                token = await self.account.async_token_for(self.dc.code)
                url = f"{self.dc.ws_addr}/?client_id=app:{self.account.uid}"
                async with self._session.ws_connect(
                    url, protocols=(token,), ssl=self._ssl, heartbeat=5,
                ) as ws:
                    connected_at = time.monotonic()
                    self.connected = True
                    _LOGGER.info("ws connected to %s", self.dc.code)
                    if on_connect is not None:
                        await on_connect()
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        _LOGGER.debug("ws ← %s", msg.data[:400])
                        ev = parse_event(msg.data)
                        if ev and on_event:
                            await on_event(ev) if is_coro else on_event(ev)
                    # the iterator ends quietly on a CLOSE frame; record why,
                    # or a server-side hang-up is indistinguishable from any
                    # other disconnect
                    _LOGGER.info("ws %s closed by server (code=%s, error=%s)",
                                 self.dc.code, ws.close_code, ws.exception())
            except asyncio.CancelledError:
                self.connected = False
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                _LOGGER.debug("ws %s dropped (%s)", self.dc.code, e)
            except HomeAccessError as e:
                # A re-login that fails (expired token + a cloud blip) raises
                # AuthError/HomeAccessConnectionError. These are not
                # aiohttp.ClientError, so they used to escape listen() and kill
                # the task for good -- realtime never came back until HA was
                # restarted, silently, while the safety-net poll carried on.
                _LOGGER.warning("ws %s auth/connection failure (%s); will retry",
                                self.dc.code, e)
            except Exception:  # noqa: BLE001 - the listener must outlive anything
                _LOGGER.exception("ws %s unexpected error; will retry", self.dc.code)

            if self.connected:
                self.connected = False
                _LOGGER.info("ws disconnected from %s", self.dc.code)
                if on_disconnect is not None:
                    await on_disconnect()

            # A session that lasted is evidence the endpoint is healthy: retry
            # promptly. Anything shorter backs off, so a persistent failure
            # (bad credentials, cloud outage) settles into a slow retry.
            if connected_at is not None and time.monotonic() - connected_at >= STABLE_AFTER:
                delay = RECONNECT_DELAY
            elif self._check_token is not None:
                # refused or dropped at once: maybe our token was displaced
                try:
                    await self._check_token()
                except Exception as e:  # noqa: BLE001 - best effort, retry anyway
                    _LOGGER.debug("ws %s token check failed: %s", self.dc.code, e)
            _LOGGER.debug("ws %s reconnecting in %ss", self.dc.code, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_DELAY_MAX)
