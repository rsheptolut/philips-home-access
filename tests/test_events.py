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


def test_lock_record_remote_vs_manual():
    remote = parse_event(_frame({"func": "wfevent", "body": {
        "lockId": "RL1", "eventtype": "record",
        "eventparams": {"eventType": 1, "eventCode": 2, "eventSource": 8, "userID": 0}}}))
    assert remote.kind == "lock" and remote.source == "remote" and remote.state == "unlocked"

    manual = parse_event(_frame({"func": "wfevent", "body": {
        "lockId": "RL1", "eventtype": "record",
        "eventparams": {"eventType": 1, "eventCode": 8, "eventSource": 255, "userID": 255}}}))
    assert manual.kind == "lock" and manual.source == "manual" and manual.state == "locked"


def test_door_open_and_close():
    opened = parse_event(_frame({"func": "wfevent", "body": {
        "lockId": "RL1", "eventtype": "record",
        "eventparams": {"eventType": 4, "eventCode": 1, "eventSource": 10, "userID": 255}}}))
    assert opened.kind == "door" and opened.state == "opened"
    closed = parse_event(_frame({"func": "wfevent", "body": {
        "lockId": "RL1", "eventtype": "record",
        "eventparams": {"eventType": 4, "eventCode": 2, "eventSource": 10}}}))
    assert closed.kind == "door" and closed.state == "closed"


def test_action_carries_battery_and_bolt_state():
    ev = parse_event(_frame({"func": "wfevent", "body": {
        "lockId": "RL1", "eventtype": "action",
        "eventparams": {"openStatus": 2, "power": 100, "doorSensor": 1}}}))
    assert ev.kind == "action" and ev.state == "unlocked" and ev.battery == 100


def test_partsinfo_carries_battery():
    ev = parse_event(_frame({"func": "partsInfo", "body": {
        "lockId": "RL1", "eventparams": {"power": 95, "partsState": 1}}}))
    assert ev.kind == "parts" and ev.battery == 95


def test_event_carries_msgid_and_timestamp():
    ev = parse_event(_frame({"msgId": 3446, "timestamp": "1781575882",
        "func": "wfevent", "body": {"lockId": "RL1", "eventtype": "record",
        "eventparams": {"eventType": 4, "eventCode": 1}}}))
    assert ev.msg_id == 3446 and ev.timestamp == "1781575882"


def test_non_json_returns_none():
    assert parse_event("not json") is None
