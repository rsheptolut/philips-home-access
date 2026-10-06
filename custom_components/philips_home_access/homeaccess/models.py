"""Typed data models for the Philips Home Access API."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import constants


@dataclass(frozen=True)
class Datacenter:
    code: str
    api_base: str
    ws_addr: str
    mqtt_addr: str

    @classmethod
    def by_code(cls, code: str) -> "Datacenter":
        d = constants.DATACENTERS[code]
        return cls(code, d["api_base"], d["ws_addr"], d["mqtt_addr"])


@dataclass
class TokenSet:
    """One account login: a uid plus one token per datacenter."""
    uid: str
    tokens: dict[str, str]          # datacenter_code -> token
    obtained: int = 0               # epoch seconds
    # datacenter_code -> that datacenter's own uid for the account. The MQTT
    # broker authenticates with it (username), and it needn't match `uid`.
    uids: dict[str, str] = field(default_factory=dict)

    def token_for(self, datacenter_code: str) -> str | None:
        return self.tokens.get(datacenter_code)

    def uid_for(self, datacenter_code: str) -> str:
        return self.uids.get(datacenter_code) or self.uid

    def to_dict(self) -> dict[str, Any]:
        return {"uid": self.uid, "tokens": self.tokens, "obtained": self.obtained,
                "uids": self.uids}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TokenSet":
        return cls(uid=d.get("uid", ""), tokens=d.get("tokens", {}),
                   obtained=d.get("obtained", 0), uids=d.get("uids") or {})


# Record fields the device cache keeps (to_dict): enough to tell a device's role
# and route its commands -- a lock behind a gateway needs masterSn and mac --
# when a command runs off the cache before the first poll.
_CACHED_RAW_KEYS = ("masterSn", "mac", "deviceType", "openStatus")


@dataclass
class Lock:
    """A smart lock under an account, tagged with the datacenter that owns it.

    device/list lists every device the same way, so the same class also carries
    a lock's accessory (is_accessory) and a Wi-Fi gateway (is_gateway).
    """
    esn: str
    datacenter_code: str
    user_number_id: int = 0
    nickname: str = ""
    online: bool = True
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def open_status(self) -> str | None:
        return constants.OPEN_STATUS.get(self.raw.get("openStatus"))

    @property
    def battery(self) -> int | None:
        p = self.raw.get("power")
        return int(p) if p is not None else None

    @property
    def door(self) -> str | None:
        """Door contact state from magneticStatus (best-effort; see constants)."""
        return constants.MAGNETIC_STATUS.get(self.raw.get("magneticStatus"))

    @property
    def master_sn(self) -> str:
        """The device this one hangs off: the lock an accessory is paired to,
        or the gateway a gateway lock talks through ("" for a direct lock)."""
        return str(self.raw.get("masterSn") or "")

    @property
    def mac(self) -> str:
        """Bluetooth MAC; a gateway lock's commands address it by this."""
        return str(self.raw.get("mac") or "")

    @property
    def is_gateway(self) -> bool:
        """A Wi-Fi gateway that bridges Bluetooth-only locks to the cloud.

        It is listed by device/list beside its locks, with no bolt or battery
        of its own; each of its locks names it in `masterSn`.
        """
        return self.raw.get("deviceType") == "GATEWAY"

    @property
    def is_accessory(self) -> bool:
        """True for a paired accessory rather than a lock in its own right.

        The magnetic door sensor (pid "DLS", model W131S -- see
        research/FINDINGS.md) is listed by device/list exactly like a lock, and
        its `deviceType` reads "LOCK" just as the lock's does, so neither field
        separates them. Two things do: an accessory is paired to a lock
        (`masterSn`) and never reports a bolt (`openStatus`). Requiring both
        keeps a genuine slave lock -- which would report a bolt -- a lock, and
        can never swallow a top-level lock, which has no masterSn at all. A
        lock behind a gateway has a masterSn too, but reports its bolt.
        """
        return bool(self.master_sn) and "openStatus" not in self.raw

    @classmethod
    def from_device_record(cls, rec: dict[str, Any], queried_from: str) -> "Lock":
        # The lock's own dataCenter field is authoritative; fall back to the
        # datacenter we queried if it's missing/unknown.
        code = constants.datacenter_code_for(rec.get("dataCenter", ""), queried_from)
        return cls(
            esn=rec.get("wifiSN", ""),
            datacenter_code=code,
            user_number_id=int(rec.get("userNumberId", 0) or 0),
            nickname=rec.get("lockNickname", ""),
            online=str(rec.get("online", "1")) == "1",
            raw=rec,
        )

    def to_dict(self) -> dict[str, Any]:
        return {"esn": self.esn, "datacenter_code": self.datacenter_code,
                "user_number_id": self.user_number_id, "nickname": self.nickname,
                "online": self.online,
                "raw": {k: self.raw[k] for k in _CACHED_RAW_KEYS if k in self.raw}}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Lock":
        return cls(esn=d["esn"], datacenter_code=d["datacenter_code"],
                   user_number_id=d.get("user_number_id", 0),
                   nickname=d.get("nickname", ""), online=d.get("online", True),
                   raw=dict(d.get("raw") or {}))


@dataclass
class LockEvent:
    """A parsed realtime WebSocket event.

    kind:  setLock | lock | door | action | parts | <func>
    state: locked/unlocked (lock) or opened/closed (door) or None
    """
    kind: str
    lock_id: str
    state: str | None = None
    source: str | None = None  # remote | manual | None
    user_id: int | None = None
    battery: int | None = None
    msg_id: int | None = None   # per-delivery sequence number (differs on re-delivery)
    timestamp: str | None = None  # event time; timestamp+body identifies the event
    raw: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        bits = [self.kind, self.lock_id]
        if self.state:
            bits.append(self.state.upper())
        if self.source:
            bits.append(f"({self.source})")
        if self.battery is not None:
            bits.append(f"battery={self.battery}")
        return " ".join(bits)
