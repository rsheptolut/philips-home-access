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
from .exceptions import (
    CommandError,
    HomeAccessConnectionError,
    HomeAccessResponseError,
)
from .models import Lock
from .realtime import Realtime
from .session import Account
from .settings import Settings, load as load_settings
from .transport import HttpClient

_LOGGER = logging.getLogger(__name__)


def _normalize_mac(mac: str) -> str:
    """"aabbccddeeff" / "aa-bb-..." -> "AA:BB:CC:DD:EE:FF" (else unchanged)."""
    cleaned = mac.replace(" ", "").replace(":", "").replace("-", "").upper()
    if len(cleaned) != 12:
        return cleaned
    return ":".join(cleaned[i:i + 2] for i in range(0, 12, 2))


class HomeAccess:
    def __init__(self, settings: Settings | None = None,
                 session: aiohttp.ClientSession | None = None) -> None:
        self.settings = settings or load_settings()
        self._session = session
        self._own_session = session is None
        self.account: Account | None = None
        self._clients: dict[tuple[str, str], HttpClient] = {}
        # datacenter -> (token code, host code) a fallback command succeeded
        # with; tried first from then on (in memory only).
        self._command_routes: dict[str, tuple[str, str]] = {}
        # Device cache is loaded lazily off the event loop in _ensure().
        self._devices: list[Lock] = []
        self._cache_loaded = False
        # Datacenters a discovery actually found locks in. Once known, we poll
        # only these (login returns tokens for datacenters that hold no locks
        # for us and 500/return nothing). Reset to re-scan all if they go dry.
        self._active_codes: list[str] | None = None
        self._datacenters_checked = False
        self._unknown_codes_logged: set[str] = set()

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
    def client(self, datacenter_code: str, host_code: str | None = None) -> HttpClient:
        """HTTP client using `datacenter_code`'s token, on `host_code`'s host
        (the same datacenter's by default; see _command for why they differ)."""
        host_code = host_code or datacenter_code
        c = self._clients.get((datacenter_code, host_code))
        if c is None:
            dc = constants.DATACENTERS[host_code]
            c = HttpClient(
                dc["api_base"],
                token_provider=lambda code=datacenter_code: self.account.async_token_for(code),
                reauth=self.account.async_relogin,
                session=self._session,
                language=self.settings.language,
                verify=self.settings.verify_tls,
                debug_proxy=self.settings.debug_proxy,
            )
            self._clients[(datacenter_code, host_code)] = c
        return c

    # -- datacenters ----------------------------------------------------------
    async def _ensure_datacenters(self) -> None:
        """Learn datacenters the built-in map lacks, once per instance.

        Login can hand out tokens for datacenters added after this code was
        written (PhilipsNorthAmericaNew); without a host for them, their locks
        were silently skipped. The cloud's /datacenters list is cached in the
        state file, so a failed fetch still has the last good copy -- and the
        built-in map always remains.
        """
        if self._datacenters_checked:
            return
        self._datacenters_checked = True
        data = await state.async_load(self.settings.identifier)
        entries = data.get("datacenters") or []
        try:
            fetched = await self.account.async_fetch_datacenters()
        except HomeAccessConnectionError as e:
            _LOGGER.debug("could not fetch datacenters (%s); using cached/built-in", e)
        else:
            if fetched != entries:
                entries = fetched
                data["datacenters"] = fetched
                await state.async_save(self.settings.identifier, data)
        added = constants.register_datacenters(entries)
        if added:
            _LOGGER.info("datacenters learned from the cloud: %s",
                         {c: constants.DATACENTERS[c]["api_base"] for c in added})

    # -- devices ------------------------------------------------------------
    async def _scan(self, codes: list[str]) -> tuple[list[Lock], list[str]]:
        """Query device/list across `codes`; return (locks, codes-worth-polling).

        One datacenter failing (down, or a new one answering in a way we don't
        expect) must not hide the locks the others serve; only when every one
        fails is it an error.
        """
        found: dict[str, Lock] = {}
        hosts: list[str] = []      # datacenters that HOST a lock -> live data
        mirrors: list[str] = []    # datacenters that only echo someone else's
        errors: list[HomeAccessConnectionError] = []
        for code in codes:
            if code not in constants.DATACENTERS:
                if code not in self._unknown_codes_logged:
                    self._unknown_codes_logged.add(code)
                    _LOGGER.info("no host known for datacenter %s; its devices "
                                 "can't be listed", code)
                continue
            try:
                resp = await self.client(code).post(constants.DEVICE_LIST_PATH,
                                                    json={"uid": self.account.uid})
            except HomeAccessConnectionError as e:
                _LOGGER.debug("device/list on %s failed: %s", code, e)
                errors.append(e)
                continue
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
        if errors and not found and len(errors) == sum(
                c in constants.DATACENTERS for c in codes):
            raise errors[0]
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
        await self._ensure_datacenters()
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
        return await self._command(esn, open_=True)

    async def async_lock(self, esn: str) -> dict[str, Any]:
        """close-device -> physically LOCKS the lock."""
        return await self._command(esn, open_=False)

    def gateway_of(self, lock: Lock) -> Lock | None:
        """The gateway `lock` talks through, or None for a direct (Wi-Fi) lock."""
        if not lock.master_sn:
            return None
        return next((d for d in self._devices
                     if d.esn == lock.master_sn and d.is_gateway), None)

    def _command_request(self, l: Lock, open_: bool) -> tuple[str, dict[str, Any]]:
        """(path, params) for an open/close: direct locks take esn alone; a
        lock behind a gateway is addressed by its mac through the gateway."""
        gw = self.gateway_of(l)
        if gw is None:
            path = constants.OPEN_DEVICE_PATH if open_ else constants.CLOSE_DEVICE_PATH
            return path, {"esn": l.esn, "userNumberId": l.user_number_id}
        path = constants.GATEWAY_OPEN_PATH if open_ else constants.GATEWAY_CLOSE_PATH
        return path, {"esn": l.esn, "mac": _normalize_mac(l.mac),
                      "masterSn": gw.esn, "userNumberId": l.user_number_id}

    def _fallback_routes(self, code: str) -> list[tuple[str, str]]:
        """(token code, host code) pairs to try after the lock's own host.

        Issue #1: a Singapore-homed lock's host answered open-device with an
        HTML 404. rjbogz's integration sends every command to the North America
        host and has working reports, so try that host with the lock's own
        token, then with the North America token.
        """
        na = constants.DEFAULT_DATACENTER
        if code == na:
            return []
        routes = [(code, na)]
        if self.account.tokenset and self.account.tokenset.token_for(na):
            routes.append((na, na))
        return routes

    async def _command(self, esn: str, open_: bool) -> dict[str, Any]:
        """Send an encrypted open/close; raise CommandError unless code 200.

        Accepted commands answer code 200 (observed live); anything else is the
        cloud refusing, and must not pass for success.

        Only a reply that is not JSON at all -- the host has no such endpoint,
        so no handler ever saw the command -- moves on to a fallback route; a
        JSON refusal from the lock's own host is final. Retrying is safe either
        way: open and close each name a target state, not a toggle.
        """
        l = await self.async_get(esn)
        path, params = self._command_request(l, open_)
        home = (l.datacenter_code, l.datacenter_code)
        learned = self._command_routes.get(l.datacenter_code)
        routes = [learned] if learned else []
        routes += [r for r in [home, *self._fallback_routes(l.datacenter_code)]
                   if r != learned]
        failures: list[str] = []
        for token_code, host_code in routes:
            fallback = (token_code, host_code) != home
            host = constants.DATACENTERS[host_code]["api_base"]
            _LOGGER.debug("%s %s via %s (token %s)", path, esn, host, token_code)
            try:
                # a fallback's 444 means "wrong token for this host", not an
                # expired session: don't re-login over it
                resp = await self.client(token_code, host_code).post_encrypted(
                    path, params, user_initiated=True, _reauth=not fallback)
            except HomeAccessResponseError as e:
                failures.append(f"{host} (token {token_code}): {e}")
                continue
            if str(resp.get("code")) == "200":
                if fallback and learned != (token_code, host_code):
                    _LOGGER.info("Commands for %s locks now go to %s with the %s "
                                 "token", l.datacenter_code, host, token_code)
                    self._command_routes[l.datacenter_code] = (token_code, host_code)
                return resp
            refusal = (f"{host} (token {token_code}): code={resp.get('code')!r} "
                       f"msg={resp.get('msg')!r}")
            if not fallback:
                raise CommandError(f"{path} for {esn} refused by {refusal}")
            failures.append(refusal)
        raise CommandError(f"{path} for {esn} failed on every route: "
                           + "; ".join(failures))

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

    async def async_check_token(self, datacenter_code: str) -> None:
        """Ask the cloud whether our token for `datacenter_code` is still live.

        A rejected token comes back as 444, which the transport answers with a
        (rate-limited) re-login -- so this both detects and repairs a session
        another login displaced.
        """
        await self.client(datacenter_code).post_signed(constants.CHECK_TOKEN_PATH, {})

    # -- realtime -----------------------------------------------------------
    def realtime(self, datacenter_code: str = constants.DEFAULT_DATACENTER) -> Realtime:
        """Build a Realtime listener for a datacenter (call after login/discover)."""
        return Realtime(self.account, self._session, datacenter_code,
                        check_token=lambda: self.async_check_token(datacenter_code))
