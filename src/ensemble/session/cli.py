"""ensemble-session, the session layer's command (FR-31, FR-42; bin/ensemble-session runs it).

start, restart and resume a worker (engine/worker.py) and print its Record; reply, send (text, or
keys in KeyPress notation) and read a session; status, show, hide, close and attach-command for
session names, or Records one per stdin line; ls; tile; hook, a tui client's hook call (tui_hook.run).
Every subcommand but hook reads the session config once: read_config, Host.parse_hosts,
Session.configure. ENSEMBLE_SID, read once here, is a start's or resume's session id. Inside an
agent session ensemble started (ENSEMBLE_AGENT_SESSION), start, restart, resume and client-resume
refuse.

stdout holds only machine output: Records, Status lines, ls rows, read and reply text, attach
commands. Exit 0 done, 2 a usage or config error, 1 a failed key or other failure. A batch answers
its keys in order; a failed key gets one stderr line and no stdout line, and the exit is 1.
"""

from __future__ import annotations

import argparse
import functools
import os
import re
import sys
import textwrap
import uuid
from collections.abc import Iterator
from dataclasses import replace

import ensemble.session.engine.types
import ensemble.utils
from ensemble.session.engine.base import Session, hosted_viewer
from ensemble.session.engine.config import read_config
from ensemble.session.engine.types import (
    ACCOUNT_SET_BY,
    COMMAND,
    NO_DISPLAY,
    SESSION_NAME,
    SESSION_NAME_RULE,
    ConfigError,
    Hosts,
    KeyPress,
    NotShowable,
    SessionError,
    SessionInfo,
    SessionRow,
)
from ensemble.session.engine.worker import Worker
from ensemble.session.hosts.base import Host, check_sid
from ensemble.session.modes import tui_hook
from ensemble.session.modes.tui import TuiMode
from ensemble.session.viewer import display as displays
from ensemble.utils import (
    ACCOUNT_ENV,
    EVENTS_ENV,
    SID_ENV,
    Record,
    Status,
    StatusState,
    answer_keys,
    module_roots,
    nested_start,
)

PROG = COMMAND
HEADER = "name\tstate\tclient\tsid\tdisplay"


class UsageError(ConfigError):
    """A malformed request."""


def _say(text: str) -> None:
    print(text, file=sys.stderr)


def _name(value: str) -> str:
    if not SESSION_NAME.fullmatch(value):
        raise argparse.ArgumentTypeError(f"want {SESSION_NAME_RULE}, not {value!r:.80}")
    return value


def _lines(value: str) -> int:
    if not value.isdigit() or int(value) < 1:
        raise argparse.ArgumentTypeError(f"want a positive int, not {value!r:.80}")
    return int(value)


def _env(item: str) -> tuple[str, str]:
    key, eq, value = item.partition("=")
    if not (eq and key) or "\0" in key or not value.isprintable():
        raise argparse.ArgumentTypeError(f"want NAME=VALUE, VALUE printable, not {item!r:.80}")
    return key, value


def _display_help(hosts: Hosts | None) -> str:
    """Each configured Host's display_help; a Host that cannot load fails its command later."""
    try:
        if hosts is not None:
            found = [
                f"  {h}: {displays.display_help(Session.host(h)) or 'as given'}"
                for h in hosts.tables
            ]
            return "display (--display D), per configured Host:\n" + "\n".join(found)
    except ConfigError:
        pass
    return (
        "display (--display D): the session config could not be read.\n"
        "Repair session configuration and rerun ensemble-session --help for configured\n"
        "Host syntax and defaults. --display none starts hidden without an anchor.\n"
        "Visible placement needs usable caller context or a visible anchor in that\n"
        "Host's syntax. For an existing session whose view cannot open, run\n"
        "ensemble-session attach-command NAME, then run the printed command."
    )


