"""Account session: login -> per-datacenter tokens, with caching + reauth."""
from __future__ import annotations

import time

import requests
import urllib3

from . import constants, state, tokens
from .models import TokenSet
from .settings import Settings


class AuthError(RuntimeError):
    pass


class Account:
    """One Philips Home Access account. Owns the TokenSet and (re)login."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        if not settings.verify_tls:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        cached = state.load(settings.identifier)
        self.tokenset: TokenSet | None = (
            TokenSet.from_dict(cached["tokenset"]) if cached.get("tokenset") else None)

    # -- login --------------------------------------------------------------
    def login(self) -> TokenSet:
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
        resp = requests.post(url, json=body, headers=headers,
                             proxies=self._proxies(), verify=s.verify_tls, timeout=30)
        data = resp.json()
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
        return ts

    # -- token access -------------------------------------------------------
    def token_for(self, datacenter_code: str, *, auto: bool = True) -> str:
        """Return a valid token for a datacenter, logging in if needed."""
        tok = self.tokenset.token_for(datacenter_code) if self.tokenset else None
        if not tokens.is_valid(tok) and auto:
            self.login()
            tok = self.tokenset.token_for(datacenter_code) if self.tokenset else None
        if not tok:
            raise AuthError(f"No token for datacenter {datacenter_code}")
        return tok

    def relogin(self) -> bool:
        """Force a re-login (used as the transport's 444 reauth callback)."""
        try:
            self.login()
            return True
        except Exception:  # noqa: BLE001
            return False

    @property
    def uid(self) -> str:
        return self.tokenset.uid if self.tokenset else ""

    def datacenter_codes(self) -> list[str]:
        return list(self.tokenset.tokens) if self.tokenset else []

    # -- persistence (tokenset + device cache share one state file) ---------
    def _persist_tokenset(self) -> None:
        data = state.load(self.settings.identifier)
        data["tokenset"] = self.tokenset.to_dict() if self.tokenset else None
        state.save(self.settings.identifier, data)

    def _proxies(self) -> dict | None:
        p = self.settings.debug_proxy
        return {"http": p, "https": p} if p else None
