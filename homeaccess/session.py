"""Account session: login -> per-datacenter tokens, with caching + reauth.

Async (aiohttp). The aiohttp ClientSession is injected (the HA integration
passes HA's shared session); the CLI/api create and own one.
"""
from __future__ import annotations

import logging
import time

import aiohttp

from . import constants, state, tokens
from .exceptions import AuthError, HomeAccessConnectionError
from .models import TokenSet
from .settings import Settings

_LOGGER = logging.getLogger(__name__)


class Account:
    """One Philips Home Access account. Owns the TokenSet and (re)login."""

    def __init__(self, settings: Settings, session: aiohttp.ClientSession) -> None:
        self.settings = settings
        self._session = session
        cached = state.load(settings.identifier)
        self.tokenset: TokenSet | None = (
            TokenSet.from_dict(cached["tokenset"]) if cached.get("tokenset") else None)

    # -- login --------------------------------------------------------------
    async def async_login(self) -> TokenSet:
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
                data = await resp.json(content_type=None)
        except aiohttp.ClientError as e:
            raise HomeAccessConnectionError(f"login request failed: {e}") from e
        if str(data.get("code")) != "200":
            raise AuthError(f"Login failed: {data}")
        users = data["data"]["users"]
        ts = TokenSet(
            uid=users[0].get("uid", ""),
            tokens={u["code"]: u["token"] for u in users},
            obtained=int(time.time()),
        )
        self.tokenset = ts
        self._persist_tokenset()
        _LOGGER.info("Logged in as %s (datacenters: %s)",
                     s.identifier, list(ts.tokens))
        return ts

    # -- token access -------------------------------------------------------
    async def async_token_for(self, datacenter_code: str, *, auto: bool = True) -> str:
        """A valid token for a datacenter, logging in if missing/expired."""
        tok = self.tokenset.token_for(datacenter_code) if self.tokenset else None
        if not tokens.is_valid(tok) and auto:
            await self.async_login()
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
    def _persist_tokenset(self) -> None:
        data = state.load(self.settings.identifier)
        data["tokenset"] = self.tokenset.to_dict() if self.tokenset else None
        state.save(self.settings.identifier, data)
