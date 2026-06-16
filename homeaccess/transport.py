"""HTTP transport for one datacenter: headers, token, signing, 444-retry."""
from __future__ import annotations

from typing import Any, Callable

import requests

from . import constants, crypto


def _is_auth_failure(resp: requests.Response) -> bool:
    try:
        return str(resp.json().get("code")) in constants.AUTH_FAIL_CODES
    except ValueError:
        return False


class HttpClient:
    """Talks to one datacenter's api_base.

    token_provider() returns the current token (called per request, so a reauth
    is picked up automatically). reauth() is called once on a 444 response; if it
    returns True the request is retried. Retrying is safe because the request
    `sign` is independent of the token.
    """

    def __init__(self, api_base: str, token_provider: Callable[[], str],
                 reauth: Callable[[], bool] | None = None, *,
                 language: str = constants.DEFAULT_LANGUAGE,
                 verify: bool = True, debug_proxy: str = "") -> None:
        self.api_base = api_base.rstrip("/")
        self._token_provider = token_provider
        self._reauth = reauth
        self.session = requests.Session()
        self.session.verify = verify
        self.session.headers.update({
            "User-Agent": constants.DEVICE_USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "k-tenant": constants.K_TENANT,
            "k-version": constants.K_VERSION,
            "k-signv": constants.K_SIGNV,
            "k-language": language,
        })
        if debug_proxy:
            self.session.proxies.update({"http": debug_proxy, "https": debug_proxy})

    def request(self, method: str, path: str, _reauth: bool = True,
                **kwargs: Any) -> requests.Response:
        url = path if path.startswith("http") else self.api_base + path
        self.session.headers[constants.TOKEN_HEADER] = self._token_provider()
        resp = self.session.request(method, url, **kwargs)
        if _reauth and self._reauth and _is_auth_failure(resp) and self._reauth():
            self.session.headers[constants.TOKEN_HEADER] = self._token_provider()
            resp = self.session.request(method, url, **kwargs)
        return resp

    def post(self, path: str, **kw: Any) -> requests.Response:
        return self.request("POST", path, **kw)

    def post_signed(self, path: str, params: dict[str, Any] | None = None,
                    **kw: Any) -> requests.Response:
        return self.post(path, json=crypto.signed_body(params), **kw)

    def post_encrypted(self, path: str, params: dict[str, Any],
                       **kw: Any) -> requests.Response:
        body = crypto.encrypted_command_body(params)
        headers = {constants.ENCRYPT_DATA_HEADER: constants.ENCRYPT_DATA_HEADER}
        return self.post(path, json=body, headers=headers, **kw)
