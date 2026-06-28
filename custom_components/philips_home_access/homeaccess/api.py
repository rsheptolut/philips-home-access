"""HomeAccess facade: login, device discovery, and lock operations (async).

Resolves each lock to its datacenter (host + token) automatically, so callers
just use esns. This is the main entry point a Home Assistant integration wraps.

Use as an async context manager (owns an aiohttp session)::

    async with HomeAccess() as ha:
        await ha.async_discover()
        await ha.async_unlock(esn)

or inject HA's shared session: ``HomeAccess(session=async_get_clientsession(hass))``.
"""
from __future__ import annotations

import logging
from typing import Any

import aiohttp

from . import constants, state
from .models import Lock
from .realtime import Realtime
from .session import Account
from .settings import Settings, load as load_settings
from .transport import HttpClient

_LOGGER = logging.getLogger(__name__)


class HomeAccess:
    def __init__(self, settings: Settings | None = None,
                 session: aiohttp.ClientSession | None = None) -> None:
        self.settings = settings or load_settings()
        self._session = session
        self._own_session = session is None
        self.account: Account | None = None
        self._clients: dict[str, HttpClient] = {}
        # Device cache is loaded lazily off the event loop in _ensure().
        self._devices: list[Lock] = []
        self._cache_loaded = False

    # -- lifecycle ----------------------------------------------------------
    async def _ensure(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        if self.account is None:
            self.account = Account(self.settings, self._session)
            await self.account.async_load_state()
        if not self._cache_loaded:
            cached = (await state.async_load(self.settings.identifier)
                      ).get("devices") or []
            self._devices = [Lock.from_dict(d) for d in cached]
            self._cache_loaded = True

    async def aclose(self) -> None:
        if self._own_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> "HomeAccess":
        await self._ensure()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # -- auth ---------------------------------------------------------------
    async def async_login(self) -> None:
        await self._ensure()
        await self.account.async_login()

    async def async_verify_credentials(self) -> str:
        """Log in and return the account uid (for the HA config flow).

        Raises AuthError on bad credentials, HomeAccessConnectionError on a
        transient network failure.
        """
        await self.async_login()
        return self.account.uid

    async def _ensure_logged_in(self) -> None:
        await self._ensure()
        if not self.account.tokenset:
            await self.account.async_login()

    # -- transport per datacenter -------------------------------------------
    def client(self, datacenter_code: str) -> HttpClient:
        c = self._clients.get(datacenter_code)
        if c is None:
            dc = constants.DATACENTERS[datacenter_code]
            c = HttpClient(
                dc["api_base"],
                token_provider=lambda code=datacenter_code: self.account.async_token_for(code),
                reauth=self.account.async_login,
                session=self._session,
                language=self.settings.language,
                verify=self.settings.verify_tls,
                debug_proxy=self.settings.debug_proxy,
            )
            self._clients[datacenter_code] = c
        return c

    # -- devices ------------------------------------------------------------
    async def async_discover(self) -> list[Lock]:
        """Query every datacenter we hold a token for; enumerate all locks."""
        await self._ensure_logged_in()
        codes = (self.settings.datacenter and [self.settings.datacenter]
                 or self.account.datacenter_codes())
        found: dict[str, Lock] = {}
        for code in codes:
            if code not in constants.DATACENTERS:
                continue
            resp = await self.client(code).post(constants.DEVICE_LIST_PATH,
                                                json={"uid": self.account.uid})
            wifi = (resp.get("data") or {}).get("wifiList") or []
            for rec in wifi:
                lk = Lock.from_device_record(rec, code)
                prev = found.get(lk.esn)
                # The same lock can appear in several datacenters' lists; keep
                # the copy fetched from its own (true) datacenter.
                if prev is None or (lk.datacenter_code == code
                                    and prev.datacenter_code != code):
                    found[lk.esn] = lk
        self._devices = list(found.values())
        await self._cache_devices()
        return self._devices

    async def async_locks(self, refresh: bool = False) -> list[Lock]:
        if refresh or not self._devices:
            return await self.async_discover()
        return self._devices

    async def async_get(self, esn: str) -> Lock:
        for l in await self.async_locks():
            if l.esn == esn:
                return l
        raise KeyError(f"Lock {esn} not found for this account")

    async def _cache_devices(self) -> None:
        data = await state.async_load(self.settings.identifier)
        data["devices"] = [l.to_dict() for l in self._devices]
        await state.async_save(self.settings.identifier, data)

    # -- operations ---------------------------------------------------------
    async def async_unlock(self, esn: str) -> dict[str, Any]:
        """open-device -> physically UNLOCKS the lock."""
        l = await self.async_get(esn)
        return await self.client(l.datacenter_code).post_encrypted(
            constants.OPEN_DEVICE_PATH, {"esn": esn, "userNumberId": l.user_number_id})

    async def async_lock(self, esn: str) -> dict[str, Any]:
        """close-device -> physically LOCKS the lock."""
        l = await self.async_get(esn)
        return await self.client(l.datacenter_code).post_encrypted(
            constants.CLOSE_DEVICE_PATH, {"esn": esn, "userNumberId": l.user_number_id})

    async def async_status(self, esn: str) -> Lock:
        """Refresh and return the lock (use .open_status / .door / .battery)."""
        l = await self.async_get(esn)
        resp = await self.client(l.datacenter_code).post(
            constants.DEVICE_LIST_PATH, json={"uid": self.account.uid})
        for rec in (resp.get("data") or {}).get("wifiList") or []:
            if rec.get("wifiSN") == esn:
                updated = Lock.from_device_record(rec, l.datacenter_code)
                self._devices = [updated if d.esn == esn else d for d in self._devices]
                await self._cache_devices()
                return updated
        return l

    async def async_query_attr(self, esn: str) -> dict[str, Any]:
        l = await self.async_get(esn)
        return await self.client(l.datacenter_code).post_signed(
            constants.QUERY_ATTR_PATH, {"esn": esn})

    async def async_dtim_wake(self, esn: str) -> dict[str, Any]:
        l = await self.async_get(esn)
        return await self.client(l.datacenter_code).post_signed(
            constants.DTIM_WAKE_PATH, {"esnList": [esn]})

    # -- realtime -----------------------------------------------------------
    def realtime(self, datacenter_code: str = constants.DEFAULT_DATACENTER) -> Realtime:
        """Build a Realtime listener for a datacenter (call after login/discover)."""
        return Realtime(self.account, self._session, datacenter_code)
