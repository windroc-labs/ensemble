#!/usr/bin/env python3
"""ensemble-delegate: run a role on HarnessDelegate, deliver its prompt, or query its runs.
`run --detach` starts the run's harness and prints the run's Record line on stdout;
`--on-event CMD` makes the harness deliver each event to CMD (split once, never a shell). main reads
$ENSEMBLE_SID and $ENSEMBLE_EVENTS once and removes them from its environment: `run`'s sid is --sid,
else $ENSEMBLE_SID (the two must agree); `resume` refuses a run of another $ENSEMBLE_SID;
`run --detach`'s events file is --events, else $ENSEMBLE_EVENTS. Under $ENSEMBLE_AGENT_SESSION,
`run` (but --list and --dry-run) and `resume` refuse.

Exits 0 on success, 2 on usage or config errors, 1 on other failures.
"""

import argparse
import json
import os
import shlex
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict

from ensemble.delegate.composer import Composer, RunParams
from ensemble.delegate.engine import direct
from ensemble.delegate.engine.harness_delegate import HarnessDelegate
from ensemble.delegate.engine.plan import run_sid
from ensemble.delegate.engine.runner import report, say
from ensemble.delegate.engine.types import Begun, ConfigError, Layout, Preview, Spec
from ensemble.session import (
    SESSION_NAME,
    SESSION_NAME_RULE,
    Mode,
    NotShowable,
    SessionBusy,
    SessionError,
)
from ensemble.session import ConfigError as SessionConfigError
from ensemble.utils import (
    EVENTS_ENV,
    EXIT_BUSY,
    SID_ENV,
    STATE,
    LayerCommand,
    Once,
    answer_keys,
    nested_start,
)

LIST_PARAMS = RunParams(input="", out="/ensemble-list/work/out.md", workdir="/ensemble-list/work")


