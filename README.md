# homeaccess — Philips Home Access cloud API client

A reverse-engineered Python client for the Philips Home Access smart lock
(Kaadas/iRevo on the Juzi Wulian "Oneness" platform). Handles login, device
discovery, lock/unlock, and realtime state — the foundation for a Home Assistant
integration. See [research/FINDINGS.md](research/FINDINGS.md) for the full
protocol teardown.

## Install

```powershell
cd D:\claude\philips
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -e .            # or: pip install -r requirements.txt
```

## Configure

Set credentials via environment variables:

```powershell
$env:HOMEACCESS_IDENTIFIER = 'you@example.com'
$env:HOMEACCESS_CREDENTIAL = 'your-password'
$env:HOMEACCESS_AREACODE   = '61'
```

…or copy `config.example.toml` to `homeaccess.toml` (gitignored) and fill it in.
Env vars take precedence. Optional: `HOMEACCESS_DATACENTER` (pin a region),
`HOMEACCESS_DEBUG_PROXY` (route traffic through an intercept proxy),
`HOMEACCESS_VERIFY_TLS`.

## CLI

```powershell
python -m homeaccess login            # log in, cache tokens
python -m homeaccess devices          # discover all locks (auto-routes per datacenter)
python -m homeaccess status  <esn>    # locked / unlocked
python -m homeaccess unlock  <esn>    # physically unlocks
python -m homeaccess lock    <esn>    # physically locks
python -m homeaccess watch [datacenter]   # stream realtime events
```

## Library

```python
from homeaccess import HomeAccess, Realtime

ha = HomeAccess()                 # settings from env / homeaccess.toml
for lock in ha.discover():
    print(lock.esn, lock.nickname, lock.open_status)

ha.unlock("RL21243710207")
print(ha.status("RL21243710207").open_status)

# realtime events (locked/unlocked, remote vs manual)
Realtime(ha.account).listen(on_event=lambda e: print(e))
```

## Architecture

| Module | Responsibility |
|--------|----------------|
| `constants.py` | Fixed protocol facts: hosts, paths, headers, datacenter map, embedded keys, event codes. |
| `settings.py`  | User config (env / `homeaccess.toml`). |
| `tokens.py`    | Token decode + expiry. |
| `crypto.py`    | Request signing (`sign256`) + lock-command encryption. |
| `state.py`     | Per-account token + device cache (gitignored `state/`). |
| `session.py`   | `Account`: login → per-datacenter tokens, reauth. |
| `transport.py` | `HttpClient` per datacenter: headers, token, signed/encrypted POST, 444-retry. |
| `api.py`       | `HomeAccess` facade: discovery, routing, lock ops. |
| `realtime.py`  | WebSocket listener + event parsing. |
| `tracker.py`   | Optional client-side state tracker (bolt/door/battery/pending, newest-wins + out-of-order guard). |
| `cli.py`       | Command line. |

Key design point: one account login yields **one token per datacenter**; each
lock is routed to its own datacenter's host+token automatically. Multiple locks
share auth — only multiple *accounts* would need separate logins.

## Tests

```powershell
pip install -e ".[test]"
python -m pytest tests -q     # offline; no network or captures needed
```

## Notes

- Tokens last ~2h; the client caches them and **re-logs-in automatically** when
  the server rejects one (code `444`).
- Reverse-engineering artifacts (APK, decompiled output, captures) are kept out
  of the repo by design; only `research/FINDINGS.md` is retained.
