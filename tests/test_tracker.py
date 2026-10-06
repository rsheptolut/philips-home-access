"""Tests for the client-side LockTracker (init + newest-wins + out-of-order)."""
from homeaccess import LockEvent, LockState, LockTracker
from homeaccess.tracker import PENDING_TIMEOUT


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


def test_wifistate_tracks_connectivity_and_state_is_frozen_while_offline():
    # A device going offline is the only realtime signal left once it drops --
    # no further lock/door/action events can arrive until it reconnects.
    tr = LockTracker(LockState("RL", bolt="locked", door="closed", battery=100))
    assert tr.state.online is True
    r = tr.apply(LockEvent("wifiState", "RL", state="0", timestamp="1"))
    assert tr.state.online is False and "online=False" in r.changes
    # commands issued while offline get no confirmation -> bolt stays put
    tr.apply(LockEvent("setLock", "RL", state="unlocked", timestamp="2"))
    assert tr.state.bolt == "locked" and tr.state.pending == "unlocking"
    r = tr.apply(LockEvent("wifiState", "RL", state="1", timestamp="3"))
    assert tr.state.online is True and "online=True" in r.changes


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


# --- commands confirmed by polling (no WebSocket) --------------------------
def test_poll_clears_pending_once_the_bolt_reaches_the_target():
    tr = LockTracker(LockState("RL", bolt="locked"))
    tr.set_pending("unlocking", now=0)
    assert "pending=cleared" not in tr.apply_poll("locked", None, 80, True, now=5)
    assert tr.state.pending == "unlocking"
    changes = tr.apply_poll("unlocked", None, 80, True, now=15)
    assert "lock=unlocked" in changes and "pending=cleared" in changes
    assert tr.state.pending is None and tr.state.pending_since is None


def test_pending_expires_when_nothing_confirms_it():
    tr = LockTracker(LockState("RL", bolt="locked"))
    tr.set_pending("unlocking", now=100)
    assert tr.expire_pending(now=100 + PENDING_TIMEOUT - 1) == []
    assert tr.expire_pending(now=100 + PENDING_TIMEOUT) == ["pending=cleared"]
    assert tr.state.pending is None


def test_poll_expires_a_stale_pending_too():
    tr = LockTracker(LockState("RL", bolt="locked"))
    tr.set_pending("unlocking", now=0)
    assert "pending=cleared" in tr.apply_poll("locked", None, None, True,
                                              now=PENDING_TIMEOUT + 1)


def test_poll_keeps_door_when_it_cannot_tell():
    tr = LockTracker(LockState("RL", bolt="locked", door="open"))
    tr.apply_poll("locked", None, None, True)
    assert tr.state.door == "open"


def test_poll_reports_connectivity_changes():
    tr = LockTracker(LockState("RL", online=True))
    assert tr.apply_poll(None, None, None, False) == ["online=False"]
    assert tr.state.online is False


def test_ws_command_event_stamps_pending_for_expiry():
    tr = LockTracker(LockState("RL", bolt="locked"))
    tr.apply(LockEvent("setLock", "RL", state="unlocked", timestamp="10"))
    assert tr.state.pending_since is not None


# --- the lock's timestamps can run backwards ------------------------------
def _rec(msg_id, ts, kind, state):
    return LockEvent(kind, "RL", state=state, msg_id=msg_id, timestamp=str(ts))


def test_quick_unlock_then_lock_ends_locked():
    """Live frames, 2026-10-06: a fast manual unlock->lock. The lock record was
    stamped 3 s BEFORE the unlock it followed (msgId still counted up), was
    dropped as stale, and HA showed unlocked while the door was locked."""
    tr = LockTracker(LockState("RL", bolt="locked"))
    tr.apply(_rec(956, 1791294218, "lock", "locked"))
    tr.apply(_rec(961, 1791294228, "lock", "unlocked"))
    tr.apply(_rec(963, 1791294228, "action", "unlocked"))
    r = tr.apply(_rec(964, 1791294225, "lock", "locked"))
    assert not r.stale and tr.state.bolt == "locked"
    for mid, ts in ((966, 1791294225), (967, 1791294226)):
        tr.apply(_rec(mid, ts, "action", "locked"))
    assert tr.state.bolt == "locked"


def test_same_second_pre_actuation_snapshot_still_loses():
    tr = LockTracker(LockState("RL", bolt="locked"))
    tr.apply(_rec(101, 500, "lock", "unlocked"))
    r = tr.apply(_rec(99, 500, "action", "locked"))   # older msgId, same second
    assert r.stale and tr.state.bolt == "unlocked"


def test_msgid_reset_after_power_cycle_is_ordered_by_time():
    """msgId restarts at 0 when the lock loses power; well apart in time, the
    timestamp decides, so the lock's events aren't ignored after a battery swap."""
    tr = LockTracker(LockState("RL", bolt="locked"))
    tr.apply(_rec(5000, 1000, "lock", "unlocked"))
    r = tr.apply(_rec(3, 1000 + 600, "lock", "locked"))
    assert not r.stale and tr.state.bolt == "locked"
