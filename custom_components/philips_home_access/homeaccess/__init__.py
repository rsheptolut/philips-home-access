"""Philips Home Access cloud API client (reverse-engineered, async).

Quick start:
    import asyncio
    from homeaccess import HomeAccess

    async def main():
        async with HomeAccess() as ha:        # settings from env / homeaccess.toml
            for lock in await ha.async_discover():
                print(lock.esn, lock.nickname, lock.open_status, lock.battery)
            await ha.async_unlock(lock.esn)

    asyncio.run(main())
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
