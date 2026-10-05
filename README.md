# Philips Home Access — Home Assistant integration

Control and monitor a Philips Home Access Wi-Fi smart lock from Home Assistant.

This integration talks to the official cloud, but without the need to involve the official app. Your lock need connection to the internet (directly or via Wifi gateway).

## Features

- **Lock** — lock/unlock, with `locking…` / `unlocking…` transitions.
- **Door** binary sensor — open/closed, from the magnetic contact.
- **Battery** — the lock's, plus the door sensor's own where one is fitted.
- **Real-time updates** over the cloud WebSocket (North American data center only): app, keypad and manual operations appear within seconds, with periodic poll as backup.
- **Auto-discovery** — every lock on the account becomes its own device. A paired door sensor is treated as an accessory of its lock.
- **Reauth** — reauthenticates as needed, prompts for the password if it changes.

## Install

### Step 1

**HACS:** ⋮ → Search for "Philips Home Access" → click Download → restart Home Assistant.

**Manual:** copy `custom_components/philips_home_access/` into
`config/custom_components/` and restart.

### Step 2

Go to Settings → Devices & Services → Add Integration → Philips Home Access and enter the account email, password, and the phone area code of the country you selected at signup (for example `61` is for Australia). Locks that you previously linked to the app should get discovered automatically.

### Use a secondary account

Home Assistant stores the password in `.storage/core.config_entries` as
plaintext, as it does for every integration that needs one. The cloud session
token expires every ~2 h and there is no refresh token, so the password is
needed to re-login.

Share the lock with a family/guest account in the Philips app and give Home
Assistant those credentials instead — you can revoke them at any time without
touching your main account. Check the shared account can actually lock and
unlock: "family" usually can, "guest" may not.

Also secure remote access to Home Assistant itself (strong password and maybe 2FA).
Home Assistant Cloud only tunnels the HA UI; it doesn't expose this
integration or its stored credentials directly.

## How it works / limitations

- **Cloud-based** — needs internet. This is not a local (LAN/BLE) integration.
- **Real-time is North America only.** Locks homed there get instant WebSocket
  pushes, with the poll as a 15-minute safety net that re-syncs on every
  reconnect. If the socket drops, or any device goes offline, polling speeds
  up to 1 minute until it is back.
- **Other datacenters are poll-only.** Singapore (MQTT) and Oneness have no push
  channel implemented in this integration, so they poll every 60 s. Commands still work, but state
  lags, and door open/close — an event-driven signal — may not show up reliably.
- **Commands are verified on North America only.** Other datacenters' command
  hosts are untested.
- **Battery is coarse** — it tends to sit at 100% for a long time, then step down. Property of the lock I'm using for testing.

## Debugging

Add this if you want to see more info in the logs.

```yaml
logger:
  logs:
    custom_components.philips_home_access: debug
```

Restart, reproduce, then check Settings → System → Logs. You get each HTTP
request/response, every raw WebSocket frame with its `msgId` and `timestamp`,
and how the tracker applied each event (`stale` / `dup` / `changes`, plus the
resulting state) — enough to reconstruct event ordering.

---

# Developer / library

The integration vendors a standalone async client, `homeaccess`, beneath the
component (`custom_components/philips_home_access/homeaccess/`) — single source,
ships with the integration, no PyPI dependency. It is also usable on its own via
a CLI, which is how the protocol was developed and tested.

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

Identity scheme used by the HA integration: config entry = account `uid`, device
= lock `esn`, entity `unique_id` = `{esn}_lock` / `{esn}_door` / `{esn}_battery`.

## Tests

```powershell
pip install -e ".[test]"
python -m pytest tests -q     # offline; no network or captures needed
```

## How it was built

See [research/FINDINGS.md](research/FINDINGS.md) for the full protocol teardown
(APK → Hermes bundle → DEX/Kaadas SDK → request signing, command encryption, and
the realtime WebSocket).

## Roadmap

- **MQTT realtime** for non-NA datacenters, so Singapore-homed locks get pushes
  too. It can't be built or tested without access to such an account, which I don't have at the present moment. Reach out if you want to help add this.
- **Local control over BLE.** The app has a BLE path (`createBleFrame`) with a
  cloud-negotiated session key — a stretch goal for no-cloud operation. But there's a bunch of downsides of pursuing that path.

## Known issues

- The signing key is static and embedded into the app (and this integration). So if the official app developer rotates it in an app update, it would need re-extracting from the new app version and updating this integration. If this happens to me I'll notice really quick and extract the key.
- `msgId` ordering assumes the cloud's sequence doesn't reset across a WebSocket reconnect (the poll self-heals if it does).

## Legal

Independent and unofficial; not affiliated with, authorized, or endorsed by anyone other than myself, an individual hobbyist and home automation enthusiast. Product and company names are the property of their owners and are used nominatively, to describe the hardware this interoperates with.

It was built by reverse-engineering the official app for interoperability —
letting hardware you own work with Home Assistant — and contains no code or other
copyrighted material from that app.

It connects using your own account credentials, which may breach the vendor's
terms of service; that is your call and your risk. Provided as is under the
[MIT License](LICENSE), and it drives a physical lock, so you own the
consequences of running it.

If you represent a rights holder and have a concern about this project, please
open an issue and it will be addressed promptly.
