"""Typed exceptions, so callers (CLI, Home Assistant) can react appropriately.

HA mapping:
    AuthError                 -> ConfigEntryAuthFailed (start reauth flow)
    HomeAccessConnectionError -> ConfigEntryNotReady (retry with backoff)
    CommandError / any of the above during lock/unlock -> HomeAssistantError
"""
from __future__ import annotations


class HomeAccessError(Exception):
    """Base class for all homeaccess errors."""


class AuthError(HomeAccessError):
    """Login/credentials rejected, or no usable token. Permanent until creds change."""


class HomeAccessConnectionError(HomeAccessError):
    """Transient network/transport failure; retrying later may succeed."""


class HomeAccessResponseError(HomeAccessConnectionError):
    """The cloud answered, but not with a JSON object (e.g. an HTML 404 page).

    A connection error so pollers treat it as transient; seen when a command is
    sent to a datacenter host that doesn't serve that endpoint.
    """


class CommandError(HomeAccessError):
    """The cloud refused a lock/unlock command (response code other than 200)."""
