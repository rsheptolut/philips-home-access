"""Tests for operation-list -> gateway power parsing (restart-gap seeding)."""
from homeaccess.api import _latest_gateway_power


def test_latest_record_wins_when_newest_first():
    records = [
        {"eventType": 5, "eventCode": 1, "timesTamp2": 1795230407},  # on
        {"eventType": 5, "eventCode": 2, "timesTamp2": 1795230390},  # off
    ]
    assert _latest_gateway_power(records) == "on"


def test_skips_unrelated_event_types():
    records = [
        {"eventType": 1, "eventCode": 2},  # unrelated (lock event)
        {"eventType": 5, "eventCode": 2},  # off
    ]
    assert _latest_gateway_power(records) == "off"


def test_no_power_record_returns_none():
    assert _latest_gateway_power([{"eventType": 1, "eventCode": 1}]) is None
    assert _latest_gateway_power([]) is None
