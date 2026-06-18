"""Command-line interface for the Philips Home Access client (async).

    python -m homeaccess login
    python -m homeaccess devices
    python -m homeaccess status  <esn>
    python -m homeaccess lock    <esn>
    python -m homeaccess unlock  <esn>
    python -m homeaccess watch   [datacenter] [--raw]   # stream events (read-only)
    python -m homeaccess monitor [esn]                  # stream events + send commands

Credentials come from env (HOMEACCESS_IDENTIFIER / HOMEACCESS_CREDENTIAL) or
homeaccess.toml. lock/unlock physically actuate the lock.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import deque

from rich.console import Console

from . import constants
from .api import HomeAccess
from .exceptions import AuthError, HomeAccessConnectionError
from .models import Datacenter, Lock, LockEvent
from .tracker import LockState, LockTracker

console = Console()


def _print_locks(locks: list[Lock]) -> None:
    for l in locks:
        console.print(f"  [bold]{l.esn}[/]  {l.nickname or '-':12} "
                      f"dc={l.datacenter_code}  user={l.user_number_id}  "
                      f"{'online' if l.online else 'offline'}  "
                      f"status={l.open_status or '?'}  "
                      f"battery={l.battery if l.battery is not None else '?'}")


def _make_event_printer(trackers: dict | None = None):
    """on_event handler backed by per-lock state trackers.

    Drops protocol re-deliveries (same timestamp+body), applies each event to the
    lock's tracker (newest-wins + out-of-order guard), shows discrete events
    always and `action`/`parts` only on change, and appends the tracked state.
    """
    trackers = trackers if trackers is not None else {}
    seen: deque = deque(maxlen=64)

    def printer(ev: LockEvent) -> None:
        key = (ev.timestamp, json.dumps(ev.raw.get("body"), sort_keys=True))
        if key in seen:
            return  # same event re-delivered (only msgId differed)
        seen.append(key)
        tr = trackers.get(ev.lock_id)
        if tr is None:
            tr = trackers[ev.lock_id] = LockTracker(LockState(ev.lock_id))
        res = tr.apply(ev)
        ts = time.strftime("%H:%M:%S")
        if res.stale:
            console.print(f"[dim]{ts} << {ev} (out-of-order, ignored)[/]")
            return
        if not (ev.kind in ("setLock", "lock", "door") or res.changes):
            return  # unchanged snapshot
        console.print(f"[dim]{ts}[/] [cyan]<< {ev}[/]  [dim]\\[{tr.state.summary()}][/]")

    return printer


_MONITOR_HELP = (
    "commands: [bold]u[/]nlock [esn] | [bold]l[/]ock [esn] | "
    "[bold]s[/]tatus [esn] | [bold]d[/]evices | [bold]h[/]elp | [bold]q[/]uit"
)


async def _monitor(ha: HomeAccess, esn_arg: str | None) -> None:
    locks = await ha.async_discover()
    if not locks:
        console.print("[yellow]No locks found on this account.[/]")
        return
    trackers = {l.esn: LockTracker(LockState(l.esn, bolt=l.open_status,
                                             door=l.door, battery=l.battery))
                for l in locks}
    target = esn_arg or (locks[0].esn if len(locks) == 1 else None)

    ws_dcs = sorted({l.datacenter_code for l in locks
                     if Datacenter.by_code(l.datacenter_code).ws_addr})
    if not ws_dcs:
        console.print("[yellow]None of your locks are in a WebSocket datacenter "
                      "(Singapore uses MQTT, not supported) -- commands only.[/]")
    printer = _make_event_printer(trackers)
    tasks = [asyncio.create_task(ha.realtime(code).listen(on_event=printer))
             for code in ws_dcs]
    for code in ws_dcs:
        console.print(f"[green]monitoring {code}[/] (events from any source: "
                      f"app, other devices, manual)")

    console.print("[bold]initial state[/] (from device list):")
    for l in locks:
        console.print(f"  {l.esn}: \\[{trackers[l.esn].state.summary()}]")
    console.print(f"[bold]target lock:[/] {target or '(specify per command)'}")
    console.print(_MONITOR_HELP)

    loop = asyncio.get_running_loop()
    try:
        while True:
            try:
                line = (await loop.run_in_executor(None, input, "> ")).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                continue
            parts = line.split()
            cmd, esn = parts[0].lower(), (parts[1] if len(parts) > 1 else target)
            if cmd in ("q", "quit", "exit"):
                break
            if cmd in ("h", "help", "?"):
                console.print(_MONITOR_HELP)
            elif cmd in ("d", "devices"):
                _print_locks(await ha.async_locks())
            elif cmd in ("s", "status"):
                t = trackers.get(esn) if esn else None
                console.print(f"  {esn}: {t.state.summary() if t else 'unknown'}"
                              if esn else "[red]need an esn[/]")
            elif cmd in ("u", "unlock", "open"):
                if not esn:
                    console.print("[red]need an esn[/]")
                else:
                    console.print(f"[yellow]>> unlock {esn}[/]")
                    await ha.async_unlock(esn)
            elif cmd in ("l", "lock", "close"):
                if not esn:
                    console.print("[red]need an esn[/]")
                else:
                    console.print(f"[yellow]>> lock {esn}[/]")
                    await ha.async_lock(esn)
            else:
                console.print(f"[red]unknown: {cmd}[/] -- {_MONITOR_HELP}")
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="homeaccess")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login")
    sub.add_parser("devices")
    for name in ("status", "lock", "unlock"):
        sub.add_parser(name).add_argument("esn")
    w = sub.add_parser("watch")
    w.add_argument("datacenter", nargs="?", default=constants.DEFAULT_DATACENTER)
    w.add_argument("--raw", action="store_true", help="dump full event JSON")
    sub.add_parser("monitor").add_argument("esn", nargs="?", default=None)
    args = p.parse_args(argv)

    async with HomeAccess() as ha:
        try:
            if args.cmd == "login":
                await ha.async_login()
                console.print(f"[green]Logged in[/] uid={ha.account.uid} "
                              f"datacenters={ha.account.datacenter_codes()}")
            elif args.cmd == "devices":
                _print_locks(await ha.async_discover())
            elif args.cmd == "status":
                l = await ha.async_status(args.esn)
                console.print(f"{l.esn}: [bold]{l.open_status or '?'}[/]  "
                              f"door={l.door or '?'}  "
                              f"battery={l.battery if l.battery is not None else '?'}")
            elif args.cmd == "unlock":
                console.print(f"[yellow]unlocking {args.esn}...[/]")
                console.print(await ha.async_unlock(args.esn))
            elif args.cmd == "lock":
                console.print(f"[yellow]locking {args.esn}...[/]")
                console.print(await ha.async_lock(args.esn))
            elif args.cmd == "watch":
                console.print(f"[bold]watching {args.datacenter}[/] (Ctrl+C to stop)")
                on_event = ((lambda e: print(json.dumps(e.raw))) if args.raw
                            else _make_event_printer())
                await ha.realtime(args.datacenter).listen(on_event=on_event)
            elif args.cmd == "monitor":
                await _monitor(ha, args.esn)
        except AuthError as e:
            console.print(f"[red]auth error:[/] {e}")
            return 1
        except HomeAccessConnectionError as e:
            console.print(f"[red]connection error:[/] {e}")
            return 1
        except KeyboardInterrupt:
            pass
    return 0


def run() -> int:
    """Sync entry point (console_scripts / python -m homeaccess)."""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    return asyncio.run(main())


if __name__ == "__main__":
    sys.exit(run())
