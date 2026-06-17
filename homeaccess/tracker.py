"""Optional client-side lock-state tracker.

Maintains the best-known current state (bolt, door, battery, pending command)
from an initial snapshot plus the realtime event stream. Newest-wins per facet
with an out-of-order guard, so a late-arriving older event can't clobber fresher
state.

This is a convenience for apps like the CLI monitor. The library still delivers
every parsed event faithfully (parse_event / Realtime); a Home Assistant
integration can run its own state machine instead.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import LockEvent

# pending command -> the bolt state that clears it
_PENDING_TARGET = {"unlocking": "unlocked", "locking": "locked"}


@dataclass
class LockState:
    esn: str
    bolt: str | None = None        # "locked" | "unlocked"
    door: str | None = None        # "open" | "closed"
    battery: int | None = None
    pending: str | None = None     # "locking" | "unlocking" | None

    def summary(self) -> str:
        bat = f"{self.battery}%" if self.battery is not None else "?"
        s = f"lock={self.bolt or '?'} door={self.door or '?'} battery={bat}"
        return s + (f" ({self.pending}…)" if self.pending else "")


@dataclass
class ApplyResult:
    stale: bool = False            # event older than what we've applied -> ignored
    changes: list[str] = field(default_factory=list)


class LockTracker:
    def __init__(self, state: LockState) -> None:
        self.state = state
        self._ts: dict[str, int] = {}   # facet -> last applied epoch seconds

    @staticmethod
    def _epoch(ev: LockEvent) -> int | None:
        t = ev.timestamp
        return int(t) if t and str(t).isdigit() else None

    def _accept(self, facet: str, ts: int | None) -> bool:
        last = self._ts.get(facet)
        # accept if we have no basis to compare, else require >= last seen
        return ts is None or last is None or ts >= last

    def apply(self, ev: LockEvent) -> ApplyResult:
        ts = self._epoch(ev)
        res = ApplyResult()

        # setLock = command issued (not yet physical) -> mark pending
        if ev.kind == "setLock" and ev.state in ("locked", "unlocked"):
            self.state.pending = "unlocking" if ev.state == "unlocked" else "locking"

        # bolt state from the real signals: action snapshot or lock record
        if ev.kind in ("action", "lock") and ev.state in ("locked", "unlocked"):
            if not self._accept("bolt", ts):
                res.stale = True
            else:
                if ev.state != self.state.bolt:
                    self.state.bolt = ev.state
                    res.changes.append(f"lock={ev.state}")
                self._ts["bolt"] = ts if ts is not None else self._ts.get("bolt", 0)
        if ev.kind == "lock":  # the confirmation clears any pending command
            self.state.pending = None
        elif self.state.pending and _PENDING_TARGET.get(self.state.pending) == self.state.bolt:
            self.state.pending = None

        # door contact from door records
        if ev.kind == "door" and ev.state in ("opened", "closed"):
            door = "open" if ev.state == "opened" else "closed"
            if not self._accept("door", ts):
                res.stale = True
            elif door != self.state.door:
                self.state.door = door
                res.changes.append(f"door={door}")
                self._ts["door"] = ts if ts is not None else self._ts.get("door", 0)
            else:
                self._ts["door"] = ts if ts is not None else self._ts.get("door", 0)

        # battery from any event that carries it
        if ev.battery is not None and self._accept("battery", ts):
            if ev.battery != self.state.battery:
                self.state.battery = ev.battery
                res.changes.append(f"battery={ev.battery}")
            self._ts["battery"] = ts if ts is not None else self._ts.get("battery", 0)

        return res
