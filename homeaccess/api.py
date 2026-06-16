"""HomeAccess facade: login, device discovery, and lock operations.

Resolves each lock to its datacenter (host + token) automatically, so callers
just use esns. This is the main entry point a Home Assistant integration wraps.
"""
from __future__ import annotations

from typing import Any

from . import constants, state
from .models import Lock
from .session import Account
from .settings import Settings, load as load_settings
from .transport import HttpClient


class HomeAccess:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or load_settings()
        self.account = Account(self.settings)
        self._clients: dict[str, HttpClient] = {}
        cached = state.load(self.settings.identifier).get("devices") or []
        self._devices: list[Lock] = [Lock.from_dict(d) for d in cached]

    # -- auth ---------------------------------------------------------------
    def login(self) -> None:
        self.account.login()

    def _ensure_login(self) -> None:
        if not self.account.tokenset:
            self.account.login()

    # -- transport per datacenter -------------------------------------------
    def client(self, datacenter_code: str) -> HttpClient:
        c = self._clients.get(datacenter_code)
        if c is None:
            dc = constants.DATACENTERS[datacenter_code]
            c = HttpClient(
                dc["api_base"],
                token_provider=lambda code=datacenter_code: self.account.token_for(code),
                reauth=self.account.relogin,
                language=self.settings.language,
                verify=self.settings.verify_tls,
                debug_proxy=self.settings.debug_proxy,
            )
            self._clients[datacenter_code] = c
        return c

    # -- devices ------------------------------------------------------------
    def discover(self) -> list[Lock]:
        """Query every datacenter we hold a token for; enumerate all locks."""
        self._ensure_login()
        codes = self.settings.datacenter and [self.settings.datacenter] \
            or self.account.datacenter_codes()
        found: dict[str, Lock] = {}
        for code in codes:
            if code not in constants.DATACENTERS:
                continue
            resp = self.client(code).post(constants.DEVICE_LIST_PATH,
                                          json={"uid": self.account.uid})
            wifi = (resp.json().get("data") or {}).get("wifiList") or []
            for rec in wifi:
                lk = Lock.from_device_record(rec, code)
                prev = found.get(lk.esn)
                # The same lock can appear in several datacenters' lists; keep the
                # copy fetched from its own (true) datacenter.
                if prev is None or (lk.datacenter_code == code
                                    and prev.datacenter_code != code):
                    found[lk.esn] = lk
        self._devices = list(found.values())
        self._cache_devices()
        return self._devices

    def locks(self, refresh: bool = False) -> list[Lock]:
        if refresh or not self._devices:
            return self.discover()
        return self._devices

    def lock(self, esn: str) -> Lock:
        for l in self.locks():
            if l.esn == esn:
                return l
        raise KeyError(f"Lock {esn} not found for this account")

    def _cache_devices(self) -> None:
        data = state.load(self.settings.identifier)
        data["devices"] = [l.to_dict() for l in self._devices]
        state.save(self.settings.identifier, data)

    # -- operations ---------------------------------------------------------
    def unlock(self, esn: str) -> dict[str, Any]:
        """open-device -> physically UNLOCKS the lock."""
        l = self.lock(esn)
        return self.client(l.datacenter_code).post_encrypted(
            constants.OPEN_DEVICE_PATH,
            {"esn": esn, "userNumberId": l.user_number_id}).json()

    def lock_device(self, esn: str) -> dict[str, Any]:
        """close-device -> physically LOCKS the lock."""
        l = self.lock(esn)
        return self.client(l.datacenter_code).post_encrypted(
            constants.CLOSE_DEVICE_PATH,
            {"esn": esn, "userNumberId": l.user_number_id}).json()

    def status(self, esn: str) -> Lock:
        """Refresh and return the lock (use .open_status for locked/unlocked)."""
        l = self.lock(esn)
        resp = self.client(l.datacenter_code).post(constants.DEVICE_LIST_PATH,
                                                   json={"uid": self.account.uid})
        wifi = (resp.json().get("data") or {}).get("wifiList") or []
        for rec in wifi:
            if rec.get("wifiSN") == esn:
                updated = Lock.from_device_record(rec, l.datacenter_code)
                self._devices = [updated if d.esn == esn else d for d in self._devices]
                self._cache_devices()
                return updated
        return l

    def query_attr(self, esn: str) -> dict[str, Any]:
        l = self.lock(esn)
        return self.client(l.datacenter_code).post_signed(
            constants.QUERY_ATTR_PATH, {"esn": esn}).json()

    def dtim_wake(self, esn: str) -> dict[str, Any]:
        l = self.lock(esn)
        return self.client(l.datacenter_code).post_signed(
            constants.DTIM_WAKE_PATH, {"esnList": [esn]}).json()
