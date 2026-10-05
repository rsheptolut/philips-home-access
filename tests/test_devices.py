"""Telling a paired accessory apart from a lock.

device/list returns the magnetic door sensor alongside the lock, and its
`deviceType` reads "LOCK" exactly as the lock's does -- so the integration built
a full lock device for it: a lock entity that could never have a bolt state and
a duplicate door entity. Records below mirror a real account's shape (serials anonymised).
"""
from homeaccess.models import Lock


def _lock_record():
    """The real lock: pid RL2, no master, reports a bolt."""
    return {"wifiSN": "RL21234567890", "lockNickname": "Front Door", "pid": "RL2",
            "model": "E1A8A-RL2", "productModel": "DDL230X-10HW", "masterSn": "",
            "deviceType": "LOCK", "online": "1", "openStatus": 1,
            "magneticStatus": 2, "power": 55, "partsState": 0,
            "subDeviceList": [{"pid": "DLS", "wifiSN": "DLS1234567890"}]}


def _accessory_record():
    """The door sensor: pid DLS, paired to the lock, no bolt -- but has a battery."""
    return {"wifiSN": "DLS1234567890", "lockNickname": "DDL501-S1234567",
            "pid": "DLS", "model": "W131S", "productModel": "DDL501-S",
            "masterSn": "RL21234567890", "deviceType": "LOCK", "online": "1",
            "power": 84, "partsState": 1}


def _mk(rec):
    return Lock.from_device_record(rec, "PhilipsNorthAmerica")


def test_the_door_sensor_is_not_a_lock():
    acc = _mk(_accessory_record())
    assert acc.is_accessory is True
    assert acc.master_sn == "RL21234567890"
    assert acc.open_status is None, "an accessory has no bolt to report"
    assert acc.battery == 84, "but it does have its own battery, worth keeping"


def test_the_lock_is_a_lock():
    lock = _mk(_lock_record())
    assert lock.is_accessory is False
    assert lock.master_sn == ""
    assert lock.open_status == "locked"
    assert lock.battery == 55, "the lock's own battery, distinct from the sensor's"


def test_deviceType_does_not_separate_them():
    """Guards the trap that caused this: both records say deviceType LOCK."""
    assert _lock_record()["deviceType"] == _accessory_record()["deviceType"]


def test_a_lock_missing_openStatus_is_still_a_lock():
    """A top-level lock has no masterSn, so a momentarily absent bolt field
    must never demote it -- that would delete the user's lock entity."""
    rec = _lock_record()
    del rec["openStatus"]
    assert _mk(rec).is_accessory is False


def test_a_slave_lock_that_reports_a_bolt_stays_a_lock():
    """Paired is not the same as accessory: a sub-device that reports a bolt is
    a lock, and must keep its lock entity."""
    rec = _accessory_record()
    rec["openStatus"] = 2
    assert _mk(rec).is_accessory is False


def test_datacenter_field_maps_to_its_code():
    from homeaccess.constants import datacenter_code_for
    assert datacenter_code_for("north-america", "X") == "PhilipsNorthAmerica"
    # the login reply pairs PhilipsSingapore with dataCenter "southeast-asia"
    assert datacenter_code_for("southeast-asia", "X") == "PhilipsSingapore"
    assert datacenter_code_for("", "PhilipsOneness") == "PhilipsOneness"
    assert datacenter_code_for("mars-colony", "PhilipsOneness") == "PhilipsOneness"


# --- locks behind a Wi-Fi gateway ------------------------------------------
# Shape from rjbogz/philips_home_access and issue #3 (Bluetooth locks bridged by
# a gateway); serials invented.
def _gateway_record():
    return {"wifiSN": "GW1234567890", "lockNickname": "Gateway",
            "deviceType": "GATEWAY", "online": "1", "rssi": "-55dBm"}


def _gateway_lock_record():
    return {"wifiSN": "BL1234567890", "lockNickname": "Back Door",
            "deviceType": "LOCK", "masterSn": "GW1234567890",
            "mac": "aabbccddeeff", "openStatus": 1, "power": 70, "online": "1"}


def test_a_gateway_is_neither_lock_nor_accessory():
    gw = _mk(_gateway_record())
    assert gw.is_gateway is True
    assert gw.is_accessory is False
    assert gw.battery is None and gw.open_status is None


def test_a_lock_behind_a_gateway_is_a_lock():
    lk = _mk(_gateway_lock_record())
    assert lk.is_gateway is False and lk.is_accessory is False
    assert lk.master_sn == "GW1234567890" and lk.mac == "aabbccddeeff"


def test_cache_keeps_what_routing_and_roles_need():
    """Commands can run off the device cache before the first poll, so the
    cached copy must still know its gateway and mac -- and must not turn a
    gateway lock (masterSn, no bolt field) into an accessory."""
    for rec in (_gateway_lock_record(), _gateway_record(), _accessory_record()):
        orig = _mk(rec)
        back = Lock.from_dict(orig.to_dict())
        assert (back.master_sn, back.mac, back.is_gateway, back.is_accessory) == \
            (orig.master_sn, orig.mac, orig.is_gateway, orig.is_accessory)


def test_old_cache_without_raw_still_loads():
    d = {"esn": "RL1", "datacenter_code": "PhilipsNorthAmerica"}
    assert Lock.from_dict(d).master_sn == ""


def test_mac_normalization():
    from homeaccess.api import _normalize_mac
    assert _normalize_mac("aabbccddeeff") == "AA:BB:CC:DD:EE:FF"
    assert _normalize_mac("aa-bb-cc-dd-ee-ff") == "AA:BB:CC:DD:EE:FF"
    assert _normalize_mac(" AA:BB:CC:DD:EE:FF ") == "AA:BB:CC:DD:EE:FF"
    assert _normalize_mac("abc") == "ABC"
    assert _normalize_mac("") == ""
