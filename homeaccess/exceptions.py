"""Typed exceptions, so callers (CLI, Home Assistant) can react appropriately.

HA mapping:
    AuthError                 -> ConfigEntryAuthFailed (start reauth flow)
    HomeAccessConnectionError -> ConfigEntryNotReady (retry with backoff)
"""
from __future__ import annotations


class HomeAccessError(Exception):
    """Base class for all homeaccess errors."""


class AuthError(HomeAccessError):
    """Login/credentials rejected, or no usable token. Permanent until creds change."""


class HomeAccessConnectionError(HomeAccessError):
    """Transient network/transport failure; retrying later may succeed."""
