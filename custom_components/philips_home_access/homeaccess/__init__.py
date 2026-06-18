"""Philips Home Access cloud API client (reverse-engineered).

Quick start:
    from homeaccess import HomeAccess
    ha = HomeAccess()          # reads settings from env / homeaccess.toml
    ha.login()
    for lock in ha.discover():
        print(lock.esn, lock.nickname, lock.open_status)
    ha.unlock("RL21243710207")
"""
from .api import HomeAccess
from .exceptions import AuthError, HomeAccessConnectionError, HomeAccessError
from .models import Datacenter, Lock, LockEvent, TokenSet
from .realtime import Realtime, parse_event
from .session import Account
from .settings import Settings, load as load_settings
from .tracker import ApplyResult, LockState, LockTracker

__all__ = [
    "HomeAccess", "Account", "Realtime", "parse_event",
    "Settings", "load_settings", "Lock", "LockEvent", "TokenSet", "Datacenter",
    "LockTracker", "LockState", "ApplyResult",
    "HomeAccessError", "AuthError", "HomeAccessConnectionError",
]
