"""Realtime lock events over WebSocket (datacenters that expose ws_addr).

The NA datacenter pushes lock events over a WebSocket; auth is the account token
in the Sec-WebSocket-Protocol handshake header. Keep-alive is WS ping frames
(not JSON). Events arrive as text frames; see parse_event() for the decoding.

Singapore-style datacenters use MQTT instead (not implemented here).
"""
from __future__ import annotations

import json
import ssl
import threading
import time
from typing import Callable

import websocket

from . import constants
from .models import Datacenter, LockEvent
from .session import Account


def parse_event(msg: str) -> LockEvent | None:
    try:
        d = json.loads(msg)
    except ValueError:
        return None
    func, body = d.get("func"), d.get("body") or {}
    lock_id = body.get("wfId") or body.get("lockId") or ""
    if func == "setLock":
        opt = (body.get("params") or {}).get("dooropt")
        return LockEvent("setLock", lock_id,
                         state="unlocked" if opt == 1 else "locked",
                         source="remote", raw=d)
    if func == "wfevent":
        ev = body.get("eventtype")
        p = body.get("eventparams") or {}
        if ev == "record":
            code, src = p.get("eventCode"), p.get("eventSource")
            if src == constants.REMOTE_EVENT_SOURCE:
                return LockEvent("record", lock_id,
                                 state=constants.EVENT_CODE_REMOTE.get(code),
                                 source="remote", user_id=p.get("userID"), raw=d)
            return LockEvent("record", lock_id,
                             state=constants.EVENT_CODE_MANUAL.get(code),
                             source="manual", user_id=p.get("userID"), raw=d)
        return LockEvent(f"wfevent/{ev}", lock_id, raw=d)
    return LockEvent(func or "?", lock_id, raw=d)


class Realtime:
    def __init__(self, account: Account,
                 datacenter_code: str = constants.DEFAULT_DATACENTER) -> None:
        self.account = account
        self.dc = Datacenter.by_code(datacenter_code)
        if not self.dc.ws_addr:
            raise RuntimeError(
                f"Datacenter {datacenter_code} has no WebSocket "
                f"(mqtt_addr={self.dc.mqtt_addr!r}); MQTT is not implemented.")
        self._verify = account.settings.verify_tls

    def _connect(self) -> "websocket.WebSocket":
        token = self.account.token_for(self.dc.code)
        uid = self.account.uid
        url = f"{self.dc.ws_addr}/?client_id=app:{uid}"
        sslopt = {"cert_reqs": ssl.CERT_NONE} if not self._verify else None
        return websocket.create_connection(
            url, header=[f"Sec-WebSocket-Protocol: {token}"],
            sslopt=sslopt, timeout=10)

    def listen(self, duration: float | None = None,
               on_event: Callable[[LockEvent], None] | None = None) -> None:
        """Stream events, calling on_event(LockEvent) for each. Auto-reconnects.

        duration=None runs until interrupted; otherwise stops after N seconds.
        """
        stop = threading.Event()
        end = (time.time() + duration) if duration else None

        def remaining() -> bool:
            return (end is None or time.time() < end) and not stop.is_set()

        while remaining():
            try:
                ws = self._connect()
            except Exception as e:  # noqa: BLE001
                if on_event is None:
                    print(f"connect failed: {e}; retry in 3s")
                stop.wait(3)
                continue

            def pinger(sock=ws) -> None:
                while not stop.is_set():
                    try:
                        sock.ping()
                    except Exception:  # noqa: BLE001
                        return
                    stop.wait(5)

            threading.Thread(target=pinger, daemon=True).start()
            ws.settimeout(2)
            while remaining():
                try:
                    msg = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                except Exception:  # noqa: BLE001
                    break  # dropped -> outer loop reconnects
                if not msg:
                    continue
                ev = parse_event(msg)
                if ev and on_event:
                    on_event(ev)
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
        stop.set()
