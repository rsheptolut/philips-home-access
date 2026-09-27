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
from .exceptions import CommandError
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
        # Datacenters a discovery actually found locks in. Once known, we poll
        # only these (login returns tokens for datacenters that hold no locks
        # for us and 500/return nothing). Reset to re-scan all if they go dry.
        self._active_codes: list[str] | None = None

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
    async def _scan(self, codes: list[str]) -> tuple[list[Lock], list[str]]:
        """Query device/list across `codes`; return (locks, codes-worth-polling)."""
        found: dict[str, Lock] = {}
        hosts: list[str] = []      # datacenters that HOST a lock -> live data
        mirrors: list[str] = []    # datacenters that only echo someone else's
        for code in codes:
            if code not in constants.DATACENTERS:
                continue
            resp = await self.client(code).post(constants.DEVICE_LIST_PATH,
                                                json={"uid": self.account.uid})
            wifi = (resp.get("data") or {}).get("wifiList") or []
            hosted = False
            for rec in wifi:
                lk = Lock.from_device_record(rec, code)
                # The same lock is echoed by several datacenters' lists, but
                # only the one that actually hosts it serves live data -- the
                # other copies are stale mirrors, and `online` in particular can
                # sit wrong there for hours. Keep the copy served BY the lock's
                # home datacenter: compare `code` (where THIS copy came from)
                # against the home the record names. Comparing a previous copy's
                # .datacenter_code cannot break the tie -- every copy names the
                # same home, whichever datacenter handed it to us.
                if lk.esn not in found or code == lk.datacenter_code:
                    found[lk.esn] = lk
                if code == lk.datacenter_code:
                    hosted = True
            if hosted:
                hosts.append(code)
            elif wifi:
                mirrors.append(code)
        # Poll only the datacenters that host a lock: a mirror costs a round-trip
        # per poll and its copy loses de-duplication anyway. Fall back to mirrors
        # if nothing claims to host, so the poll set is never empty (and a
        # re-homed lock is still reachable until the next full re-scan).
        return list(found.values()), hosts or mirrors

    async def async_discover(self) -> list[Lock]:
        """Enumerate all locks, polling only the datacenters that host them.

        Login hands back a token per datacenter, but a given account's locks
        live in only one (or a few) of them; the rest 500, return nothing, or
        echo a stale mirror of a lock homed elsewhere. We pin the hosting
        datacenter(s) after the first find and poll only those, re-scanning
        everything if they ever come back empty.
        """
        await self._ensure_logged_in()
        if self.settings.datacenter:
            codes = [self.settings.datacenter]
        else:
            codes = self._active_codes or self.account.datacenter_codes()
        devices, productive = await self._scan(codes)

        # Pinned set went dry (datacenter down? lock re-homed?) -> re-scan all.
        if not devices and not self.settings.datacenter and self._active_codes:
            _LOGGER.debug("pinned datacenter(s) %s returned no locks; re-scanning all",
                          self._active_codes)
            self._active_codes = None
            devices, productive = await self._scan(self.account.datacenter_codes())

        if productive and not self.settings.datacenter:
            if productive != self._active_codes:
                _LOGGER.debug("pinning device polling to datacenter(s): %s", productive)
            self._active_codes = productive
        self._devices = devices
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
        return await self._command(esn, constants.OPEN_DEVICE_PATH)

    async def async_lock(self, esn: str) -> dict[str, Any]:
        """close-device -> physically LOCKS the lock."""
        return await self._command(esn, constants.CLOSE_DEVICE_PATH)

    async def _command(self, esn: str, path: str) -> dict[str, Any]:
        """Send an encrypted open/close; raise CommandError unless code 200.

        Accepted commands answer code 200 (observed live); anything else is the
        cloud refusing, and must not pass for success.
        """
        l = await self.async_get(esn)
        resp = await self.client(l.datacenter_code).post_encrypted(
            path, {"esn": esn, "userNumberId": l.user_number_id})
        if str(resp.get("code")) != "200":
            raise CommandError(
                f"{path} for {esn} via {l.datacenter_code} refused: "
                f"code={resp.get('code')!r} msg={resp.get('msg')!r}")
        return resp

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