def _parser(hosts: Hosts | None) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog=PROG,
        description="Start and control agent sessions. A Worker is a plain client session;\n"
        "a Client is the configured agent program. A Host runs processes; display\n"
        "is its placement value. TUI means text user interface.",
        epilog=(
            "Prerequisites and effects\n"
            "  Commands need valid session configuration and their Client/Host tools.\n"
            "  Packaged defaults are overlaid by ~/.ensemble/config.local.toml's\n"
            "  [session] tables. --client names [session.clients.NAME]; the default\n"
            "  Host is [session.hosts].default. Help needs no running tools/services.\n"
            "  Query commands print stdout; failures print stderr. Exit 0: success;\n"
            "  1: session/Host operation failed; 2: usage/configuration or nested start.\n"
            "  A Record is a serialized JSON control/identity line: name, session ids,\n"
            "  display and control/resume commands. Pass original Records unchanged,\n"
            "  one per stdin line, only to commands whose help permits them. Explicit\n"
            "  names select current sessions; Records guard against replaced sessions.\n\n"
            "Examples (configured Codex; no session named helper)\n"
            "  ensemble-session start helper --client codex --display none\n"
            "  Starts hidden; prints a Record. Starting needs a user-started terminal;\n"
            "  ENSEMBLE_AGENT_SESSION forbids nested starts/restarts/resumes.\n\n"
            "Recovery and next help\n"
            "  Discover names: ensemble-session ls\n"
            "  Inspect launch options: ensemble-session start --help\n"
            "  Recover a missing view: ensemble-session attach-command --help\n\n"
            + _display_help(hosts)
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="COMMAND")
    p = sub.add_parser(
        "start",
        help="start worker NAME; print its Record",
        description=textwrap.fill(
            "Start worker NAME, a plain text user interface session of CLIENT; everything after -- is "
            "its client's flags, verbatim. Its session id: --resume SID (resumes that "
            f"conversation), else ${SID_ENV}, else a new one; the two must agree. Prints its "
            "Record once it has run 5 s without exiting (up to 15 s while its launch input is "
            "pending).",
            width=77,
        ),
        epilog="Prerequisites and effects\n"
        "  Requires configured CLIENT and default Host tools, an existing cwd, and a\n"
        "  free NAME. Names match [A-Za-z0-9_-]+ except none. Creates a worker and\n"
        f"  prints its Record. ${EVENTS_ENV}, when nonempty, is its events file.\n"
        "  Scalar options repeated: last wins; --env accumulates by variable name.\n\n"
        "Examples (Codex configured; no helper session)\n"
        "  ensemble-session start helper --client codex --display none\n"
        "  Starts hidden; no visible anchor needed.\n"
        "  Form: ensemble-session start NAME [SESSION_OPTIONS] -- CLIENT_FLAGS\n"
        "  Before --: Session options; after --: client flags, preserved verbatim.\n"
        "  Outer help: ensemble-session start --help, before --. Help after --\n"
        "  belongs to CLIENT and may start the outer operation; use CLIENT --help.\n\n"
        "Recovery and Next help\n"
        "  Existing name: ensemble-session status helper, or choose another name.\n"
        "  Placement/defaults: ensemble-session --help\n"
        "  Missing view: ensemble-session attach-command --help",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("name", type=_name, help="required new worker name; not none")
    p.add_argument(
        "--client", required=True, help="required [session.clients.NAME] configuration name"
    )
    p.add_argument("--cwd", help="its directory (default: this one)")
    p.add_argument("--host", metavar="NAME", help="its Host (default: [session.hosts].default)")
    p.add_argument(
        "--resume", metavar="SID", help=f"resume conversation SID; else ${SID_ENV}, else new id"
    )
    p.add_argument("--prompt", default="", help="first prompt text (default: empty)")
    p.add_argument(
        "--env",
        action="append",
        default=[],
        type=_env,
        metavar="NAME=VALUE",
        help="set in its environment; repeatable, the last of a NAME wins",
    )
    p.add_argument(
        "--account",
        metavar="NAME",
        help="its [session.clients.CLIENT.accounts.NAME] account (default: default; Docker Host only)",
    )
    p.add_argument(
        "--display",
        metavar="D",
        help=f"Host placement; {NO_DISPLAY}: hidden; omitted: caller context. See ensemble-session --help",
    )
    for cmd, text in (
        ("restart", "restart a worker's session in place; print its Record"),
        ("resume", "print a live worker's Record, else resume it; print its Record"),
        ("reply", "print a text user interface session's latest reply"),
    ):
        effects = {
            "restart": "Terminates/restarts the saved worker in place; prints its Record.",
            "resume": "Prints a live worker's Record, else resumes its saved conversation.",
            "reply": "Prints the latest reply text, or nothing if no reply is available.",
        }
        p = sub.add_parser(
            cmd,
            help=text,
            description=text + ".",
            epilog="Prerequisites and effects\n"
            + (
                "  Requires a named worker's saved metadata and configured Client/Host.\n"
                if cmd != "reply"
                else "  Requires a live TUI (text user interface) session and configured Host.\n"
            )
            + (f"  ${SID_ENV}, when set, must match the saved worker.\n" if cmd != "reply" else "")
            + f"  {effects[cmd]}\n"
            + (
                "  Uses saved cwd, flags and display; no new --display option.\n"
                if cmd != "reply"
                else ""
            )
            + f"\nExamples (existing worker helper)\n  ensemble-session {cmd} helper\n\n"
            "Recovery and Next help\n"
            "  Find current names: ensemble-session ls\n"
            "  Placement and configuration: ensemble-session --help\n"
            "  Missing metadata: ensemble-session start --help",
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        p.add_argument(
            "name",
            type=_name,
            help="required saved worker name" if cmd != "reply" else "required TUI session name",
        )
        if cmd == "reply":
            p.add_argument(
                "--sid", help="require this session id (default: no identity guard; last wins)"
            )
    for cmd, text in (
        ("status", "one Status JSON line per session"),
        ("content", "print one session's content"),
        ("show", "show each session where its display says"),
        ("hide", "close what show opened for each session; it runs on"),
        ("close", "close each session; absent is fine"),
        ("attach-command", "print each session's attach command"),
    ):
        details = {
            "status": "Prints one Status JSON line per key, including gone sessions.",
            "content": "Exactly one key; prints content from its first carried session.",
            "show": "Uses saved display; no new --display option. Work already exists.",
            "hide": "Closes only the view while work continues; requires a showable Host.",
            "close": "Terminates sessions; absent or replaced Record targets are harmless.",
            "attach-command": "Prints an attachment command for the first session of each key.",
        }
        p = sub.add_parser(
            cmd,
            help=text,
            description=textwrap.fill(f"{text}: names, else Records on stdin.", width=77),
            epilog="Prerequisites and effects\n"
            "  Requires configured target Hosts; show/hide/attach-command need a\n"
            "  showable Host. Names override stdin; otherwise read one Record per line.\n"
            "  Duplicate keys are rejected before effects. Batch commands continue\n"
            "  other valid keys after a failure; exit 2 for invalid input, 1 for\n"
            "  operational failures. Records carry session identity and display.\n"
            + f"  {details[cmd]}\n"
            + f"\nExamples (existing session helper)\n  ensemble-session {cmd} helper\n"
            + ("  Run the printed command to attach.\n" if cmd == "attach-command" else "")
            + "\nRecovery and Next help\n"
            "  Discover sessions: ensemble-session ls\n"
            "  Record schema and placement: ensemble-session --help\n"
            + ("  View failure: ensemble-session attach-command --help\n" if cmd == "show" else ""),
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        p.add_argument(
            "names",
            nargs="*",
            type=_name,
            metavar="NAME",
            help="session names; omitted: Records on stdin; content accepts one key",
        )
        if cmd == "hide":
            p.add_argument(
                "--if-closed",
                action="store_true",
                help="only after the view was closed by hand (automation); default: hide now",
            )
    p = sub.add_parser(
        "ls",
        help="list sessions",
        description="List sessions as a tab-separated table, or emit their Records.",
        epilog="Prerequisites and effects\n"
        "  Reads configured Hosts. Default: all sessions; header and columns name,\n"
        "  state, client, sid, display (- means absent). --records instead prints\n"
        "  one Record per session that has one, sorted by name. No state changes.\n"
        "  --here and --display exclude each other. --descendants requires both\n"
        "  --here and --records. Repeated scalar options use the last value.\n\n"
        "Examples (visible caller with sessions it opened)\n"
        "  ensemble-session ls --here --records\n"
        "  Prints Records for those sessions; no caller display yields no matches.\n\n"
        "Recovery and Next help\n"
        "  No matches: remove --here/--display/--match to inspect all sessions.\n"
        "  Display filtering and Record schema: ensemble-session --help\n"
        "  Record consumers: ensemble-session status --help",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--match", metavar="RE", help="name regular expression, full match (default: all names)"
    )
    where = p.add_mutually_exclusive_group()
    where.add_argument(
        "--display",
        metavar="D",
        help="Host-specific display filter (default: none); ensemble-session --help",
    )
    where.add_argument(
        "--here", action="store_true", help="only those this caller opened (default: all)"
    )
    p.add_argument(
        "--records",
        action="store_true",
        help="print original Records instead of table (default: table)",
    )
    p.add_argument(
        "--descendants",
        action="store_true",
        help="with --here --records: also those they opened, transitively (close-all)",
    )
    p = sub.add_parser(
        "send",
        help="send TEXT (then Enter), or keys",
        description=textwrap.fill(
            "Send TEXT as a paste then Enter, or --key K…: keys in a Host-independent "
            "notation: one character, or Enter Escape Tab BTab BSpace Up Down Left Right Home End "
            "PageUp PageDown F1-F12, after any of C- M- S- (C-c, S-Up).",
            width=77,
        ),
        epilog="Prerequisites and effects\n"
        "  Requires a live session and configured Host. Supply exactly one of TEXT\n"
        "  or --key; no stdin input. All keys are validated before sending any.\n"
        "  Sends to the running program, prints nothing. Scalar repeats: last wins.\n\n"
        "Examples (live session helper)\n"
        "  ensemble-session send helper 'hello'\n"
        "  Pastes hello and sends Enter.\n"
        "  ensemble-session send helper --key C-c\n"
        "  Sends Ctrl-C, which may interrupt its running program.\n\n"
        "Recovery and Next help\n"
        "  Check target identity: ensemble-session status --help\n"
        "  Inspect its screen: ensemble-session read --help\n"
        "  Configuration: ensemble-session --help",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("name", type=_name, help="required live session name")
    p.add_argument("text", nargs="?", help="literal paste text; required unless --key is given")
    p.add_argument("--sid", help="require this session id (default: no identity guard)")
    p.add_argument(
        "--key",
        nargs="+",
        metavar="K",
        help="one or more keys instead of TEXT; repeated --key: last group wins",
    )
    p = sub.add_parser(
        "read",
        help="print a session's screen",
        description="Print a session's visible screen or recent history.",
        epilog="Prerequisites and effects\n"
        "  Requires a live session and showable configured Host. Prints screen text\n"
        "  to stdout without changing the session. Scalar repeats: last wins.\n\n"
        "Examples (live session helper)\n"
        "  ensemble-session read helper --lines 20\n"
        "  Prints the last 20 lines including history.\n\n"
        "Recovery and Next help\n"
        "  Find a live name: ensemble-session status --help\n"
        "  Latest structured reply: ensemble-session reply --help\n"
        "  Host configuration: ensemble-session --help",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("name", type=_name, help="required session name")
    p.add_argument("--sid", help="require this session id (default: no identity guard)")
    p.add_argument(
        "--lines",
        type=_lines,
        metavar="N",
        help="positive number of last lines, with history (default: visible screen)",
    )
    p = sub.add_parser(
        "tile",
        help="lay out a window's sessions (the default Host's)",
        description="Specialist/automation: retile a window on the default Host.",
        epilog="Prerequisites and effects\n"
        "  Requires a showable default Host and an existing Host window. Changes\n"
        "  window layout, prints nothing; normally waits for another tile to finish.\n\n"
        "Examples: invocation form (substitute an existing Host window)\n"
        "  ensemble-session tile WINDOW [--no-wait] [--if-panes-changed]\n\n"
        "Recovery and Next help\n"
        "  Host syntax and configuration: ensemble-session --help\n"
        "  Open saved views: ensemble-session show --help",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("window", help="the window, as its Host names it")
    p.add_argument(
        "--no-wait",
        action="store_true",
        help="return at once while another tile runs (default: wait)",
    )
    p.add_argument(
        "--if-panes-changed",
        action="store_true",
        help="only when panes changed since last tile (default: retile now)",
    )
    p = sub.add_parser(
        "hook",
        help="automation: record a TUI client's hook payload",
        description="Automation: consume a TUI (text user interface) client's hook payload.",
        epilog="Prerequisites and effects\n"
        "  Requires the existing absolute session directory with valid session.json\n"
        "  and its declared callbacks. Drains stdin to EOF; parses a JSON object\n"
        "  up to 64 MiB (invalid/oversized payload becomes {}). Client callbacks may\n"
        "  transform/drop it; otherwise appends hooks.jsonl and notifies an optional\n"
        "  Host callback. Calls default to exit 0 with no stdout; --strict returns\n"
        "  1 on failure for supervisor retries; with --event, a retry of the event\n"
        "  hooks.jsonl last journaled reruns only the Host callback. Failures print\n"
        "  stderr and retain hook.log where possible. hook --serve DIR answers many\n"
        "  calls in one process: each stdin line {label, event, payload} is one call,\n"
        '  its stdout line {"ok": bool} the result, until EOF.\n\n'
        "Examples: invocation form (generated by client hook configuration)\n"
        "  ensemble-session hook /absolute/session/dir LABEL < payload.json\n"
        "  Only exact hook --help and hook -h print help without reading stdin.\n"
        "  All other arguments retain the callback contract.\n\n"
        "Recovery and Next help\n"
        "  Inspect DIR/hook.log for failures; repair the callback configuration.\n"
        "  Session configuration: ensemble-session --help",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("directory", metavar="DIR", help="required existing absolute session directory")
    p.add_argument("label", metavar="LABEL", help="required hook label: [A-Za-z0-9_.-]{1,64}")
    p.add_argument(
        "--strict", action="store_true", help="return 1 on hook failure (default: always exit 0)"
    )
    p.add_argument(
        "--event", metavar="ID", help="relay event ID; its retry journals once (default: none)"
    )
    p.add_argument(
        "--serve", action="store_true", help="DIR's calls as stdin lines (default: one call)"
    )
    p = sub.add_parser(
        "client-resume",
        help="specialist: run a client's own saved resume command",
        description="Specialist: resume a Client conversation using its native argument contract.",
        epilog="Prerequisites and effects\n"
        "  Requires configured CLIENT, its executable and saved conversation. Runs\n"
        "  that Client's resume handler and propagates its exit status; may replace\n"
        "  this process. Prefer the resume command saved in session information.\n\n"
        "Examples: annotated invocation form\n"
        "  ensemble-session client-resume CLIENT CLIENT_ARGUMENTS...\n"
        "  CLIENT selects [session.clients.NAME]; all later tokens, including --\n"
        "  and --help, belong to its resume handler, without delimiter removal.\n"
        "  Outer help: ensemble-session client-resume --help, before CLIENT.\n\n"
        "Recovery and Next help\n"
        "  Native arguments: CLIENT --help (replace CLIENT with its executable).\n"
        "  Worker recovery: ensemble-session resume --help\n"
        "  Client configuration: ensemble-session --help",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("client", help="required configured Client name")
    p.add_argument(
        "arguments",
        nargs=argparse.REMAINDER,
        help="Client-specific resume arguments, verbatim after CLIENT",
    )
    return ap


def _configure() -> Hosts:
    config = read_config()
    hosts = Host.parse_hosts(config.get("hosts"))
    Session.configure_from(config, hosts=hosts)
    return hosts


def _host_of(name: str, hosts: Hosts) -> str:
    """The configured Host listing session `name`, else the default."""
    return next((h for h in hosts.tables if name in Session.host(h).list()), hosts.default)


def _shown(name: str, hosts: Hosts) -> Host:
    host = Session.host(_host_of(name, hosts))
    if not host.attachable:
        raise NotShowable(f"session {name}: host {host.name} cannot show")
    return host


def _resolve(key: str | Record) -> tuple[str, tuple[str, Record | None]]:
    name, record = (key, None) if isinstance(key, str) else (key.name, key)
    sessions = _sessions(name, record)
    if not sessions:
        raise UsageError("names no session")
    for session in sessions:
        if not SESSION_NAME.fullmatch(session):
            raise UsageError(f"{name}: session {session!r:.80}: want {SESSION_NAME_RULE}")
    return name, (name, record)


def _sessions(name: str, record: Record | None) -> tuple[str, ...]:
    return (name,) if record is None else record.sessions


def _state(name: str, hosts: Hosts, sid: str | None = None) -> Status:
    """Session `name`'s own Status: gone once its host no longer lists it."""
    host = Session.host(_host_of(name, hosts))
    process = host.attach(name) if name in host.list() else None
    try:
        check_sid(name, process, sid)
    except SessionError:
        process = None
    if process is None:
        return Status(
            name=name, alive=False, state=StatusState.GONE, outcome=None, attach=None, hidden=False
        )
    found = displays.status(hosted_viewer(), host, process)
    return replace(found, cwd=_cwd(name)) if found.alive else found


def _cwd(name: str) -> str | None:
    """Tui session `name`'s cwd as its start recorded it; None for any other session."""
    try:
        info = SessionInfo.from_fields(TuiMode.snapshot(name))
    except SessionError:
        return None
    return None if info is None else info.cwd


def _status(name: str, record: Record | None, hosts: Hosts) -> str:
    found = [
        _state(session, hosts, None if record is None else record.sid)
        for session in _sessions(name, record)
    ]
    shown = next((each for each in found if each.alive), found[0])
    if record is not None and record.sid is not None:
        shown = next((each for each in found if each.state == StatusState.GONE), shown)
    return replace(shown, name=name, hidden=any(each.hidden for each in found)).to_line()


def _carried(
    name: str, record: Record | None, hosts: Hosts, *, stale_ok: bool = False
) -> tuple[str, ...]:
    sessions = _sessions(name, record)
    if record is None:
        return sessions
    for session in sessions:
        host = Session.host(_host_of(session, hosts))
        process = host.attach(session) if session in host.list() else None
        try:
            check_sid(session, process, record.sid)
        except SessionError:
            if stale_ok:
                return ()
            raise
    return sessions


def _show(name: str, record: Record | None, hosts: Hosts) -> None:
    for session in _carried(name, record, hosts):
        host = _shown(session, hosts)
        display = host.attach(session).display() if record is None else record.display
        command = hosted_viewer().show(session, host, display=display)
        if command is not None:
            _say(f"session {session}: not shown; attach with {command}")


def _hide(name: str, record: Record | None, hosts: Hosts, *, if_closed: bool = False) -> None:
    for session in _carried(name, record, hosts):
        hosted_viewer().hide(session, _shown(session, hosts), if_closed=if_closed)


def _attach_command(name: str, record: Record | None, hosts: Hosts) -> str:
    session = _carried(name, record, hosts)[0]
    return hosted_viewer().attach_command(session, _shown(session, hosts))


def _close(name: str, record: Record | None, hosts: Hosts) -> None:
    for session in _carried(name, record, hosts, stale_ok=True):
        try:
            Session.retire(
                session, sid=None if record is None else record.sid, host=_host_of(session, hosts)
            )
        except SessionError:
            if record is None or _carried(name, record, hosts, stale_ok=True):
                raise
            return


def _content(name: str, record: Record | None, hosts: Hosts) -> str:
    session = _carried(name, record, hosts)[0]
    return Session.attach(
        session, sid=None if record is None else record.sid, host=_host_of(session, hosts)
    ).content()


BATCH = {
    "status": _status,
    "content": _content,
    "show": _show,
    "hide": _hide,
    "close": _close,
    "attach-command": _attach_command,
}


def _answers(args, hosts: Hosts) -> int:
    act = BATCH[args.cmd]
    if args.cmd == "hide":
        act = functools.partial(_hide, if_closed=args.if_closed)
    if not args.names and (sys.stdin is None or sys.stdin.isatty()):
        raise UsageError(f"{args.cmd}: give session names or Records on stdin")
    try:
        return answer_keys(
            args.names,
            sys.stdin,
            _resolve,
            lambda key: act(*key, hosts),
            single=args.cmd == "content",
            usage=(UsageError,),
            failures=(ConfigError, SessionError, OSError),
            prog=PROG,
            out=sys.stdout,
            err=sys.stderr,
        )
    except UnicodeDecodeError:
        raise UsageError(f"{args.cmd}: stdin: not UTF-8") from None


def _listed(args, hosts: Hosts) -> Iterator[tuple[Host, str]]:
    try:
        pattern = None if args.match is None else re.compile(args.match)
    except re.error as error:
        raise UsageError(f"--match {args.match!r:.80}: {error}") from None
    for name in hosts.tables:
        host = Session.host(name)
        display = Session.display(None, host=name) if args.here else args.display
        if args.here and not display:
            continue
        names = displays.names(hosted_viewer(), host, display, descendants=args.descendants)
        yield from ((host, n) for n in names if pattern is None or pattern.fullmatch(n))


def _row(row: SessionRow) -> str:
    values = (row.name, row.state, row.client, row.sid, row.display)
    return "\t".join("-" if value is None else str(value) for value in values)


def _ls(args, hosts: Hosts) -> int:
    if args.descendants and not (args.here and args.records):
        raise UsageError("ls: --descendants needs --here --records")
    if args.records:
        for host, name in sorted(_listed(args, hosts), key=lambda found: found[1]):
            try:
                record = host.attach(name).record()
            except SessionError:
                if name in host.list():
                    raise
                continue
            if record is not None:
                print(record.to_line())
        return 0
    if args.here:
        names = {name for _, name in _listed(args, hosts)}
        rows = [row for row in Session.list(args.match) if row.name in names]
    else:
        rows = Session.list(args.match, display=args.display)
    print(HEADER)
    for row in rows:
        print(_row(row))
    return 0


def _start(args, flags: list[str], hosts: Hosts, start_sid: str | None) -> int:
    if args.resume is not None and start_sid is not None and start_sid != args.resume:
        raise UsageError(f"{SID_ENV} {start_sid} differs from --resume {args.resume}")
    cwd = os.path.abspath(args.cwd or os.getcwd())
    if not os.path.isdir(cwd):
        raise UsageError(f"--cwd {cwd}: not a directory")
    host = args.host or hosts.default
    if host not in hosts.tables:
        raise UsageError(f"--host {host}: want one of {', '.join(sorted(hosts.tables))}")
    env = dict(args.env)
    if ACCOUNT_ENV in env:
        raise UsageError(ACCOUNT_SET_BY)
    if args.account:
        env[ACCOUNT_ENV] = args.account
    events = os.environ.get(EVENTS_ENV) or None
    worker = Worker.new(
        name=args.name,
        sid=args.resume or start_sid or str(uuid.uuid4()),
        client=args.client,
        cwd=cwd,
        host=host,
        flags=tuple(flags),
        env=env,
        events=None if events is None else os.path.abspath(events),
        display=Session.display(args.display, host=host),
    )
    worker.check(prompt=args.prompt, resume=args.resume is not None)
    with worker.locked():
        record = worker.start(
            prompt=args.prompt,
            resume=args.resume is not None,
            hidden=args.display == NO_DISPLAY,
        )
    print(record.to_line())
    return 0


def _worker(args, start_sid: str | None) -> int:
    worker = Worker.load(args.name, sid=start_sid)
    with worker.locked():
        record = worker.restart() if args.cmd == "restart" else worker.resume()
    print(record.to_line())
    return 0


def _send(args, hosts: Hosts) -> int:
    if (args.text is None) == (args.key is None):
        raise UsageError("send: give TEXT or --key K…, not both")
    keys = None if args.key is None else tuple(map(KeyPress.parse, args.key))
    session = Session.attach(args.name, sid=args.sid, host=_host_of(args.name, hosts))
    if keys is None:
        session.send(args.text)
    else:
        session.press(keys)
    return 0


def _run(args, flags: list[str], hosts: Hosts, start_sid: str | None) -> int:
    cmd = args.cmd
    if cmd == "start":
        return _start(args, flags, hosts, start_sid)
    if cmd in ("restart", "resume"):
        return _worker(args, start_sid)
    if cmd in BATCH:
        return _answers(args, hosts)
    if cmd == "ls":
        return _ls(args, hosts)
    if cmd == "send":
        return _send(args, hosts)
    if cmd == "reply":
        text = Session.attach(args.name, sid=args.sid, host=_host_of(args.name, hosts)).reply
        if text:
            print(text)
    elif cmd == "read":
        host = Session.host(_host_of(args.name, hosts))
        process = host.attach(args.name)
        check_sid(args.name, process, args.sid)
        try:
            print(process.screen(args.lines))
        except NotShowable:
            raise NotShowable(f"session {args.name}: host {host.name} cannot show") from None
    elif cmd == "client-resume":
        return Session.client_resume(args.client, args.arguments)
    else:
        host = Session.host()
        if not host.attachable:
            raise NotShowable(f"session {args.window}: host {host.name} cannot show")
        displays.tile(
            hosted_viewer(),
            args.window,
            wait=not args.no_wait,
            if_panes_changed=args.if_panes_changed,
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv in (["hook", "--help"], ["hook", "-h"]):
        _parser(None).parse_args(argv)
    if argv[:2] == ["hook", "--serve"]:
        requests, replies = tui_hook.private_stdio()
        tui_hook.serve(
            argv[2:], requests, replies, roots=module_roots(ensemble.session, ensemble.utils)
        )
        return 0
    if argv[:1] == ["hook"]:
        strict = "--strict" in argv[1:]
        arguments, event = [arg for arg in argv[1:] if arg != "--strict"], None
        if "--event" in arguments:
            at = arguments.index("--event")
            event = "".join(arguments[at + 1 : at + 2])
            del arguments[at : at + 2]
        succeeded = tui_hook.run(
            arguments, sys.stdin, roots=module_roots(ensemble.session, ensemble.utils), event=event
        )
        return int(strict and not succeeded)
    start_sid = os.environ.get(SID_ENV) or None
    flags: list[str] = []
    if argv[:1] == ["start"] and "--" in argv:  # argparse < 3.13 drops a later -- from nargs
        at = argv.index("--")
        argv, flags = argv[:at], argv[at + 1 :]
    try:
        hosts, failure = _configure(), None
    except (ConfigError, OSError, ValueError) as error:
        hosts, failure = None, error
    args = _parser(hosts).parse_args(argv)
    starts = args.cmd in {"start", "restart", "resume", "client-resume"}
    if starts and (refused := nested_start()):
        _say(f"{PROG}: {refused}")
        return 2
    if hosts is None:
        _say(f"{PROG}: config: {failure}")
        return 2
    try:
        return _run(args, flags, hosts, start_sid)
    except NotShowable as error:
        _say(str(error))
        return 1
    except ConfigError as error:
        _say(f"{PROG}: {error}")
        return 2
    except (SessionError, OSError) as error:
        _say(f"{PROG}: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
