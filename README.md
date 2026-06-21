# Philips Home Access — Home Assistant integration

Control and monitor your **Philips Home Access** Wi-Fi smart lock from Home
Assistant. The lock is a Kaadas/iRevo device on the Juzi Wulian ("Oneness")
cloud; this integration talks to that cloud directly (no extra app or bridge),
with **real-time** state updates.

> Unofficial / reverse-engineered. Not affiliated with or endorsed by Philips,
> Versuni, Kaadas, or Juzi Wulian. Use at your own risk.

## Features

- **Lock** entity — lock / unlock, with `locking…` / `unlocking…` transitions.
- **Door** binary sensor — open / closed (the magnetic door contact).
- **Battery** sensor — lock battery level.
- **Real-time updates** over the cloud WebSocket — reflects app, keypad, and
  manual operations within seconds (plus a 5-minute safety poll).
- **Auto-discovery** — all locks on your account appear automatically; each
  becomes its own device.
- **Reauth** — prompts you to re-enter the password if it changes.

## Installation

### HACS (recommended)
1. HACS → ⋮ → **Custom repositories** → add this repo's URL, category **Integration**.
2. Install **Philips Home Access**, then **restart Home Assistant**.

### Manual
Copy `custom_components/philips_home_access/` into your HA `config/custom_components/`
and restart.

## Setup

**Settings → Devices & Services → Add Integration → "Philips Home Access"**, then
enter the **email** and **password** of a Philips Home Access account, and your
phone **area code** (used at signup, e.g. `61`). Locks are discovered
automatically.

## ⚠️ Account & credential security — please read

To stay signed in, Home Assistant **stores the email and password** you enter in
its config-entry storage (`.storage/core.config_entries`). Like every HA
integration that needs a password, this file is **plaintext on the HA host** (and
in backups). The account's cloud session token expires every ~2 hours, so the
password is needed to re-login automatically — it can't be avoided here.

**Recommendation: don't use your primary lock-owner account.** In the Philips
Home Access app, **share the lock with a secondary account** (a family/guest
user) and use *that* account's credentials in Home Assistant. You can **revoke
its access at any time** from the app, which limits the blast radius if your HA
host or a backup is ever exposed. (Check that the shared account's role can
actually lock/unlock — a "family" member usually can; a "guest" may be limited.)

Also: keep HA's remote access locked down as usual (strong password, 2FA). Home
Assistant Cloud / Nabu Casa only tunnels the HA UI — it does not expose this
integration or its credentials directly.

## How it works / limitations

- **Cloud-based** — requires internet; this is not a local (LAN/BLE) integration.
- **Real-time** is delivered over a WebSocket for North-America-region locks.
  Locks homed in a MQTT-only datacenter (e.g. Singapore) still work for commands
  and update via the 5-minute poll, but don't get instant pushes yet.
- **Battery** is reported coarsely by the lock (it tends to sit at 100% then step
  down), so don't expect a smooth percentage.

---

# Developer / library

The integration vendors a standalone async client, `homeaccess`, beneath the
component (`custom_components/philips_home_access/homeaccess/`) — single source,
ships with the integration, no PyPI dependency. It's also usable on its own (it
has a CLI), which is how the protocol was developed and tested.

## CLI / dev install

```powershell
pip install -e .              # exposes `homeaccess` (library lives under the component)
# credentials via env (or a gitignored homeaccess.toml):
$env:HOMEACCESS_IDENTIFIER='you@example.com'; $env:HOMEACCESS_CREDENTIAL='...'
python -m homeaccess devices                 # discover locks
python -m homeaccess monitor                 # live events + lock/unlock prompt
python -m homeaccess watch --raw             # dump raw event JSON
```

## Library (async)

```python
import asyncio
from homeaccess import HomeAccess

async def main():
    async with HomeAccess() as ha:            # owns an aiohttp session (HA injects its own)
        for lock in await ha.async_discover():
            print(lock.esn, lock.nickname, lock.open_status, lock.door, lock.battery)
        await ha.async_unlock(lock.esn)
        await ha.realtime().listen(on_event=lambda e: print(e))  # until cancelled

asyncio.run(main())
```

Errors are typed (`AuthError`, `HomeAccessConnectionError`); the library logs via
`logging` (no printing — `rich` is used only by the CLI).

| Module | Responsibility |
|--------|----------------|
| `constants` / `models` / `crypto` / `tokens` / `exceptions` | Pure: protocol facts, dataclasses, signing + encryption, token decode, error types. |
| `settings` / `state` | User config (env / `homeaccess.toml`) and the token/device cache. |
| `session` / `transport` | async `Account` (login → per-datacenter tokens, reauth) and `HttpClient` (signed/encrypted POST, 444-retry). |
| `api` | async `HomeAccess` facade: discovery, per-datacenter routing, lock ops, `realtime()`. |
| `realtime` / `tracker` | async WebSocket listener + event parsing; optional client-side state tracker. |
| `cli` | Command line. |

Identity scheme used by the HA integration: config entry = account `uid`,
device = lock `esn`, entity `unique_id` = `{esn}_lock` / `{esn}_door` / `{esn}_battery`.

## Tests

```powershell
pip install -e ".[test]"
python -m pytest tests -q     # offline; no network or captures needed
```

## How it was built

See [research/FINDINGS.md](research/FINDINGS.md) for the full protocol teardown
(APK → Hermes bundle → DEX/Kaadas SDK → request signing, command encryption, and
the realtime WebSocket).
