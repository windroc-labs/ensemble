"""The manager side of skills/manage/scripts/ensemble, whose --help is its usage: hold a manager's
place while a command runs, list every manager, and resume a held manager's recorded client.
"""

import argparse
import contextlib
import os
import shlex
import sys
from collections.abc import Sequence
from dataclasses import replace

from ensemble.manager import Roster
from ensemble.manager.engine import managers
from ensemble.manager.engine.holder import Holder, inherited
from ensemble.manager.engine.types import (
    Held,
    Manager,
    Refused,
    RosterError,
    SessionRecord,
    Unread,
    Unreadable,
)
from ensemble.utils import nested_start, printable

NOT_HELD = "run ensemble resume under the manager's hold: ensemble list"


def recovery(manager: str, *, force: bool = False) -> str:
    """The ensemble command restarting manager's client under a new holder."""
    words = ("ensemble", *(("--force",) if force else ()), manager, "--")
    return f"{shlex.join(words)} <client resume command> (ensemble --help)"


def takeover(manager: str, holder: str | None) -> str:
    """What to do when holder, None when unknown, keeps manager's place."""
    commander = "the holding commander" if holder is None else f"{holder}'s commander"
    return f"use {commander}; only the user takes over, on purpose: {recovery(manager, force=True)}"


def refused(error: Held, roster: Roster) -> None:
    """Add the takeover to error, raised because another holder keeps roster's place."""
    with contextlib.suppress(OSError, RosterError):
        error.add_note(takeover(os.path.basename(roster.directory), roster.lease_state()[2]))


def hold(name: str, command: tuple[str, ...], *, force: bool) -> int:
    roster = managers.roster(name, f"invalid manager name {printable(name)}")
    try:
        return Holder(roster).run(command, session=name, force=force)
    except Held as error:
        refused(error, roster)
        raise


def _row(*fields: str) -> str:
    return "\t".join(printable(field).replace("\t", " ") for field in fields)


def _rows(found: list[Manager | Unreadable], out, *, numbered: bool) -> bool:
    """Print each manager's rows to out; whether any could not be read."""
    failed = False
    for number, manager in enumerate(found, 1):
        prefix = f"{number}) " if numbered else ""
        if isinstance(manager, Unreadable):
            print(prefix + _row(manager.name, "-", "-", "-", "error", manager.error), file=out)
            failed = True
            continue
        record = manager.record
        fields = [manager.name, manager.state]
        fields += [record.client, record.cwd] if isinstance(record, SessionRecord) else ["-", "-"]
        errors = [part.error for part in (record, manager.entries) if isinstance(part, Unread)]
        if errors:
            fields += ["error", "; ".join(errors)]
            failed = True
        print(prefix + _row(*fields), file=out)
        if isinstance(manager.entries, Unread):
            continue
        for name, entry in sorted(manager.entries.items()):
            kind = "-" if entry.record is None else entry.record.kind
            print("  " + _row(name, kind, entry.state), file=out)
        if not manager.entries:
            print("  -", file=out)
    return failed


def _choose(found: list[Manager | Unreadable]) -> Manager | Unreadable | None:
    names = [manager.name for manager in found]
    prompt = f"select a manager [1-{len(found)}], Enter cancels: "
    while True:
        try:
            print(prompt, end="", file=sys.stderr, flush=True)
            answer = sys.stdin.readline().strip()
        except KeyboardInterrupt:
            print(file=sys.stderr)
            return None
        if not answer:
            return None
        if answer.isdecimal() and 1 <= int(answer) <= len(found):
            return found[int(answer) - 1]
        if answer in names:
            return found[names.index(answer)]
        print(
            f"ensemble: no manager {printable(answer)}; give 1-{len(found)} or a name",
            file=sys.stderr,
        )


def _select(manager: Manager | Unreadable) -> int:
    record = manager.record if isinstance(manager, Manager) else None
    if isinstance(record, Unread):
        print(
            f"ensemble: manager {manager.name}: {record.error}; its session is not resumed",
            file=sys.stderr,
        )
    live = isinstance(record, SessionRecord) and managers.alive(record.pid)
    plan = managers.plan(manager, live=live)
    if isinstance(plan, Refused):
        print(
            f"ensemble: manager {manager.name}: {printable(plan.reason)}; nothing opened",
            file=sys.stderr,
        )
        return 1
    print(f"{plan.verb} {plan.name}")
    return 0


def listing(*, select: bool) -> int:
    out = sys.stderr if select else sys.stdout
    found = managers.managers(os.path.expanduser(managers.ROOT))
    if not found:
        print("no managers yet; start one: ensemble <name>", file=out)
        return 0
    failed = _rows(found, out, numbered=select)
    chosen = _choose(found) if select else None
    return _select(chosen) if chosen is not None else 1 if failed else 0


def resume(manager: str | None) -> int:
    """Replace this process with the held manager's recorded client; manager: else
    ENSEMBLE_MANAGER.
    """
    roster = managers.roster(manager or os.environ.get("ENSEMBLE_MANAGER"), NOT_HELD)
    lease = inherited()
    if lease is None:
        raise ValueError(NOT_HELD)
    roster.bind(lease)
    roster.check_lease()
    record, argv = managers.resume_target(roster.directory)
    os.chdir(record.cwd)
    # exec keeps this pid: the record names the client from its start, not only from its first turn
    with contextlib.suppress(OSError):
        managers.write_record(roster.directory, replace(record, pid=os.getpid()))
    os.execvp(argv[0], argv)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ensemble", description="The manager side of ensemble; usage: ensemble --help."
    )
    subs = parser.add_subparsers(dest="verb", required=True)
    holding = subs.add_parser("hold", usage="%(prog)s [--force] NAME -- COMMAND [ARG ...]")
    holding.add_argument("--force", action="store_true")
    holding.add_argument("name", metavar="NAME")
    subs.add_parser("list").add_argument("--select", action="store_true")
    subs.add_parser("resume")
    return parser


def _report(error: Exception) -> None:
    print(f"ensemble: {error}", file=sys.stderr)
    for note in getattr(error, "__notes__", ()):
        print(f"ensemble: {note}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    command: tuple[str, ...] = ()
    if arguments[:1] == ["hold"] and "--" in arguments:
        delimiter = arguments.index("--")
        arguments, command = arguments[:delimiter], tuple(arguments[delimiter + 1 :])
    parser = _parser()
    args = parser.parse_args(arguments)
    if args.verb == "hold" and not command:
        parser.error("hold requires -- followed by a command")
    if args.verb != "list" and (refused := nested_start()):
        print(f"ensemble: {refused}", file=sys.stderr)
        return 2
    try:
        if args.verb == "hold":
            return hold(args.name, command, force=args.force)
        if args.verb == "list":
            return listing(select=args.select)
        return resume(None)
    except ValueError as error:
        _report(error)
        return 2
    except (OSError, RosterError, RuntimeError) as error:
        _report(error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
