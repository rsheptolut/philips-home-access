"""Account session: login -> per-datacenter tokens, with caching + reauth.

Async (aiohttp). The aiohttp ClientSession is injected (the HA integration
passes HA's shared session); the CLI/api create and own one.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import aiohttp

from . import constants, state, tokens
from .exceptions import AuthError, HomeAccessConnectionError, HomeAccessResponseError
from .models import TokenSet
from .settings import Settings

_LOGGER = logging.getLogger(__name__)

# Never log these values: session tokens, passwords, one-time codes.
_SECRET_KEYS = {"token", "credential", "password", "confirmCode", "adminPwd"}

# The cloud keeps ONE session per account: every login invalidates the tokens
# of the one before (ours, the phone app's, anyone's -- verified live, see
# research/FINDINGS.md). A re-login driven by background work (realtime
# reconnect, poll) would sign out whoever displaced us, who signs back in and
# displaces us again; so background re-logins wait this long after the last
# attempt. A user's lock/unlock bypasses the limit.
RELOGIN_MIN_INTERVAL = 600


def _redact(obj: Any) -> Any:
    """Copy of a cloud reply that is safe to log."""
    if isinstance(obj, dict):
        return {k: "***" if k in _SECRET_KEYS and v else _redact(v)
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


class Account:
    """One Philips Home Access account. Owns the TokenSet and (re)login."""

    def __init__(self, settings: Settings, session: aiohttp.ClientSession) -> None:
        self.settings = settings
        self._session = session
        # Cached tokenset is loaded lazily off the event loop (async_load_state).
        self.tokenset: TokenSet | None = None
        self._loaded = False
        # One login at a time: concurrent 444s (poll + command + realtime) each
        # asking for a login would, under single-session, kill each other's
        # fresh tokens in a cascade.
        self._login_lock = asyncio.Lock()
        self._last_login_attempt: float | None = None   # time.monotonic()
        self._warned_displaced: str | None = None        # token we warned about

    async def async_load_state(self) -> None:
        """Load the cached tokenset from disk (once, off the event loop)."""
        if self._loaded:
            return
        cached = await state.async_load(self.settings.identifier)
        if cached.get("tokenset"):
            self.tokenset = TokenSet.from_dict(cached["tokenset"])
        self._loaded = True

    # -- login --------------------------------------------------------------
    async def async_login(self) -> TokenSet:
        """Log in now (initial sign-in, config flow, CLI). Serialized."""
        async with self._login_lock:
            return await self._login()

    async def async_relogin(self, rejected_token: str | None = None, *,
                            user_initiated: bool = False) -> None:
        """Re-login after the cloud rejected `rejected_token` (a 444).

        Serialized, and skipped when another caller already replaced the
        rejected token while we waited for the lock -- one login serves every
        request that failed with the same token. Background callers are
        rate-limited (RELOGIN_MIN_INTERVAL); inside the window this raises
        HomeAccessConnectionError, a transient failure, never an auth one.
        """
        async with self._login_lock:
            if (rejected_token and self.tokenset
                    and rejected_token not in self.tokenset.tokens.values()):
                return  # someone re-logged in while we waited
            exp = tokens.token_exp(rejected_token) if rejected_token else None
            if (exp is not None and exp > time.time()
                    and rejected_token != self._warned_displaced):
                # rejected long before its expiry: another login replaced it
                self._warned_displaced = rejected_token
                _LOGGER.warning(
                    "Another client signed in to the Philips account %s; "
                    "re-signing in will sign it out. Use a dedicated account "
                    "for Home Assistant (see README).", self.settings.identifier)
            last = self._last_login_attempt
            if (not user_initiated and last is not None
                    and time.monotonic() - last < RELOGIN_MIN_INTERVAL):
                raise HomeAccessConnectionError(
                    f"re-login rate-limited (last attempt "
                    f"{time.monotonic() - last:.0f}s ago)")
            await self._login()

    async def _login(self) -> TokenSet:
        """The login request itself; callers hold _login_lock."""
        self._last_login_attempt = time.monotonic()
        s = self.settings
        if not s.has_credentials:
            raise AuthError("Missing credentials (set HOMEACCESS_IDENTIFIER / "
                            "HOMEACCESS_CREDENTIAL or homeaccess.toml).")
        url = constants.AUTH_BASE + constants.LOGIN_PATH
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": constants.LOGIN_USER_AGENT,
            "lang": s.language, "language": s.language,
            "reqSource": "app", "timestamp": str(int(time.time())),
        }
        body = {"identifier": s.identifier, "credential": s.credential,
                "areacode": s.areacode}
        try:
            async with self._session.post(
                url, json=body, headers=headers,
                ssl=None if s.verify_tls else False,
                proxy=s.debug_proxy or None,
            ) as resp:
                status = resp.status
                text = await resp.text()
        except aiohttp.ClientError as e:
            raise HomeAccessConnectionError(f"login request failed: {e}") from e
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if not isinstance(data, dict):
            _LOGGER.warning("Login: HTTP %s, unreadable reply: %r", status, text[:500])
            raise HomeAccessResponseError(
                f"login -> HTTP {status}, not a JSON object: {text[:120]!r}")

        code = data.get("code")
        if str(code) != "200":
            # The cloud's refusal is the only clue to *why* (1001 wrong password,
            # 1004 unknown account, ...) -- never swallow it.
            _LOGGER.warning("Login rejected for %s: HTTP %s, reply %s",
                            s.identifier, status, _redact(data))
            msg = data.get("msg") or data.get("errDes") or "no message"
            raise AuthError(f"Login failed: {_redact(data)}", code=code,
                            reason=f"{msg} (code {code})")
        _LOGGER.debug("Login: HTTP %s, reply %s", status, _redact(data))

        payload = data.get("data") if isinstance(data.get("data"), dict) else {}
        users = payload.get("users") or []
        if not users:
            # e.g. status 1 + confirmCode: the app then calls loginConfirm, a
            # second step this client does not implement yet.
            _LOGGER.warning("Login accepted but returned no session for %s "
                            "(status=%r, confirmCode present=%s): %s",
                            s.identifier, payload.get("status"),
                            bool(payload.get("confirmCode")), _redact(data))
            raise AuthError(
                f"Login returned no users: {_redact(data)}", code=code,
                reason=f"the account needs an extra sign-in step that isn't "
                       f"supported yet (status {payload.get('status')!r})")
        ts = TokenSet(
            uid=users[0].get("uid", ""),
            tokens={u["code"]: u["token"] for u in users},
            obtained=int(time.time()),
        )
        self.tokenset = ts
        await self._persist_tokenset()
        _LOGGER.info("Logged in as %s (datacenters: %s)",
                     s.identifier, list(ts.tokens))
        return ts

    async def async_fetch_datacenters(self) -> list[dict]:
        """The cloud's datacenter list (unauthenticated POST /datacenters).

        Raises HomeAccessConnectionError on any failure; callers fall back to
        the built-in map.
        """
        s = self.settings
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": constants.LOGIN_USER_AGENT,
            "lang": s.language, "language": s.language,
            "reqSource": "app", "timestamp": str(int(time.time())),
        }
        try:
            async with self._session.post(
                constants.AUTH_BASE + constants.DATACENTERS_PATH, json={},
                headers=headers, ssl=None if s.verify_tls else False,
                proxy=s.debug_proxy or None,
            ) as resp:
                text = await resp.text()
        except aiohttp.ClientError as e:
            raise HomeAccessConnectionError(f"datacenters request failed: {e}") from e
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        entries = data.get("data") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            raise HomeAccessResponseError(f"datacenters: unexpected reply {text[:120]!r}")
        return entries

    # -- token access -------------------------------------------------------
    async def async_token_for(self, datacenter_code: str, *, auto: bool = True) -> str:
        """A token for a datacenter, re-logging in only if it's provably expired.

        We don't re-login just because a token can't be decoded (opaque
        datacenter tokens) -- that would re-login on every poll for no gain. A
        server-side rejection (444) still drives a one-shot reauth in transport.
        """
        tok = self.tokenset.token_for(datacenter_code) if self.tokenset else None
        if auto and tokens.is_expired(tok):
            async with self._login_lock:
                # re-check: a caller ahead of us in the lock may have renewed it
                tok = self.tokenset.token_for(datacenter_code) if self.tokenset else None
                if tokens.is_expired(tok):
                    await self._login()
                    tok = self.tokenset.token_for(datacenter_code) if self.tokenset else None
        if not tok:
            raise AuthError(f"No token for datacenter {datacenter_code}")
        return tok

    @property
    def uid(self) -> str:
        return self.tokenset.uid if self.tokenset else ""

    def datacenter_codes(self) -> list[str]:
        return list(self.tokenset.tokens) if self.tokenset else []

    # -- persistence --------------------------------------------------------
    async def _persist_tokenset(self) -> None:
        data = await state.async_load(self.settings.identifier)
        data["tokenset"] = self.tokenset.to_dict() if self.tokenset else None
        await state.async_save(self.settings.identifier, data)
