"""Runtime state: per-account token + device cache (gitignored).

State lives under <project>/state/<account-key>.json, keyed by account so
multiple accounts don't collide. This is NOT user config -- it's regenerable
cache (delete it and a re-login rebuilds it).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path
from typing import Any

STATE_DIR = Path(__file__).resolve().parent.parent / "state"


def account_key(identifier: str) -> str:
    """Filesystem-safe key for an account identifier (email)."""
    slug = re.sub(r"[^a-zA-Z0-9._-]", "_", identifier)[:40]
    digest = hashlib.sha1(identifier.encode()).hexdigest()[:8]
    return f"{slug}-{digest}" if identifier else "default"


def _path(identifier: str) -> Path:
    return STATE_DIR / f"{account_key(identifier)}.json"


def load(identifier: str) -> dict[str, Any]:
    p = _path(identifier)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save(identifier: str, data: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _path(identifier).write_text(json.dumps(data, indent=2), encoding="utf-8")


def clear(identifier: str) -> None:
    p = _path(identifier)
    if p.exists():
        p.unlink()


# -- async wrappers ---------------------------------------------------------
# The blocking disk I/O above must not run on an event loop (Home Assistant
# flags it). These run it in a worker thread; callers inside coroutines should
# use these instead of the sync functions.
async def async_load(identifier: str) -> dict[str, Any]:
    return await asyncio.to_thread(load, identifier)


async def async_save(identifier: str, data: dict[str, Any]) -> None:
    await asyncio.to_thread(save, identifier, data)


async def async_clear(identifier: str) -> None:
    await asyncio.to_thread(clear, identifier)
