"""User settings: account credentials + a few options.

Loaded from environment variables, optionally overlaid on a local TOML file
(homeaccess.toml). Credentials never live in code or VCS -- see config.example.toml.

Precedence: environment variable > homeaccess.toml > built-in default.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from . import constants

# Search for homeaccess.toml in CWD then the project root (parent of package).
_TOML_CANDIDATES = [
    Path.cwd() / "homeaccess.toml",
    Path(__file__).resolve().parent.parent / "homeaccess.toml",
]


def _load_toml() -> dict:
    for p in _TOML_CANDIDATES:
        if p.exists():
            with open(p, "rb") as f:
                data = tomllib.load(f)
            return data.get("account", data)
    return {}


def _truthy(v: str) -> bool:
    return str(v).lower() not in ("0", "false", "no", "")


@dataclass
class Settings:
    identifier: str = ""          # account email
    credential: str = ""          # account password
    areacode: str = "61"
    language: str = constants.DEFAULT_LANGUAGE
    datacenter: str = ""          # optional pin; "" = use all/auto
    verify_tls: bool = True
    debug_proxy: str = ""         # optional http proxy for traffic inspection

    @property
    def has_credentials(self) -> bool:
        return bool(self.identifier and self.credential)


def load() -> Settings:
    toml = _load_toml()

    def pick(env: str, key: str, default):
        if env in os.environ:
            return os.environ[env]
        if key in toml:
            return toml[key]
        return default

    verify = pick("HOMEACCESS_VERIFY_TLS", "verify_tls", True)
    return Settings(
        identifier=pick("HOMEACCESS_IDENTIFIER", "identifier", ""),
        credential=pick("HOMEACCESS_CREDENTIAL", "credential", ""),
        areacode=str(pick("HOMEACCESS_AREACODE", "areacode", "61")),
        language=pick("HOMEACCESS_LANGUAGE", "language", constants.DEFAULT_LANGUAGE),
        datacenter=pick("HOMEACCESS_DATACENTER", "datacenter", ""),
        verify_tls=_truthy(verify) if isinstance(verify, str) else bool(verify),
        debug_proxy=pick("HOMEACCESS_DEBUG_PROXY", "debug_proxy", ""),
    )
