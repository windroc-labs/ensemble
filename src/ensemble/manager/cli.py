"""ensemble-crew: command-based launches and a manager's persistent roster."""

import argparse
import os
import re
import shlex
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path

from ensemble.manager import Crew, Roster, RosterError, Spec, State
from ensemble.manager.engine import managers
from ensemble.manager.engine.holder import Holder
from ensemble.manager.engine.types import (
    Lease,
    Manager,
    Refused,
    SessionRecord,
    StaleHolder,
    StartBusy,
    StatusError,
    Unread,
    Unreadable,
)
from ensemble.utils import EXIT_BUSY, STATE, LayerCommand, nested_start, printable

LEASE_FREE = ("env", "-u", "ENSEMBLE_HOLDER_ID", "-u", "ENSEMBLE_HOLDER_GENERATION")
ENDED = "this environment's ENSEMBLE_HOLDER_ID/ENSEMBLE_HOLDER_GENERATION name an ended holder"


def _number(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("want a non-negative integer") from error
    if number < 0:
        raise argparse.ArgumentTypeError("want a non-negative integer")
    return number


def _sid(value: str) -> str:
    try:
        identity = uuid.UUID(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("want a canonical UUID4") from error
    if identity.version != 4 or str(identity) != value:
        raise argparse.ArgumentTypeError("want a canonical UUID4")
    return value


def _after(value: str) -> int | None:
    return None if value == "end" else _number(value)


def _roster(args) -> Roster:
    manager = args.manager or os.environ.get("ENSEMBLE_MANAGER")
    if not manager or not managers.NAME.fullmatch(manager):
        raise ValueError("give --manager NAME or set ENSEMBLE_MANAGER to a manager name")
    directory = Path(os.path.expanduser(managers.ROOT)) / manager
    return Roster(str(directory))


def _named(manager: str, option: str = "--manager") -> tuple[str, ...]:
    return (f"{option}={manager}",) if manager.startswith("-") else (option, manager)


def _restart(manager: str, *force: str) -> str:
    hold = ("ensemble-crew", "hold", *force, *_named(manager), *_named(manager, "--session"), "--")
    return f"{shlex.join(hold)} <client resume command> (ensemble-crew hold --help)"


def _lease_free(*words: str) -> str:
    return shlex.join((*LEASE_FREE, "ensemble-crew", *words))


def _lease_free_recovery(
    manager: str, cursor: int, generation: int, reader: tuple[int, int] | None
) -> list[str]:
    named = _named(manager)
    if reader is not None and reader[0] > cursor and reader[1] == generation:
        ack = _lease_free("ack", *named, "--through", str(reader[0]), "--gen", str(generation))
        ack = f"handled through {reader[0]}? first: {ack}, then attach"
    else:
        ack = f"{_lease_free('ack', *named, '--through')} <line> --gen {generation}"
        ack = f"handled events after N={cursor}? first: {ack}, then attach; unacknowledged events replay"
    monitor = ("--after", str(cursor), "--gen", str(generation), "--content", "--follow")
    return [
        "lasting fix: restart the commander's client under a new holder, then attach and arm its"
        f" monitor: command: {_restart(manager)}",
        ack,
        "until then, attach without a lease, keeping the prefix on the commands it prints:"
        f" {_lease_free('attach', *named)}",
        "and monitor with its monitor: line; with no ack first, this is that line:"
        f" {_lease_free('next-event', *named, *monitor)}",
    ]


def _stale_holder_notes(args, reader: tuple[int, int] | None = None) -> list[str]:
    """Recovery for a pair whose holder ended; reader: the failed next-event's line and GEN."""
    roster = _roster(args)
    cursor, generation, holder = roster.lease_state()
    manager = os.path.basename(roster.directory)
    state = f"handled N={cursor} GEN={generation}"
    if holder is not None:
        return [
            f"{ENDED}; {holder} holds manager {manager} now; {state}",
            f"use {holder}'s commander; take over only on purpose: {_restart(manager, '--force')}",
        ]
    return [
        f"{ENDED}; no holder is active; {state}",
        *_lease_free_recovery(manager, cursor, generation, reader),
    ]


def _add_recovery(error: StaleHolder, args, reader: tuple[int, int] | None = None) -> None:
    if any(note.startswith(ENDED) for note in getattr(error, "__notes__", ())):
        return
    try:
        notes = _stale_holder_notes(args, reader)
    except (OSError, RosterError, ValueError):
        return
    for note in notes:
        error.add_note(note)


def _lease() -> Lease | None:
    holder = os.environ.get("ENSEMBLE_HOLDER_ID")
    generation = os.environ.get("ENSEMBLE_HOLDER_GENERATION")
    if holder is None and generation is None:
        return None
    if not holder or generation is None or not re.fullmatch(r"[1-9][0-9]*", generation):
        raise ValueError("ENSEMBLE_HOLDER_ID and ENSEMBLE_HOLDER_GENERATION must go together")
    identity = uuid.UUID(holder)
    if identity.version != 4 or str(identity) != holder:
        raise ValueError("ENSEMBLE_HOLDER_ID must be a canonical UUID4")
    return Lease(holder, int(generation))


def _crew(args) -> Crew:
    return Crew(_roster(args), lease=_lease())


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
    while True:
        try:
            prompt = f"select a manager [1-{len(found)}], Enter cancels: "
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
            f"crew: no manager {printable(answer)}; give 1-{len(found)} or a name", file=sys.stderr
        )


def _select(manager: Manager | Unreadable) -> int:
    record = manager.record if isinstance(manager, Manager) else None
    if isinstance(record, Unread):
        print(
            f"crew: manager {manager.name}: {record.error}; its session is not resumed",
            file=sys.stderr,
        )
    live = isinstance(record, SessionRecord) and managers.alive(record.pid)
    plan = managers.plan(manager, live=live)
    if isinstance(plan, Refused):
        print(
            f"crew: manager {manager.name}: {printable(plan.reason)}; nothing opened",
            file=sys.stderr,
        )
        return 1
    print(f"{plan.verb} {plan.name}")
    return 0


def _list(args) -> int:
    out = sys.stderr if args.select else sys.stdout
    found = managers.managers(os.path.expanduser(managers.ROOT))
    if not found:
        print("no managers yet; start one: ensemble <name>", file=out)
        return 0
    failed = _rows(found, out, numbered=args.select)
    chosen = _choose(found) if args.select else None
    return _select(chosen) if chosen is not None else 1 if failed else 0


def _hold(args) -> int:
    return Holder(_roster(args)).run(args.command, session=args.session, force=args.force)


def _resume_commander(args) -> int:
    roster, lease = _roster(args), _lease()
    if lease is None:
        raise ValueError("run resume-commander under the manager's hold: ensemble list")
    roster.bind(lease)
    roster.check_lease()
    cwd, argv = managers.resume_target(roster.directory)
    os.chdir(cwd)
    os.execvp(argv[0], argv)


def _run(args) -> int:
    print(_crew(args).run(Spec(args.name, args.command, args.note), resume=args.resume).to_line())
    return 0


def _launch(args) -> int:
    record = _crew(args).launch(
        args.kind,
        args.key,
        tuple(args.arguments),
        note=args.note,
        name=args.entry_name,
        resume=args.resume,
        work_dir=args.work_dir,
        task=args.task,
        credentials=args.credentials,
    )
    print(record.to_line())
    return 0


def _attach(args) -> int:
    crew = _crew(args)
    lines = crew.attach().splitlines()
    trailer = lines.pop()
    cursor, generation = (field.partition("=")[2] for field in trailer.split())
    named = _named(os.path.basename(crew.roster.directory))
    print("\n".join(lines))
    for name, entry in crew.roster.entries().items():
        if entry.state == State.GONE:
            command = ("ensemble-crew", "resume", *named, name)
            print(f"resume: {shlex.join(command)}")
    if os.path.isdir(crew.roster.directory):
        for label, verb, after, *extra in (
            ("monitor", "next-event", cursor, "--content", "--follow"),
            ("next-event", "next-event", cursor, "--content"),
        ):
            command = ("ensemble-crew", verb, *named, "--after", after, "--gen", generation, *extra)
            print(f"{label}: {shlex.join(command)}")
        print(f"monitor expired: {shlex.join(('ensemble-crew', 'attach', *named))}")
        print(f"manual: {shlex.join(('ensemble-crew', 'pending', *named, '--content'))}")
    print(trailer)
    return 0


def _status(args) -> int:
    failed = None
    try:
        statuses = _crew(args).status(args.names)
    except StatusError as error:
        statuses, failed = error.statuses, error
    for status in statuses:
        print(status.to_line())
    if failed is not None:
        _report_error(failed)
        return 1
    return 0


def _report_error(error: Exception) -> None:
    print(f"crew: {error}", file=sys.stderr)
    if hasattr(error, "__notes__"):
        for note in error.__notes__:
            print(f"crew: {note}", file=sys.stderr)


def _report(args) -> int:
    print(_crew(args).report(), end="")
    return 0


def _resume(args) -> int:
    crew = _crew(args)
    failed = 0
    for name in args.names:
        try:
            record = crew.resume(name, input=args.input)
            if record is not None:
                print(record.to_line())
        except StaleHolder:
            raise
        except (RosterError, RuntimeError, ValueError) as error:
            _report_error(error)
            failed = max(failed, 2 if isinstance(error, ValueError) else 1)
    return failed


def _view(args) -> int:
    failures = _crew(args).view(args.verb, args.names)
    for name, failure in failures.items():
        print(f"crew: {name}: {failure}", file=sys.stderr)
    return 1 if failures else 0


def _close(args) -> int:
    _crew(args).close(args.name)
    return 0


def _close_all(args) -> int:
    names = _crew(args).close_all(dry_run=args.dry_run)
    print("\n".join(names) if names else "nothing to close")
    return 0


def _forget(args) -> int:
    _crew(args).forget(args.name, force=args.force)
    return 0


def _note(args) -> int:
    _crew(args).note(args.name, args.text)
    return 0


def _release(args) -> int:
    _crew(args).release()
    return 0


def _next_event(args) -> int:
    crew = _crew(args)
    after = args.after
    try:
        while True:
            after, event = crew.next_event(after=after, gen=args.gen)
            block = f"{after} {event}\n"
            if args.content:
                block += crew.event_content(event)
            # One write per event: Monitor groups lines written within 200 ms into one notification.
            print(block, end="", flush=True)
            if not args.follow:
                return 0
    except StaleHolder as error:
        _add_recovery(error, args, None if after is None else (after, args.gen))
        raise


def _pending(args) -> int:
    crew = _crew(args)
    cursor, generation, events = crew.roster.backlog(require_lease=True)
    for line, event in enumerate(events, cursor + 1):
        print(f"{line} {printable(event)}")
        if args.content:
            print(crew.event_content(event), end="")
    print(f"N={cursor} GEN={generation}")
    return 0


def _ack(args) -> int:
    _crew(args).roster.advance(args.through, args.gen)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ensemble-crew",
        description="Launch workers and role/issue runs; manage their persistent roster.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Manager: roster selected by --manager, else ENSEMBLE_MANAGER; required,
but by list, which reads every manager without a lease.
Names use [A-Za-z0-9_-]+. An Entry is a name tracked in that roster.
Crew consumes names, not Records on stdin. A Record is the serialized control
and identity line returned by a launch: ensemble-session --help.

Prerequisites and effects:
  Install ensemble commands on PATH; configure the owning client/run layer.
  hold gives its child ENSEMBLE_MANAGER, the paired ENSEMBLE_HOLDER_ID /
  ENSEMBLE_HOLDER_GENERATION and ENSEMBLE_HOLDER_PID, its own pid: the
  SessionStart re-arm prompt goes only to a client in its process tree.
  Keep the pair together. An active holder fences ownership-sensitive roster
  updates; run those from its commander environment.
  ENSEMBLE_AGENT_SESSION blocks hold/run/worker/reopen/issue/role/resume and
  resume-commander; use a user-started commander. Help still works. State: ~/.ensemble/managers/NAME/.
  run/worker/reopen/issue/role give starters empty stdin; supply literal input.
  hold instead inherits the caller's input for its foreground command.
  Exit 0: success; 2: usage/invalid value; 1: operational/lease failure;
  75: run/role starter busy, before any stdout.
  hold instead propagates its child's status, signals as 128 + signal number.
  Scalar options use the last value; boolean flags may repeat.

Examples:
  ensemble-crew worker --help
  ensemble-crew hold --help

Next help:
  ensemble-session --help          workers, clients, hosts and placement
  ensemble-delegate run --help     roles and tasks
  ensemble-orchestrator router --help    issue runs
""",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--manager", help="roster name; default: ENSEMBLE_MANAGER; required from one source"
    )
    subs = parser.add_subparsers(required=True)

    def page(name, purpose, details, *, manager=True, **options):
        return subs.add_parser(
            name,
            parents=[common] if manager else [],
            help=purpose,
            description=purpose + ".",
            epilog=details + "\nRoster, environment and exits: ensemble-crew --help\n",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            **options,
        )

    hold = page(
        "hold",
        "Keep a manager's holder lease while COMMAND runs in the foreground",
        """Prerequisites and effects:
  COMMAND [ARG ...] is required after --, executed without a shell.
  Outer help: ensemble-crew hold --help. After --, even --help belongs to
  COMMAND; the holder lease is acquired before executing it.
  The child inherits manager/lease identity; the lease ends when it exits.
  --session labels the holder; it does not select a terminal to attach.
  By default a holder conflict is refused. --force deliberately takes over.

Examples:
  With Codex on PATH, from a user-started terminal:
  ensemble-crew hold --manager demo --session commander -- codex
  Inside that commander's environment, reconcile its roster:
  ensemble-crew attach --manager demo
  Restart a commander whose holder ended, resuming its conversation
  (Codex: codex resume --last):
  ensemble-crew hold --manager demo --session demo -- claude --continue

Recovery and next help:
  Conflict: use the existing commander, or --force for deliberate takeover.
  Reconciliation does not acquire a holder: ensemble-crew attach --help
""",
        usage="%(prog)s [--manager MANAGER] [--session LABEL] [--force] -- COMMAND [ARG ...]",
    )
    hold.add_argument("--session", metavar="LABEL", help="optional holder display label")
    hold.add_argument("--force", action="store_true", help="take over and fence the old holder")
    hold.set_defaults(func=_hold)
    run = page(
        "run",
        "Run a Record-producing starter and track it as NAME",
        """Prerequisites and effects:
  COMMAND [ARG ...] after -- is required; no shell expansion is performed.
  The starter must finish with a Record on stdout and honor ENSEMBLE_SID.
  Crew supplies an identity (--resume SID, else a new one) and roster events
  file, records launch intent before executing, then prints the Record.
  A failed launch may remain lost.
  Put Crew options before NAME, then --, then the starter and its options.
  Outer help: ensemble-crew run --help. Help after -- runs the starter path.

Examples:
  With Codex configured and no helper entry or session:
  ensemble-crew run --manager demo helper -- \\
    ensemble-session start helper --client codex --display none

Recovery and next help:
  Inspect/reconcile failed launches: ensemble-crew attach --help
  Starter options: ensemble-session start --help
""",
        usage="%(prog)s [--manager MANAGER] [--note TEXT] [--resume SID] NAME -- COMMAND [ARG ...]",
    )
    run.add_argument("name", metavar="NAME", help="new roster entry name")
    run.add_argument("--note", metavar="TEXT", help="optional roster note; default: none")
    run.add_argument(
        "--resume",
        type=_sid,
        metavar="SID",
        help="ENSEMBLE_SID to give the starter; default: a new one",
    )
    run.set_defaults(func=_run)
    for kind, key, purpose, owner, details in (
        (
            "worker",
            "NAME",
            "Start a named worker and track its Record",
            f"{LayerCommand.SESSION} start",
            """NAME is both the worker's session name and roster entry name.
Session options go after NAME; --client CLIENT is required there.
Session passes CLIENT_FLAGS after its -- verbatim to the client.
--resume SID resumes conversation SID (ensemble-session start --resume SID)
under that same session identity.

Examples:
  With Codex configured and no helper session/entry:
  ensemble-crew worker --manager demo helper --client codex --display none
  Starts a hidden worker, records it in demo and prints its Record.
  Form: worker [CREW_OPTIONS] NAME [SESSION_OPTIONS] [-- CLIENT_FLAGS]
  Client flag help: use the configured client executable with --help.
  Placement: ensemble-session --help
""",
        ),
        (
            "reopen",
            "ISSUE",
            "Resume a closed issue run's conversation as a worker and track its Record",
            f"{LayerCommand.SESSION} start",
            """ISSUE is a board issue identifier with a recorded run.
Crew reads the run's client, session id, cwd, agent name and Host from its run
record, WORK_DIR/work/ISSUE/run/delegate.json, then starts worker AGENT, the
agent name: ensemble-session start AGENT --host HOST --client CLIENT --cwd CWD
--resume SID, under that session identity, so a Docker run comes back on the
Docker Host; its Claude account is the session's choice unless SESSION_OPTIONS
give --account. No record or session id: exit 1.
SESSION_OPTIONS naming --client, --cwd, --host or --resume, abbreviated
or not: exit 2. A run still alive
(roster entry ISSUE or AGENT, or a session listed here or below with its
session id) or one whose liveness is unknown (its status or the session listing
failed): exit 2, so its conversation gets no second client. Where its start is
owned (every built-in client's tui on tmux or Docker), it runs under a session
harness of its own with no handlers, tracked like any other worker; the board is
not touched.

Examples:
  With TASK-123's run closed:
  ensemble-crew reopen --manager demo TASK-123 --display none
  Form: reopen [CREW_OPTIONS] ISSUE [SESSION_OPTIONS] [-- CLIENT_FLAGS]
  Placement: ensemble-session --help
""",
        ),
        (
            "issue",
            "ISSUE",
            "Start an attended issue run and track its Record",
            f"{LayerCommand.ORCHESTRATOR} router",
            """ISSUE is a configured board issue identifier and the roster entry name.
Crew supplies router --issue ISSUE --tui, a session identity and events file.
Requires board credentials, a configured role/client and an eligible issue;
execution can claim/change the board and launch agent/harness sessions.

Examples:
  With configured board access and eligible test issue TASK-123:
  ensemble-crew issue --manager demo TASK-123 --display none
  Starts the run hidden and prints its Record.
  Form: issue [CREW_OPTIONS] ISSUE [ROUTER_OPTIONS]
  There is no client-flags delimiter at this boundary.
  Placement: ensemble-session --help
""",
        ),
        (
            "role",
            "ROLE",
            "Start a detached role run and track its Record",
            f"{LayerCommand.DELEGATE} run",
            """ROLE selects a persona; --task before ROLE optionally selects its work type.
Crew supplies --role ROLE --detach --name NAME, identity and events file.
Requires configured role/client and downstream --input. Prints a Record.
Without --credentials JSON before ROLE, resolves the role/task's orchestrator declaration.
An explicit value replaces that declaration entirely; resumes reuse the saved value.

Examples:
  With the built-in pm role and its client configured:
  ensemble-crew role --manager demo --name planner --task product-design pm --input 'Plan a feature'
  Form: role [CREW_OPTIONS] ROLE [DELEGATE_OPTIONS]
  There is no client-flags delimiter at this boundary.
  Discover roles/tasks: ensemble-delegate run --list
  Placement: ensemble-session --help
""",
        ),
    ):
        launch = page(
            kind,
            purpose,
            f"Crew options go before {key}; downstream options go after it.\n"
            f"Get outer help before supplying {key}: ensemble-crew {kind} --help\n"
            "Forwarded --help belongs to the destination and may enter Crew's launch\n"
            "path, which records intent. Use the owning help directly.\n\n"
            + details
            + f"\nDownstream options: {owner} --help\n",
        )
        launch.add_argument("--note", metavar="TEXT", help="optional roster note; default: none")
        if kind == "role":
            launch.add_argument(
                "--task", metavar="TASK", help="role task; default: the role's default task"
            )
            launch.add_argument(
                "--credentials",
                metavar="JSON",
                help="opaque credential spec; default: the orchestrator's role/task declaration",
            )
            launch.add_argument(
                "--name",
                dest="entry_name",
                metavar="NAME",
                help="roster/session name; default: ROLE-<new session id's first 8 characters>",
            )
        if kind == "worker":
            launch.add_argument(
                "--resume",
                type=_sid,
                metavar="SID",
                help="resume conversation SID; default: a new one",
            )
        if kind == "reopen":
            launch.add_argument(
                "--work-dir",
                metavar="WORK_DIR",
                default=STATE,
                help=f"the board's configured work_dir; default: {STATE}",
            )
        launch.add_argument("key", metavar=key, help=f"required {key.lower()}; see below")
        launch.add_argument(
            "arguments",
            nargs=argparse.REMAINDER,
            metavar="DOWNSTREAM_OPTIONS",
            help=f"forwarded verbatim to {owner}; defaults/values: its --help",
        )
        launch.set_defaults(
            func=_launch,
            kind=kind,
            entry_name=None,
            resume=None,
            work_dir=STATE,
            task=None,
            credentials=None,
        )
    attach = page(
        "attach",
        "Reconcile a roster and print recovery/monitoring commands",
        """Prerequisites and effects:
  Use the holder's environment when a holder is active. Does not acquire a
  lease or attach a terminal. Reconciles/adopts matching launches, shows
  eligible live panes and rotates events. Prints roster rows, backlog,
  recovery/monitor commands and N=cursor GEN=generation.
  Uses the stored handled cursor; attach does not acknowledge events.

Examples:
  ensemble-crew attach --manager demo

Recovery and next help:
  Follow the printed monitor command to read events. Reading events alone
  does not verify notification delivery to a commander.
  On monitor expiry, use its printed monitor expired command, then rearm
  from the returned handled N and GEN. Confirm handling with ack.
  Acquire a holder: ensemble-crew hold --help
  Event reading: ensemble-crew next-event --help
""",
    )
    attach.set_defaults(func=_attach)
    status = page(
        "status",
        "Print one Status line per selected roster entry",
        """Reads owning controllers; does not reconcile or acquire a holder.
Omit names for the whole roster. Partial operational failures exit 1 while
valid entries still print. Missing entries are usage errors.

Examples:
  ensemble-crew status --manager demo

Recovery: ensemble-crew attach --help
""",
    )
    status.add_argument(
        "names", nargs="*", metavar="NAME", help="entry names; default: all entries"
    )
    status.set_defaults(func=_status)
    report = page(
        "report",
        "Summarize every entry: state, note, latest reply and pull request",
        """Reads controllers and, for a pull request link, gh (authenticated).
Prints tab-separated rows including checks, mergeability and review. Does not
adopt, show, mark or acquire a lease; unavailable details are reported.

Examples:
  ensemble-crew report --manager demo

Recovery: check gh authentication for missing pull request details;
ensemble-crew status --help for individual status queries.
""",
    )
    report.set_defaults(func=_report)
    resume = page(
        "resume",
        "Recover gone entries or answer one waiting entry; print Records",
        """Requires existing roster Records and lease access. Live entries return
their Record; gone entries run their saved resume command. An entry already
resuming may print nothing. --input requires exactly one waiting entry;
without it, waiting entries are refused. Issue answers belong on the board.
A closed entry is no longer in the roster: a closed issue run's conversation
comes back with ensemble-crew reopen --help.

Examples:
  With helper gone and its saved conversation available:
  ensemble-crew resume --manager demo helper

Recovery: inspect state with ensemble-crew status --help;
reconcile interrupted bookkeeping with ensemble-crew attach --help.
""",
    )
    resume.add_argument("names", nargs="+", metavar="NAME", help="one or more existing entry names")
    resume.add_argument("--input", metavar="TEXT", help="answer exactly one waiting role entry")
    resume.set_defaults(func=_resume)
    for verb, text in (
        ("hide", "close each entry's panes; its work runs on"),
        ("show", "show each entry as at its launch; print attach commands its layer gives"),
    ):
        view = page(
            verb,
            text,
            f"""Give entry names or --all, never both; neither is a usage error.
Uses saved placement; no new --display is accepted. Work survives hide.
Requires the owning controllers; no holder lease is needed.
Partial failures exit 1.

Examples:
  With a live helper entry:
  ensemble-crew {verb} --manager demo helper

Recovery: show can print owning-layer attachment commands; run the printed
command when placement fails. Placement: ensemble-session --help
""",
        )
        view.add_argument("names", nargs="*", metavar="NAME", help="existing roster entry names")
        view.add_argument("--all", action="store_true", help="every live entry")
        view.set_defaults(func=_view, verb=verb)
    for name, handler in (("close", _close), ("forget", _forget)):
        command = page(
            name,
            "Terminate an entry's work and remove its roster entry"
            if name == "close"
            else "Remove a stopped entry's bookkeeping",
            (
                "close calls the owning controller before removing the entry; a session\n"
                "name owned by an entry also selects it. Requires lease access. Removes\n"
                "the entry only once its owner confirmed the work ended; a failed close\n"
                "keeps it, exits 1 and appends a line to the manager's\n"
                "close-failures.jsonl. A locked run's environment lock goes only once\n"
                "its container is confirmed removed.\n"
                if name == "close"
                else "By default refuses live work. --force skips controller checks and removes\n"
                "bookkeeping only; work may remain alive. Requires lease access.\n"
            )
            + f"\nExamples:\n  With helper {'tracked' if name == 'close' else 'stopped'}:\n"
            f"  ensemble-crew {name} --manager demo helper\n"
            "\nRecovery: inspect entries with ensemble-crew status --help\n",
        )
        command.add_argument("name", metavar="NAME", help="existing entry name")
        if name == "forget":
            command.add_argument(
                "--force",
                action="store_true",
                help="drop bookkeeping without ctl; work may remain alive",
            )
        command.set_defaults(func=handler)
    close_all = page(
        "close-all",
        "Terminate roster entries and the caller's descendant work",
        """Requires lease access and ensemble-session listing. Also includes untracked
descendants opened by this caller. Prints selected names or nothing to close.
Without --dry-run closes work and removes successful entries; a failed one is
kept and logged to close-failures.jsonl. Partial listing or controller failures
exit 1 and may leave work running.

Examples:
  From the intended commander's terminal, preview first:
  ensemble-crew close-all --manager demo --dry-run

Recovery: ensemble-crew attach --help
""",
    )
    close_all.add_argument(
        "--dry-run", action="store_true", help="list targets without closing/removing"
    )
    close_all.set_defaults(func=_close_all)
    note = page(
        "note",
        "Set or clear an entry's roster note",
        """Updates bookkeeping; prints nothing. Requires an existing entry and lease
access. Quote text containing spaces; empty text clears it.

Examples:
  With helper tracked:
  ensemble-crew note --manager demo helper 'Waiting for review'
  ensemble-crew note --manager demo helper ''

Inspect notes: ensemble-crew report --help
""",
    )
    note.add_argument("name", metavar="NAME", help="existing entry name")
    note.add_argument("text", metavar="TEXT", help="required note; empty text clears it")
    note.set_defaults(func=_note)
    page(
        "release",
        "Release this holder's lease without closing its work",
        """Requires the current holder's environment. Removes ownership, not roster
entries or sessions; prints nothing. A holder normally releases on child exit.

Examples:
  Inside demo's current holder environment:
  ensemble-crew release --manager demo

Acquire ownership again: ensemble-crew hold --help
""",
    ).set_defaults(func=_release)
    pending = page(
        "pending",
        "Read unhandled events without waiting or acknowledging",
        """Requires lease access when a holder is active. Prints numbered pending
events, then the handled N and matching GEN. --content adds current owning-entry
content. Run each active turn when monitoring is manual.

Examples:
  ensemble-crew pending --manager demo --content

Recovery: ensemble-crew attach --help
Handling confirmation: ensemble-crew ack --help
""",
    )
    pending.add_argument(
        "--content", action="store_true", help="include current owning-entry content"
    )
    pending.set_defaults(func=_pending)
    ack = page(
        "ack",
        "Confirm handling through a line in the current event generation",
        """Requires lease access. Acknowledges the entire prefix through LINE;
confirm every action in that prefix first. Repeated acknowledgment is safe.
Stale generations or out-of-range lines fail without advancing the cursor.

Examples:
  After handling lines through 3 in generation 1:
  ensemble-crew ack --manager demo --through 3 --gen 1

Recovery: ensemble-crew attach --help; use its current N and GEN.
Read pending events: ensemble-crew pending --help
""",
    )
    ack.add_argument(
        "--through", type=_number, required=True, metavar="LINE", help="handled prefix end >=0"
    )
    ack.add_argument(
        "--gen", type=_number, required=True, metavar="GEN", help="matching event generation >=0"
    )
    ack.set_defaults(func=_ack)
    next_event = page(
        "next-event",
        "Wait for and print roster events after a cursor",
        """Use paired N/GEN values printed by attach; --after end skips existing events
for this reader only. Reads do not acknowledge handling; use ack afterward.
Prints LINE EVENT, optionally followed by current owning-entry content.
Default: wait for one event. --follow repeats until stopped or an error.
Malformed complete lines exit 1 with the events path and physical line number;
inspect with pending. An unfinished final line waits for its newline.
Requires roster event access; no notification-delivery guarantee is implied.

Examples:
  If attach printed N=0 GEN=1 for demo:
  ensemble-crew next-event --manager demo --after 0 --gen 1 --content --follow

Recovery: on expiry/rotation, run attach's printed monitor expired command.
Rearm from its handled N and GEN, never the last printed line.
Handling confirmation: ensemble-crew ack --help
""",
    )
    next_event.add_argument(
        "--after",
        type=_after,
        required=True,
        metavar="LINE|end",
        help="reader position >=0, or end; never acknowledges",
    )
    next_event.add_argument(
        "--gen", type=_number, required=True, metavar="GEN", help="matching generation >=0"
    )
    next_event.add_argument(
        "--content",
        action="store_true",
        help="then print the owning entry's ctl content at the time of reading",
    )
    next_event.add_argument(
        "--follow", action="store_true", help="keep printing events until stopped or failing"
    )
    next_event.set_defaults(func=_next_event)
    listing = page(
        "list",
        "Print every manager, its holder state, session record and entries",
        """Reads each ~/.ensemble/managers/NAME/ without a lease; changes nothing and
works while a manager is held. Takes no --manager and ignores ENSEMBLE_MANAGER.
One row per manager: NAME, holder state (client-running, hold-only or
not-held), client and cwd from its session record (- without one). Under it,
one indented row per entry: name, kind, roster state; - for none.
Exit 1 when a manager's roster, session record or locks cannot be read: its row
ends with error and the reason; the others still print.
--select (ensemble list adds it on a terminal): the rows go to stderr, numbered,
then a prompt reads a number or name from stdin; an empty line or EOF cancels.
stdout gets the selection's plan, VERB NAME, VERB one of attach, resume-held,
resume-new or open; a manager it must not open prints why and exits 1.

Examples:
  ensemble-crew list
  ensemble-crew list --select
""",
        manager=False,
    )
    listing.add_argument(
        "--select",
        action="store_true",
        help="list on stderr, prompt on stdin, print the selection's plan: VERB NAME",
    )
    listing.set_defaults(func=_list)
    page(
        "resume-commander",
        "Resume the manager's recorded client session under this holder",
        """Requires this environment's holder pair for the manager's live holder: run it
in the shell its hold keeps (ensemble list types or runs it there). Reads
~/.ensemble/managers/NAME/session.json, refuses while its client still runs
or when its cwd is gone, gets the client's resume command from
ensemble-session resume-argv, then replaces itself with that client in the
recorded cwd. Exit 2 without a holder pair; 1 when it cannot resume.

Examples:
  In tmux session demo's held shell:
  ensemble-crew resume-commander

Recovery and next help:
  Not held here: ensemble list, then select the manager.
  Client resume commands: ensemble-session resume-argv --help
""",
    ).set_defaults(func=_resume_commander)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    command = ()
    if arguments and arguments[0] in {"run", "hold"} and "--" in arguments:
        delimiter = arguments.index("--")
        command = tuple(arguments[delimiter + 1 :])
        arguments = arguments[:delimiter]
    parser = _parser()
    args = parser.parse_args(arguments)
    if args.func in {_run, _hold}:
        if not command:
            parser.error("run and hold require -- followed by a command")
        args.command = command
    elif command:
        parser.error("only run and hold accept a command after --")
    if args.func is _view and bool(args.names) == args.all:
        parser.error(f"{args.verb} takes entry names or --all")
    if args.func is _resume and args.input is not None and len(args.names) != 1:
        parser.error("resume --input requires exactly one name")
    if args.func in {_run, _hold, _resume, _launch, _resume_commander} and (
        refused := nested_start()
    ):
        print(f"crew: {refused}", file=sys.stderr)
        return 2
    try:
        return args.func(args)
    except StartBusy as error:
        _report_error(error)
        return EXIT_BUSY
    except ValueError as error:
        _report_error(error)
        return 2
    except StaleHolder as error:
        _add_recovery(error, args)
        _report_error(error)
        return 1
    except (OSError, RosterError, RuntimeError) as error:
        _report_error(error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
