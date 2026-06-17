"""Command-line interface for the Philips Home Access client.

    python -m homeaccess login
    python -m homeaccess devices
    python -m homeaccess status  <esn>
    python -m homeaccess lock    <esn>
    python -m homeaccess unlock  <esn>
    python -m homeaccess watch   [datacenter]      # stream events (read-only)
    python -m homeaccess monitor [esn]             # stream events + send commands

Credentials come from env (HOMEACCESS_IDENTIFIER / HOMEACCESS_CREDENTIAL) or
homeaccess.toml. open/close physically actuate the lock.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from collections import deque

from rich.console import Console

from . import constants
from .api import HomeAccess
from .models import Datacenter, LockEvent
from .realtime import Realtime
from .session import AuthError
from .tracker import LockState, LockTracker

console = Console()


def _print_locks(ha: HomeAccess) -> None:
    for l in ha.locks():
        console.print(f"  [bold]{l.esn}[/]  {l.nickname or '-':12} "
                      f"dc={l.datacenter_code}  user={l.user_number_id}  "
                      f"{'online' if l.online else 'offline'}  "
                      f"status={l.open_status or '?'}  "
                      f"battery={l.battery if l.battery is not None else '?'}")


def _make_event_printer(trackers: dict | None = None):
    """Thread-safe on_event handler backed by per-lock state trackers.

    - drops protocol re-deliveries (same timestamp+body, only msgId differs)
    - applies each event to the lock's tracker (newest-wins + out-of-order guard)
    - shows discrete events (setLock/lock/door) always; `action`/`parts` only
      when they actually change tracked state; flags out-of-order events
    - appends the resulting tracked state so you can watch it evolve
    """
    trackers = trackers if trackers is not None else {}
    guard = threading.Lock()
    seen: deque = deque(maxlen=64)

    def printer(ev: LockEvent) -> None:
        with guard:
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
            console.print(f"[dim]{ts}[/] [cyan]<< {ev}[/]  "
                          f"[dim]\\[{tr.state.summary()}][/]")

    return printer


_MONITOR_HELP = (
    "commands: [bold]u[/]nlock [esn] | [bold]l[/]ock [esn] | "
    "[bold]s[/]tatus [esn] | [bold]d[/]evices | [bold]h[/]elp | [bold]q[/]uit"
)


def _monitor(ha: HomeAccess, esn_arg: str | None) -> None:
    locks = ha.discover()
    if not locks:
        console.print("[yellow]No locks found on this account.[/]")
        return
    # Seed a state tracker per lock from the initial device snapshot.
    trackers = {l.esn: LockTracker(LockState(l.esn, bolt=l.open_status,
                                             door=l.door, battery=l.battery))
                for l in locks}
    target = esn_arg or (locks[0].esn if len(locks) == 1 else None)

    # Start a realtime listener for each datacenter that exposes a WebSocket.
    ws_dcs = sorted({l.datacenter_code for l in locks
                     if Datacenter.by_code(l.datacenter_code).ws_addr})
    if not ws_dcs:
        console.print("[yellow]None of your locks are in a WebSocket datacenter "
                      "(Singapore uses MQTT, not supported) -- commands only.[/]")
    printer = _make_event_printer(trackers)
    for code in ws_dcs:
        rt = Realtime(ha.account, code)
        threading.Thread(target=rt.listen, kwargs={"on_event": printer},
                         daemon=True).start()
        console.print(f"[green]monitoring {code}[/] (events from any source: "
                      f"app, other devices, manual)")

    console.print("[bold]initial state[/] (from device list):")
    for l in locks:
        console.print(f"  {l.esn}: \\[{trackers[l.esn].state.summary()}]")
    console.print(f"[bold]target lock:[/] {target or '(specify per command)'}")
    console.print(_MONITOR_HELP)
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        parts = line.split()
        cmd = parts[0].lower()
        esn = parts[1] if len(parts) > 1 else target
        if cmd in ("q", "quit", "exit"):
            break
        if cmd in ("h", "help", "?"):
            console.print(_MONITOR_HELP)
        elif cmd in ("d", "devices"):
            _print_locks(ha)
        elif cmd in ("s", "status"):
            if not esn:
                console.print("[red]need an esn[/]")
            else:
                t = trackers.get(esn)
                console.print(f"  {esn}: {t.state.summary() if t else 'unknown'}")
        elif cmd in ("u", "unlock", "open"):
            if not esn:
                console.print("[red]need an esn[/]")
            else:
                console.print(f"[yellow]>> unlock {esn}[/]")
                ha.unlock(esn)
        elif cmd in ("l", "lock", "close"):
            if not esn:
                console.print("[red]need an esn[/]")
            else:
                console.print(f"[yellow]>> lock {esn}[/]")
                ha.lock_device(esn)
        else:
            console.print(f"[red]unknown: {cmd}[/] -- {_MONITOR_HELP}")


def main(argv: list[str] | None = None) -> int:
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

    ha = HomeAccess()
    try:
        if args.cmd == "login":
            ha.login()
            console.print(f"[green]Logged in[/] uid={ha.account.uid} "
                          f"datacenters={ha.account.datacenter_codes()}")
        elif args.cmd == "devices":
            ha.discover()
            console.print("[bold]Locks:[/]")
            _print_locks(ha)
        elif args.cmd == "status":
            l = ha.status(args.esn)
            console.print(f"{l.esn}: [bold]{l.open_status or '?'}[/]  "
                          f"battery={l.battery if l.battery is not None else '?'}")
        elif args.cmd == "unlock":
            console.print(f"[yellow]unlocking {args.esn}...[/]")
            console.print(ha.unlock(args.esn))
        elif args.cmd == "lock":
            console.print(f"[yellow]locking {args.esn}...[/]")
            console.print(ha.lock_device(args.esn))
        elif args.cmd == "watch":
            rt = Realtime(ha.account, args.datacenter)
            console.print(f"[bold]watching {args.datacenter}[/] (Ctrl+C to stop)")
            if args.raw:
                import json
                rt.listen(on_event=lambda e: print(json.dumps(e.raw)))
            else:
                rt.listen(on_event=_make_event_printer())
        elif args.cmd == "monitor":
            _monitor(ha, args.esn)
    except AuthError as e:
        console.print(f"[red]{e}[/]")
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
