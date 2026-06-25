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


def test_same_second_stale_action_does_not_regress_bolt():
    # Fast unlock->lock: the confirming lock record and the stale pre-actuation
    # `action` (old state) share a timestamp second. msgId breaks the tie.
    tr = LockTracker(LockState("RL", bolt="unlocked"))
    r1 = tr.apply(LockEvent("lock", "RL", state="locked", msg_id=3346, timestamp="1005"))
    assert tr.state.bolt == "locked" and "lock=locked" in r1.changes
    # stale snapshot arrives late, same second, LOWER msgId -> must be rejected
    r2 = tr.apply(LockEvent("action", "RL", state="unlocked", msg_id=3342, timestamp="1005"))
    assert r2.stale and not r2.changes and tr.state.bolt == "locked"


def test_redelivery_does_not_regress_bolt():
    tr = LockTracker(LockState("RL", bolt="unlocked"))
    # original stale snapshot (recorded for dedup)
    tr.apply(LockEvent("action", "RL", state="unlocked", msg_id=3340,
                       timestamp="1004", raw={"body": {"u": 1}}))
    tr.apply(LockEvent("lock", "RL", state="locked", msg_id=3346,
                       timestamp="1005", raw={"body": {"l": 1}}))
    assert tr.state.bolt == "locked"
    # re-delivery of the stale snapshot: same timestamp+body, new (higher) msgId
    r = tr.apply(LockEvent("action", "RL", state="unlocked", msg_id=3360,
                           timestamp="1004", raw={"body": {"u": 1}}))
    assert r.duplicate and not r.changes and tr.state.bolt == "locked"
