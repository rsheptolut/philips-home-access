"""Offline tests for realtime event parsing (vectors mirror live captures)."""
import json

from homeaccess import parse_event


def _frame(d: dict) -> str:
    return json.dumps(d)


def test_setlock_unlock_and_lock():
    unlock = parse_event(_frame({"func": "setLock", "body": {
        "wfId": "RL1", "params": {"dooropt": 1, "userNumberId": 0}}}))
    assert unlock.kind == "setLock" and unlock.state == "unlocked"
    lock = parse_event(_frame({"func": "setLock", "body": {
        "wfId": "RL1", "params": {"dooropt": 0, "userNumberId": 0}}}))
    assert lock.state == "locked"


def test_record_remote_vs_manual():
    remote = parse_event(_frame({"func": "wfevent", "body": {
        "lockId": "RL1", "eventtype": "record",
        "eventparams": {"eventCode": 2, "eventSource": 8, "userID": 0}}}))
    assert remote.source == "remote" and remote.state == "unlocked"

    manual = parse_event(_frame({"func": "wfevent", "body": {
        "lockId": "RL1", "eventtype": "record",
        "eventparams": {"eventCode": 8, "eventSource": 255, "userID": 255}}}))
    assert manual.source == "manual" and manual.state == "locked"


def test_non_json_returns_none():
    assert parse_event("not json") is None
