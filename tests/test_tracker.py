"""Tests for the client-side LockTracker (init + newest-wins + out-of-order)."""
from homeaccess import LockEvent, LockState, LockTracker


def test_pre_actuation_snapshot_is_not_a_change():
    # Seeded as locked; the pre-actuation `action LOCKED` echoes current state.
    tr = LockTracker(LockState("RL", bolt="locked", door="closed", battery=100))
    r1 = tr.apply(LockEvent("action", "RL", state="locked", battery=100, timestamp="100"))
    assert r1.changes == [] and not r1.stale          # no spurious change
    r2 = tr.apply(LockEvent("lock", "RL", state="unlocked", timestamp="104"))
    assert "lock=unlocked" in r2.changes and tr.state.bolt == "unlocked"


def test_out_of_order_event_ignored():
    tr = LockTracker(LockState("RL", bolt="locked"))
    tr.apply(LockEvent("lock", "RL", state="unlocked", timestamp="200"))
    r = tr.apply(LockEvent("lock", "RL", state="locked", timestamp="150"))  # older
    assert r.stale and tr.state.bolt == "unlocked"     # stale event did not clobber


def test_pending_set_on_command_and_cleared_on_confirm():
    tr = LockTracker(LockState("RL", bolt="locked"))
    tr.apply(LockEvent("setLock", "RL", state="unlocked", timestamp="10"))
    assert tr.state.pending == "unlocking"
    tr.apply(LockEvent("lock", "RL", state="unlocked", timestamp="14"))
    assert tr.state.pending is None and tr.state.bolt == "unlocked"


def test_door_and_battery_tracking():
    tr = LockTracker(LockState("RL"))
    assert tr.apply(LockEvent("door", "RL", state="opened", timestamp="1")).changes == ["door=open"]
    assert tr.state.door == "open"
    tr.apply(LockEvent("door", "RL", state="closed", timestamp="2"))
    assert tr.state.door == "closed"
    r = tr.apply(LockEvent("parts", "RL", battery=95, timestamp="3"))
    assert tr.state.battery == 95 and "battery=95" in r.changes
