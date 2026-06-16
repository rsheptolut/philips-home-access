"""Command-line interface for the Philips Home Access client.

    python -m homeaccess login
    python -m homeaccess devices
    python -m homeaccess status  <esn>
    python -m homeaccess lock    <esn>
    python -m homeaccess unlock  <esn>
    python -m homeaccess watch   [datacenter]

Credentials come from env (HOMEACCESS_IDENTIFIER / HOMEACCESS_CREDENTIAL) or
homeaccess.toml. open/close physically actuate the lock.
"""
from __future__ import annotations

import argparse
import sys

from rich.console import Console

from . import constants
from .api import HomeAccess
from .models import LockEvent
from .realtime import Realtime
from .session import AuthError

console = Console()


def _print_locks(ha: HomeAccess) -> None:
    for l in ha.locks():
        console.print(f"  [bold]{l.esn}[/]  {l.nickname or '-':12} "
                      f"dc={l.datacenter_code}  user={l.user_number_id}  "
                      f"{'online' if l.online else 'offline'}  "
                      f"status={l.open_status or '?'}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="homeaccess")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login")
    sub.add_parser("devices")
    for name in ("status", "lock", "unlock"):
        sp = sub.add_parser(name)
        sp.add_argument("esn")
    sw = sub.add_parser("watch")
    sw.add_argument("datacenter", nargs="?", default=constants.DEFAULT_DATACENTER)
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
            console.print(f"{l.esn}: [bold]{l.open_status or '?'}[/] "
                          f"(openStatus={l.raw.get('openStatus')}, "
                          f"power={l.raw.get('power')})")
        elif args.cmd == "unlock":
            console.print(f"[yellow]unlocking {args.esn}...[/]")
            console.print(ha.unlock(args.esn))
        elif args.cmd == "lock":
            console.print(f"[yellow]locking {args.esn}...[/]")
            console.print(ha.lock_device(args.esn))
        elif args.cmd == "watch":
            rt = Realtime(ha.account, args.datacenter)
            console.print(f"[bold]watching {args.datacenter}[/] (Ctrl+C to stop)")

            def show(ev: LockEvent) -> None:
                console.print(f"  [cyan]{ev}[/]")

            rt.listen(on_event=show)
    except AuthError as e:
        console.print(f"[red]{e}[/]")
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
