# Philips Home Access — Home Assistant integration

Control and monitor a Philips Home Access Wi-Fi smart lock from Home Assistant.

This integration talks to the official cloud, but without the need to involve the official app. Your lock need connection to the internet (directly or via Wifi gateway).

## Features

- **Lock** — lock/unlock, with `locking…` / `unlocking…` transitions (confirmed
  by realtime events, or by a quick follow-up poll where there are none).
- **State-only locks** — some locks can't be locked or unlocked remotely (the
  Philips app shows no buttons for them). Mark those under Settings → Devices &
  services → Philips Home Access → **Configure**, and they get a read-only lock
  sensor (locked/unlocked) instead of a lock you can't use.
- **Door** binary sensor — open/closed, from the magnetic contact.
- **Battery** — the lock's, plus the door sensor's own where one is fitted.
- **Real-time updates** over the cloud WebSocket (North American data center only): app, keypad and manual operations appear within seconds, with periodic poll as backup.
- **Auto-discovery** — every lock on the account becomes its own device. A paired door sensor is treated as an accessory of its lock.
- **Locks behind a Wi-Fi gateway** (Bluetooth locks) — each lock is a device
  under its gateway, and commands go through the gateway. This is **unverified**:
  I don't own one. If you do, please report how it went in
  [issue #3](https://github.com/rsheptolut/philips-home-access/issues/3), ideally
  with debug logs (see below).
- **Reauth** — reauthenticates as needed, prompts for the password if it changes.

## Install

### Step 1

**HACS:** ⋮ → Search for "Philips Home Access" → click Download → restart Home Assistant.

**Manual:** copy `custom_components/philips_home_access/` into
`config/custom_components/` and restart.

### Step 2

Go to Settings → Devices & Services → Add Integration → Philips Home Access and enter the account email, password, and the phone country code of the country you selected at signup, digits only (`1` = US/Canada, `61` = Australia, `65` = Singapore). Locks that you previously linked to the app should get discovered automatically.

### Use a dedicated account (required)

The Philips cloud allows **one signed-in session per account**: every sign-in
signs out the one before it. If Home Assistant and the Philips app share an
account, they keep signing each other out. Home Assistant then signs back in
(at most every 10 minutes in the background, and it logs a warning when this
happens), which signs your app out again.

So create a second account, share the lock with it from the Philips app, and
give Home Assistant those credentials. Check the shared account can actually
lock and unlock: "family" usually can, "guest" may not.

It is also the safer setup. Home Assistant stores the password in
`.storage/core.config_entries` as plaintext, as it does for every integration
that needs one (the cloud session expires every ~2 h with no refresh token, so
the password is needed to sign in again), and a shared account can be revoked
at any time without touching your main one.

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
  hosts are untested. If one answers with something that isn't the API (as a
  Singapore host did in [issue #1](https://github.com/rsheptolut/philips-home-access/issues/1)),
  the command is retried on the North America host, which other integrations use
  for every region.
- **Datacenters are learned from the cloud.** New ones (like
  `PhilipsNorthAmericaNew`) are picked up at startup and polled; realtime stays
  off for them until verified.
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
python -m homeaccess datacenters             # the cloud's datacenter list
python -m homeaccess mqtt-watch              # capture Singapore MQTT pushes to a log file
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

Errors are typed (`AuthError`, `HomeAccessConnectionError`, `CommandError`); the library logs via
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
= lock / accessory / gateway `esn`, entity `unique_id` = `{esn}_lock` /
`{esn}_door` / `{esn}_battery`.

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
- Events are ordered by the lock's own sequence number (`msgId`) when they are
  close in time, and by its clock (`timestamp`) when further apart: the clock
  can run a few seconds backwards, and the sequence restarts when the lock
  loses power. After a battery swap the clock can also come back wrong (seen a
  day behind), so its events can be ignored as stale for a while; the poll
  still corrects the state.

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