def main(
    argv: Sequence[str] | None = None, *, delegate_cls: type[HarnessDelegate] = HarnessDelegate
) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    sid, events = (os.environ.pop(key, "") or None for key in (SID_ENV, EVENTS_ENV))
    parser = argparse.ArgumentParser(
        prog="ensemble-delegate",
        allow_abbrev=False,
        description="Run a role, open an environment worker, or control an existing run.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Role: persona; task: optional work type, otherwise the role chooses it.
Discover configured roles/tasks and their sources: ensemble-delegate run --list
Client, Host, display and Record schema: ensemble-session --help
A Record is a serialized control/identity line, not a workdir or session name.

Prerequisites and effects:
  Configure the role and its client executable. Text user interface (TUI) runs
  need its Host tools and usable placement: ensemble-session --help.
  Missing configuration: use run --list to identify the failing role/source.
  Query keys are run workdirs, never agent session names. Omitted keys read
  one Record per stdin line; explicit keys ignore stdin. Duplicate targets
  are rejected before controls run. Batch failures retain valid results.
  Exit 0: success; 2: usage/config error; 1: operational failure;
  75: busy, a lock held (its holder on stderr).

Environment and options:
  run --sid overrides ENSEMBLE_SID only when equal; otherwise refused.
  resume refuses another ENSEMBLE_SID. These and ENSEMBLE_EVENTS are consumed
  by this command. ENSEMBLE_AGENT_SESSION refuses real run/resume; use a
  user-started commander. Help, --list and --dry-run remain available.
  Options --role/--out/--workdir/--cwd/--client/--mode/--name/--on-event accept
  one occurrence only. Other scalars use the last value; flags may repeat.

Examples:
  ensemble-delegate run --list
  ensemble-delegate run --help
""",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser(
        "run",
        help="run a role in an agent session",
        allow_abbrev=False,
        description="Run a role on the configured client, or inspect its launch plan.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""Prerequisites and effects:
  --role and --input are required unless --list; --list takes no other option.
  --input TEXT is literal, even if it names a file; --input - reads stdin.
  --detach needs --events FILE or ENSEMBLE_EVENTS (flag wins); in_process
  cannot detach. --events and --on-event require --detach.
  --on-event CMD is shell-tokenized once, never run through a shell; each
  event's JSON (JavaScript Object Notation) line goes to that command's stdin.
  Default workdir: {STATE}/adhoc/DATE-ROLE[-TASK]-SID8.
  Creates agent/run directories; session runs save to <workdir>/run/out.md
  unless --out changes it. Configured role destinations govern publication.
  Foreground waits and reports outcome status/deliverable path on stderr;
  a valid outcome (including failed/needs_input) exits 0, no outcome exits 1,
  a held lock 75. A locked run's container, TUI and lock outlast its outcome
  until its user closes it (ensemble-crew close) or its client exits.
  Detached mode starts agent/harness sessions and prints a Record when live.
  Modes: headless runs without a terminal; tui is a text user interface (TUI);
  in_process uses the configured in-process client runtime.
  --dry-run prints a JSON preview, writes nothing and skips launch preflight;
  it still requires valid configuration and conditional inputs. Its "environment" is the
  resolved container environment, null for none.

Examples:
  Discover roles/tasks before choosing one:
  ensemble-delegate run --list
  With the built-in pm role/client configured, start hidden and detached:
  ensemble-delegate run --role pm --input 'Plan a feature' --detach \\
    --display none --events /tmp/ensemble-events.jsonl
  Inspect the plan without launching:
  ensemble-delegate run --role pm --input 'Plan a feature' --dry-run

Recovery and next help:
  Save the returned Record or workdir. Inspect/recover with
  ensemble-delegate status --help and ensemble-delegate resume --help.
  Placement and missing-anchor recovery: ensemble-session --help
  Role/environment/option rules: ensemble-delegate --help
""",
    )
    _run_options(run)
    _open_options(commands)
    for name, many, what in (
        ("status", True, "one Status line per run"),
        ("content", False, "the run's content"),
        ("outcome", False, "the run's outcome as JSON, null without one"),
        ("resume", False, "resume a stopped run; print its Record"),
        ("show", True, "show each run's agent session"),
        ("hide", True, "close each run's agent and harness panes; the runs go on"),
        ("close", True, "close each run's sessions"),
        ("attach-command", False, "the command attaching to the run"),
    ):
        single = not many
        effect = {
            "status": "Prints a Status JSON line per target; does not launch or show sessions.",
            "content": "Prints the run's current content; does not resume it.",
            "outcome": "Prints outcome JSON, or null when no outcome is recorded.",
            "resume": "Live/completed runs return their Record; interrupted runs restart.\n"
            "A run whose worker is ending (its container still reaped) exits 75, busy.\n"
            "--input answers a needs_input role run; pipeline answers belong on the\n"
            "issue board. Requires the original workdir, client and session identity.",
            "show": "Shows the agent session using saved display; accepts no new --display.",
            "hide": "Closes agent/harness panes; the run continues in the background.",
            "close": "Terminates the run's sessions; keeps its workdir and recorded outcome.",
            "attach-command": "Prints an attachment command; run that printed command to attach.\n"
            "Fails when the run has no live showable session.",
        }[name]
        query = commands.add_parser(
            name,
            help=what,
            description=what[0].upper() + what[1:] + ".",
            allow_abbrev=False,
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog=(
                "Selection and effects:\n  "
                + (
                    "Requires exactly one workdir or one Record on stdin.\n"
                    if single
                    else "Accepts a batch of workdirs or Records on stdin.\n"
                )
                + "  A workdir identifies a run, not its agent session name.\n"
                + effect
                + "\n\nExamples:\n"
                "  With a recorded run at /tmp/ensemble-role:\n"
                f"  ensemble-delegate {name} /tmp/ensemble-role\n\n"
                "Recovery and next help:\n"
                "  No live view: ensemble-delegate attach-command --help\n"
                "  Interrupted run: ensemble-delegate resume --help\n"
                "  Placement: ensemble-session --help\n"
                "  Selection, environment and exits: ensemble-delegate --help\n"
            ),
        )
        if name == "resume":
            query.add_argument("--input", metavar="TEXT", help="answer a needs_input run")
        query.add_argument(
            "keys",
            nargs="*" if many else "?",
            metavar="WORKDIR",
            help="existing run workdir; omit for one Record per stdin line; see cardinality below",
        )
    prompt = commands.add_parser(
        "prompt",
        help="write a role's prompt as an instruction file",
        allow_abbrev=False,
        description="Generate and write a role's instruction file without starting an agent.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Prerequisites and effects:
  Requires configured role/task and --role, --input, --out and --workdir.
  Input is literal text; - reads stdin. --out must be a path, never -.
  Writes the composed instruction and reports its path on stderr.

Examples:
  With the built-in pm role configured:
  ensemble-delegate prompt --role pm --input 'Plan a feature' \\
    --workdir /tmp/ensemble-role --out /tmp/ensemble-prompt.md

Recovery and next help:
  Discover roles/tasks: ensemble-delegate run --list
  Run the role: ensemble-delegate run --help
  Environment, option rules and exits: ensemble-delegate --help
""",
    )
    _common_options(prompt, out="where the prompt is written")
    a = parser.parse_args(argv)
    starts = a.command in {"resume", "open"} or (a.command == "run" and not (a.list or a.dry_run))
    if starts and (refused := nested_start()):
        say(f"delegate: {refused}")
        return 2
    if a.command == "open":
        return _open(a, sid, events)
    if a.command == "run":
        return _run(a, run, argv[1:], delegate_cls, sid=sid, events=events)
    if a.command == "prompt":
        return _prompt(a, prompt, delegate_cls)
    return _query(a, delegate_cls, sid)


def _worker_name(value: str) -> str:
    if not SESSION_NAME.fullmatch(value) or value.startswith("-"):
        raise argparse.ArgumentTypeError(
            f"want {SESSION_NAME_RULE}, not starting with -, not {value!r:.80}"
        )
    return value


def _open_value(value: str) -> str:
    if not value or not value.isprintable():
        raise argparse.ArgumentTypeError("want nonempty printable text")
    return value


def _open_options(commands) -> None:
    ap = commands.add_parser(
        "open",
        help="open a named environment as an interactive worker",
        description="Open a configured environment as a plain TUI worker, without a role or task.",
        allow_abbrev=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Prerequisites and effects:
  Requires a named [delegate.environments.NAME] and a Docker default Host.
  Resolves the Host default then the named environment; prefetch is refused.
  --client defaults to delegate's global client; --account is passed to session,
  otherwise session selects by usage. --cwd defaults to this directory.
  No charter, task, prompt or outcome. Prints a worker Record on stdout.
  ENSEMBLE_SID and ENSEMBLE_EVENTS identify and track the worker through crew.
  All scalar options accept one occurrence. Exit 2: configuration; 1: failure;
  75: lock busy, naming its holder on stderr. Nested agent starts are refused.

Examples:
  With dummy configured:
  ensemble-delegate open dummy-1 --environment dummy --display none

Recovery and next help:
  Track with crew:
  ensemble-crew run dummy-1 -- ensemble-delegate open dummy-1 --environment dummy
  Close/release its lock: ensemble-session close dummy-1
  Resume its saved environment: ensemble-session resume dummy-1
  Placement and Record controls: ensemble-session --help
  Commands and exits: ensemble-delegate --help
  Environment configuration: [delegate.environments.<name>] in ~/.ensemble/config.local.toml
""",
    )
    ap.add_argument(
        "name", type=_worker_name, help="new worker's session name; must not start with -"
    )
    ap.add_argument(
        "--environment",
        required=True,
        action=Once,
        type=_open_value,
        metavar="ENV",
        help="named environment already defined in delegate config",
    )
    for name, text in (
        ("client", "configured session client; default: delegate global client"),
        ("account", "session client account; omitted: automatic by usage"),
        ("cwd", "worker directory; default: current directory"),
        ("display", "session placement; none: hidden; omitted: caller context"),
    ):
        ap.add_argument(f"--{name}", action=Once, type=_open_value, help=text)


