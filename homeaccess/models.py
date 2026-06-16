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

    def token_for(self, datacenter_code: str) -> str | None:
        return self.tokens.get(datacenter_code)

    def to_dict(self) -> dict[str, Any]:
        return {"uid": self.uid, "tokens": self.tokens, "obtained": self.obtained}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TokenSet":
        return cls(uid=d.get("uid", ""), tokens=d.get("tokens", {}),
                   obtained=d.get("obtained", 0))


@dataclass
class Lock:
    """A smart lock under an account, tagged with the datacenter that owns it."""
    esn: str
    datacenter_code: str
    user_number_id: int = 0
    nickname: str = ""
    online: bool = True
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def open_status(self) -> str | None:
        return constants.OPEN_STATUS.get(self.raw.get("openStatus"))

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
                "online": self.online}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Lock":
        return cls(esn=d["esn"], datacenter_code=d["datacenter_code"],
                   user_number_id=d.get("user_number_id", 0),
                   nickname=d.get("nickname", ""), online=d.get("online", True))


@dataclass
class LockEvent:
    """A parsed realtime WebSocket event."""
    kind: str                 # setLock | record | action | <other>
    lock_id: str
    state: str | None = None  # locked | unlocked | None
    source: str | None = None  # remote | manual | None
    user_id: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        bits = [self.kind, self.lock_id]
        if self.state:
            bits.append(self.state.upper())
        if self.source:
            bits.append(f"({self.source})")
        return " ".join(bits)
