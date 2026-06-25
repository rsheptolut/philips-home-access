"""HTTP transport for one datacenter: headers, token, signing, 444-reauth.

Async (aiohttp). Returns parsed JSON dicts (every endpoint responds JSON).
`token_provider` and `reauth` are awaitables supplied by the session, so a fresh
token (or a re-login) is picked up transparently.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

import aiohttp

from . import constants, crypto
from .exceptions import HomeAccessConnectionError

_LOGGER = logging.getLogger(__name__)


def _is_auth_failure(data: dict) -> bool:
    return str(data.get("code")) in constants.AUTH_FAIL_CODES


class HttpClient:
    """Talks to one datacenter's api_base.

    token_provider() -> current token (awaited per request, so reauth is seen).
    reauth()         -> re-login; raises on permanent/transient failure. Called
                        once on a 444 ("Not logged in") response, then retried.
    Retrying is safe because the request `sign` is independent of the token.
    """

    def __init__(self, api_base: str, token_provider: Callable[[], Awaitable[str]],
                 reauth: Callable[[], Awaitable[Any]] | None = None, *,
                 session: aiohttp.ClientSession,
                 language: str = constants.DEFAULT_LANGUAGE,
                 verify: bool = True, debug_proxy: str = "") -> None:
        self.api_base = api_base.rstrip("/")
        self._token_provider = token_provider
        self._reauth = reauth
        self._session = session
        self._ssl = None if verify else False
        self._proxy = debug_proxy or None
        self._headers = {
            "User-Agent": constants.DEVICE_USER_AGENT,
            "Accept": "application/json",
            "k-tenant": constants.K_TENANT,
            "k-version": constants.K_VERSION,
            "k-signv": constants.K_SIGNV,
            "k-language": language,
        }

    async def _headers_with_token(self, extra: dict | None) -> dict:
        token = await self._token_provider()
        return {**self._headers, constants.TOKEN_HEADER: token, **(extra or {})}

    async def _send(self, method: str, url: str, headers: dict, kwargs: dict) -> dict:
        try:
            async with self._session.request(
                method, url, headers=headers, ssl=self._ssl, proxy=self._proxy,
                **kwargs,
            ) as resp:
                return await resp.json(content_type=None)
        except aiohttp.ClientError as e:
            raise HomeAccessConnectionError(f"{method} {url} failed: {e}") from e

    async def request(self, method: str, path: str, *, headers: dict | None = None,
                      _reauth: bool = True, **kwargs: Any) -> dict:
        url = path if path.startswith("http") else self.api_base + path
        _LOGGER.debug("→ %s %s", method, path)
        data = await self._send(method, url, await self._headers_with_token(headers), kwargs)
        code = data.get("code") if isinstance(data, dict) else None
        _LOGGER.debug("← %s %s code=%s", method, path, code)
        if _reauth and self._reauth and _is_auth_failure(data):
            _LOGGER.info("Token rejected (444); re-authenticating")
            await self._reauth()  # raises AuthError / HomeAccessConnectionError
            data = await self._send(method, url, await self._headers_with_token(headers), kwargs)
            _LOGGER.debug("← %s %s code=%s (after reauth)", method, path,
                          data.get("code") if isinstance(data, dict) else None)
        return data

    async def post(self, path: str, **kw: Any) -> dict:
        return await self.request("POST", path, **kw)

    async def post_signed(self, path: str, params: dict[str, Any] | None = None,
                          **kw: Any) -> dict:
        return await self.post(path, json=crypto.signed_body(params), **kw)

    async def post_encrypted(self, path: str, params: dict[str, Any], **kw: Any) -> dict:
        body = crypto.encrypted_command_body(params)
        headers = {constants.ENCRYPT_DATA_HEADER: constants.ENCRYPT_DATA_HEADER}
        return await self.post(path, json=body, headers=headers, **kw)