def _open(args, sid: str | None, events: str | None) -> int:
    try:
        return direct.open_worker(
            args.name,
            args.environment,
            client=args.client,
            account=args.account,
            cwd=args.cwd,
            display=args.display,
            sid=sid,
            events=events,
        )
    except (ConfigError, SessionConfigError, ValueError) as error:
        say(f"delegate: {error}")
        return 2
    except (SessionError, OSError) as error:
        say(f"delegate: {error}")
        return 1


def _common_options(ap: argparse.ArgumentParser, *, out: str) -> None:
    ap.add_argument(
        "--role", action=Once, help="required persona; discover: ensemble-delegate run --list"
    )
    ap.add_argument("--task", help="work type; default: role chooses; discover: run --list")
    ap.add_argument(
        "--input", metavar="TEXT|-", help="the input text, even when it names a file; -: stdin's"
    )
    ap.add_argument("--out", action=Once, metavar="PATH", help=out)
    ap.add_argument(
        "--workdir",
        action=Once,
        metavar="PATH",
        help="run directory; run default: generated, prompt: required",
    )
    ap.add_argument(
        "--repo", metavar="PATH", help="directory for role's configured repo access; default: unset"
    )


def _run_options(ap: argparse.ArgumentParser) -> None:
    _common_options(ap, out="where the deliverable is saved; default <workdir>/run/out.md")
    ap.add_argument(
        "--credentials",
        metavar="JSON",
        help="opaque credential references for Docker; omitted: none; in_process refuses them",
    )
    ap.add_argument(
        "--cwd",
        action=Once,
        metavar="PATH",
        help="agent directory; default: role's cwd, else <workdir>/agent",
    )
    ap.add_argument(
        "--list",
        action="store_true",
        help="list each role, its destination type and tasks, with their sources",
    )
    ap.add_argument(
        "--client",
        action=Once,
        help="configured client name; default: role's client in Delegate config",
    )
    ap.add_argument("--sid", help=f"default ${SID_ENV}; the two must agree")
    ap.add_argument(
        "--mode",
        action=Once,
        choices=[m.value for m in Mode],
        help="default: tui with --detach, else headless",
    )
    ap.add_argument(
        "--name",
        action=Once,
        type=_name,
        metavar="N",
        help="its tui session's and Record's name, <N>-drive its harness's; default <role>-<sid8>",
    )
    ap.add_argument("--detach", action="store_true", help="run in a harness session of its own")
    ap.add_argument(
        "--events", metavar="FILE", help=f"--detach: the run's events file; default ${EVENTS_ENV}"
    )
    ap.add_argument(
        "--on-event",
        action=Once,
        type=_command,
        metavar="CMD",
        help="--detach: the command each event's JSON line goes to on stdin",
    )
    ap.add_argument(
        "--display",
        metavar="D",
        help="where its sessions show, completed on its agent's Host: ensemble-session --help",
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="print launch preview without starting or writing"
    )


