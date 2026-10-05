"""Optional client-side lock-state tracker.

Maintains the best-known current state (bolt, door, battery, pending command)
from an initial snapshot plus the realtime event stream.

Ordering is the tricky part. Event timestamps are only second-granularity, and a
lock/unlock emits a *pre-actuation* `action` snapshot carrying the OLD state at
the same second as later events. So we order per facet by **(timestamp, msgId)**
-- msgId is the cloud's monotonic per-event sequence, which breaks same-second
ties (the real confirmation has a higher msgId than the stale pre-actuation
snapshot). We also drop protocol re-deliveries (identical timestamp+body, only
msgId differs) so a re-delivered stale snapshot can't clobber fresher state.

This is a convenience for apps like the CLI monitor. The library still delivers
every parsed event faithfully (parse_event / Realtime); a Home Assistant
integration can run its own state machine instead.
"""
from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass, field

from .models import LockEvent

# pending command -> the bolt state that clears it
_PENDING_TARGET = {"unlocking": "unlocked", "locking": "locked"}

# A pending command that no event or poll has confirmed in this long is given
# up on, so a jammed bolt (or a lost confirmation) doesn't spin forever.
PENDING_TIMEOUT = 30.0


@dataclass
class LockState:
    esn: str
    bolt: str | None = None        # "locked" | "unlocked"
    door: str | None = None        # "open" | "closed"
    battery: int | None = None
    pending: str | None = None     # "locking" | "unlocking" | None
    online: bool = True            # cloud's view of the device's WiFi connectivity
    pending_since: float | None = None  # time.monotonic() when pending was set

    def summary(self) -> str:
        bat = f"{self.battery}%" if self.battery is not None else "?"
        s = f"lock={self.bolt or '?'} door={self.door or '?'} battery={bat}"
        if not self.online:
            s += " OFFLINE"
        return s + (f" ({self.pending}…)" if self.pending else "")


@dataclass
class ApplyResult:
    stale: bool = False                  # older than what we've applied -> ignored
    duplicate: bool = False              # protocol re-delivery -> ignored
    changes: list[str] = field(default_factory=list)


class LockTracker:
    def __init__(self, state: LockState) -> None:
        self.state = state
        self._order: dict[str, tuple[int, int]] = {}   # facet -> (epoch_ts, msgId)
        self._seen: deque[tuple] = deque(maxlen=128)    # recent (timestamp, body)

    @staticmethod
    def _key(ev: LockEvent) -> tuple[int, int]:
        ts = int(ev.timestamp) if ev.timestamp and str(ev.timestamp).isdigit() else 0
        mid = ev.msg_id if isinstance(ev.msg_id, int) else 0
        return (ts, mid)

    def _accept(self, facet: str, key: tuple[int, int]) -> bool:
        last = self._order.get(facet)
        return last is None or key > last

    def _is_redelivery(self, ev: LockEvent) -> bool:
        body = ev.raw.get("body") if ev.raw else None
        if body is None:
            return False  # no body to compare -> rely on (ts, msgId) ordering
        k = (ev.timestamp, json.dumps(body, sort_keys=True, default=str))
        if k in self._seen:
            return True
        self._seen.append(k)
        return False

    def set_pending(self, pending: str | None, now: float | None = None) -> list[str]:
        """Mark a command in flight ("locking"/"unlocking"), or clear it."""
        if pending == self.state.pending:
            return []
        self.state.pending = pending
        self.state.pending_since = (time.monotonic() if now is None else now) \
            if pending else None
        return [f"pending={pending}" if pending else "pending=cleared"]

    def expire_pending(self, now: float | None = None) -> list[str]:
        """Drop a pending command older than PENDING_TIMEOUT."""
        since = self.state.pending_since
        if self.state.pending is None or since is None:
            return []
        if (time.monotonic() if now is None else now) - since < PENDING_TIMEOUT:
            return []
        return self.set_pending(None)

    def apply_poll(self, bolt: str | None, door: str | None, battery: int | None,
                   online: bool, now: float | None = None) -> list[str]:
        """Fold in a device/list poll; return what changed.

        A poll is authoritative for the current bolt and battery; door only
        when the poll can tell (door is event-driven). `online` is always a
        definite bool and the cloud's freshest word on reachability, so it is
        trusted outright. A pending command clears once the bolt has reached
        its target -- for a lock no WebSocket covers, the poll is the only
        confirmation there is.
        """
        changes: list[str] = []
        if bolt and bolt != self.state.bolt:
            self.state.bolt = bolt
            changes.append(f"lock={bolt}")
        if door and door != self.state.door:
            self.state.door = door
            changes.append(f"door={door}")
        if battery is not None and battery != self.state.battery:
            self.state.battery = battery
            changes.append(f"battery={battery}")
        if online != self.state.online:
            self.state.online = online
            changes.append(f"online={online}")
        if self.state.pending and _PENDING_TARGET.get(self.state.pending) == self.state.bolt:
            changes += self.set_pending(None)
        changes += self.expire_pending(now)
        return changes

    def apply(self, ev: LockEvent) -> ApplyResult:
        res = ApplyResult()
        if self._is_redelivery(ev):
            res.duplicate = True
            return res
        key = self._key(ev)

        # the device's own WiFi connectivity, as seen by the cloud. This is the
        # only realtime signal we still get once a lock drops offline (no more
        # lock/door/action events will arrive until it reconnects).
        if ev.kind == "wifiState" and ev.state in ("0", "1"):
            online = ev.state == "1"
            if online != self.state.online:
                self.state.online = online
                res.changes.append(f"online={online}")

        # setLock = command issued (not yet physical) -> mark pending
        if ev.kind == "setLock" and ev.state in ("locked", "unlocked"):
            res.changes += self.set_pending(
                "unlocking" if ev.state == "unlocked" else "locking")

        # bolt from action snapshot or lock record, ordered by (ts, msgId)
        if ev.kind in ("action", "lock") and ev.state in ("locked", "unlocked"):
            if not self._accept("bolt", key):
                res.stale = True
            else:
                self._order["bolt"] = key
                if ev.state != self.state.bolt:
                    self.state.bolt = ev.state
                    res.changes.append(f"lock={ev.state}")
                if ev.kind == "lock" and self.state.pending:  # confirmation
                    res.changes += self.set_pending(None)

        # clear pending once the bolt has reached the commanded target
        if self.state.pending and _PENDING_TARGET.get(self.state.pending) == self.state.bolt:
            res.changes += self.set_pending(None)

        # door contact from door records, ordered by (ts, msgId)
        if ev.kind == "door" and ev.state in ("opened", "closed"):
            door = "open" if ev.state == "opened" else "closed"
            if not self._accept("door", key):
                res.stale = True
            else:
                self._order["door"] = key
                if door != self.state.door:
                    self.state.door = door
                    res.changes.append(f"door={door}")

        # battery from any event that carries it, ordered by (ts, msgId)
        if ev.battery is not None and self._accept("battery", key):
            self._order["battery"] = key
            if ev.battery != self.state.battery:
                self.state.battery = ev.battery
                res.changes.append(f"battery={ev.battery}")

        return res