def _run(
    a: argparse.Namespace,
    ap: argparse.ArgumentParser,
    argv: Sequence[str],
    delegate_cls: type[HarnessDelegate],
    *,
    sid: str | None,
    events: str | None,
) -> int:
    if a.list:
        if extra := next((arg for arg in argv if arg != "--list"), None):
            ap.error(f"--list takes no other option, not {extra}")
        try:
            print(listing(Composer()), end="")
        except ConfigError as e:
            say(f"delegate: {e}")
            return 2
        return 0
    if a.role is None:
        ap.error("the following arguments are required: --role")
    if a.mode is None:
        a.mode = (Mode.TUI if a.detach else Mode.HEADLESS).value
    if a.detach and a.events is None:
        a.events = events
    try:
        if sid is not None and a.sid not in (None, sid):
            raise ConfigError(f"${SID_ENV} {sid} differs from --sid {a.sid}")
        a.sid = sid if a.sid is None else a.sid
        _check(a)
    except ConfigError as e:
        say(f"delegate: {e}")
        return 2
    spec = spec_of(a, sys.stdin.read() if a.input == "-" else a.input)
    placement = {"display": a.display}
    if a.on_event is not None:
        placement["callback"] = a.on_event
    try:
        if a.dry_run:
            shown = _shown(delegate_cls.preview(a.role, spec, **placement), a)
            print(json.dumps(shown, ensure_ascii=False, indent=1))
            return 0
        if a.detach:
            return _detach(delegate_cls, a.role, spec, placement)
        result = delegate_cls.create(a.role, spec, display=a.display).run(_begun)
    except ConfigError as e:
        say(f"delegate: {e}")
        return 2
    except SessionError as e:
        say(f"delegate: {e}")
        return EXIT_BUSY if SessionBusy.PATTERN.match(str(e)) else 1
    return report(result, spec.out or Layout(spec.workdir).out)[0]


def _detach(delegate_cls: type[HarnessDelegate], role: str, spec: Spec, placement: dict) -> int:
    """Prints the run's Record line once its listed sessions are live."""
    try:
        record = delegate_cls.create(role, spec, **placement).detach()
    except (SessionError, OSError) as e:
        say(f"delegate: {e}")
        return EXIT_BUSY if SessionBusy.PATTERN.match(str(e)) else 1
    say(f"delegate: session {record.sid}")
    print(record.to_line(), flush=True)
    return 0


def spec_of(a, text: str) -> Spec:
    """The Spec `run`'s options name, its input `text`."""
    sid, workdir = a.sid, a.workdir
    if workdir is None:
        sid = run_sid(sid)
        task = f"-{a.task}" if a.task else ""
        name = f"{time.strftime('%Y-%m-%d')}-{a.role}{task}-{sid[:8]}"
        workdir = os.path.join(os.path.expanduser(STATE), "adhoc", name)
    return Spec(
        text,
        workdir,
        task=a.task,
        mode=a.mode,
        client=a.client,
        sid=sid,
        name=a.name,
        events=a.events,
        out=a.out,
        repo=a.repo,
        cwd=a.cwd,
        credentials=getattr(a, "credentials", None),
    )


def _check(a: argparse.Namespace) -> None:
    if a.input is None:
        raise ConfigError("--input is required")
    _check_out(a)
    if a.events is not None and not a.detach:
        raise ConfigError("--events needs --detach")
    if a.on_event is not None and not a.detach:
        raise ConfigError("--on-event needs --detach")
    if a.detach and a.events is None:
        raise ConfigError("--detach needs --events")
    if a.detach and a.mode == Mode.IN_PROCESS:
        raise ConfigError("--detach needs --mode headless or tui")


def _name(value: str) -> str:
    if not SESSION_NAME.fullmatch(value):
        raise argparse.ArgumentTypeError(f"want {SESSION_NAME_RULE}, not {value!r:.80}")
    return value


def _command(text: str) -> tuple[str, ...]:
    try:
        argv = tuple(shlex.split(text))
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None
    if not argv:
        raise argparse.ArgumentTypeError("empty command")
    return argv


def _begun(event) -> None:
    if isinstance(event, Begun):
        say(f"delegate: session {event.sid}")


def _shown(preview: Preview, a: argparse.Namespace) -> dict:
    access, s = preview.plan.session.access, preview.plan.spec
    environment = preview.plan.session.environment
    shown = {
        "client": s.client,
        "mode": s.mode,
        "sid": s.sid,
        "workdir": s.workdir,
        "cwd": access.cwd,
        "project": access.project,
        "dirs": access.dirs,
        "reads": access.reads,
        "commands": access.commands,
        "tier": preview.plan.session.tier,
        "effort": preview.plan.session.effort,
        "prompt": preview.plan.prompt,
        "sessions": {"agent": preview.agent},
        "hosts": {"agent": preview.host},
        "callback": preview.callback,
        "environment": None if environment is None else environment.persisted(),
    }
    if a.detach:
        shown["events"] = os.path.abspath(a.events)
    return shown


def _check_out(a: argparse.Namespace) -> None:
    if a.out == "-":
        raise ConfigError("--out takes a PATH, not -")


def _prompt(
    a: argparse.Namespace, ap: argparse.ArgumentParser, delegate_cls: type[HarnessDelegate]
) -> int:
    if a.role is None:
        ap.error("the following arguments are required: --role")
    try:
        if None in (a.input, a.out, a.workdir):
            raise ConfigError("--input, --out and --workdir are required")
        _check_out(a)
        spec = Spec(
            sys.stdin.read() if a.input == "-" else a.input,
            a.workdir,
            task=a.task,
            out=a.out,
            repo=a.repo,
        )
        result = delegate_cls.instruct(a.role, spec)
    except (ConfigError, OSError) as error:
        say(f"delegate: {error}")
        return 2
    say(f"delegate: instruction delivered: {result.instruction}")
    return 0


def _query(a: argparse.Namespace, delegate_cls: type[HarnessDelegate], sid: str | None) -> int:
    single = a.command in ("outcome", "resume", "attach-command", "content")
    keys = ([] if a.keys is None else [a.keys]) if single else a.keys

    def viewed(key):
        view = delegate_cls.attach(key, sid=sid if a.command == "resume" else None)
        if single:
            return "", view
        status = view.status()
        return status.name, (view, status)

    refused: list[SessionError] = []

    def resumed(view) -> str:
        try:
            return _resume(view, a.input)
        except SessionError as error:
            refused.append(error)
            raise

    reply = {
        "status": lambda pair: pair[1].to_line(),
        "show": lambda pair: pair[0].show(),
        "hide": lambda pair: pair[0].hide(),
        "close": lambda pair: pair[0].close(),
        "outcome": _outcome,
        "content": lambda view: view.content(),
        "resume": resumed,
        "attach-command": _attach_command,
    }[a.command]
    code = answer_keys(
        keys,
        sys.stdin,
        viewed,
        reply,
        single=single,
        usage=(ConfigError, SessionConfigError),
        failures=(SessionError, OSError),
        prog="delegate",
        out=sys.stdout,
        err=sys.stderr,
    )
    busy = code == 1 and any(SessionBusy.PATTERN.match(str(error)) for error in refused)
    return EXIT_BUSY if busy else code


def _resume(view, input: str | None) -> str:
    if input is not None and view.ctl() != (LayerCommand.DELEGATE,):
        raise ConfigError("pipeline answers belong on the issue board")
    record = view.resume(input=input)
    if input is not None:
        say(f"delegate: session {record.sid}")
    return record.to_line()


def _outcome(view) -> str:
    outcome = view.outcome()
    return json.dumps(None if outcome is None else asdict(outcome), separators=(",", ":"))


def _attach_command(view) -> str:
    command = view.attach_command()
    if command is None:
        raise NotShowable("no showable session")
    return command


def listing(composer: Composer) -> str:
    """Each role and its tasks with their sources; checks every file and prompt, writing nothing."""
    composer.check_team_dirs(writes=[])
    composer.check_all()
    lines = []
    for role, tasks in composer.index().items():
        config = composer.resolve(role)
        composer.render(config, LIST_PARAMS)
        source = composer.team.source(composer.path_for("roles", role))
        lines.append(f"{role}  {config.output['type']}  {source}\n")
        lines += [
            f"  {t}: {what}  [{composer.team.source(composer.path_for('tasks', t))}]\n"
            for t, what in tasks.items()
        ]
    return "".join(lines)


if __name__ == "__main__":
    sys.exit(main())
