"""Execute opaque starter, control and resume commands while Roster owns their bookkeeping."""

import contextlib
import datetime
import json
import os
import re
import shlex
import subprocess
import sys
import time
import uuid
from collections.abc import Iterable
from dataclasses import replace

from ensemble.manager.engine.pr import NO_PR, VIEW_FIELDS, cells, pr_url, unknown
from ensemble.manager.engine.roster import LOCK_TIMEOUT, Roster
from ensemble.manager.engine.types import (
    Entry,
    Lease,
    Liveness,
    RosterError,
    Spec,
    StartBusy,
    StartRefused,
    State,
    StatusError,
)
from ensemble.utils import (
    EVENTS_ENV,
    EXIT_BUSY,
    LEASE_ENV,
    REPLY_ID,
    SID_ENV,
    STATE,
    LayerCommand,
    OutcomeStatus,
    Record,
    Status,
    StatusState,
    one_line,
    printable,
    read_reply,
)

TIMEOUT = 30
# A start's or resume's: a Docker one may first rebuild its image, which its broker allows 1800 s.
START_TIMEOUT = 1920
GRACE = 60  # from SIGTERM to SIGKILL: a Docker start in flight removes its container
RECOVERY_MARGIN = 5
# Events, receipt open/read and commit may each wait; margin covers timestamps and cleanup.
RECOVERY_AGE = START_TIMEOUT + GRACE + 4 * LOCK_TIMEOUT + RECOVERY_MARGIN
EVENT = re.compile(r"[0-9]{2}:[0-9]{2}:[0-9]{2} \S+ \S.*")
HERE = ("ensemble-session", "ls", "--here", "--records")
DESCENDANTS = (*HERE, "--descendants")
HEADER = "name\tkind\tsid\tstate\tview\tnote\tcwd\tattach"
REPORT_HEADER = "name\tkind\tstate\tnote\treply\tpr\tchecks\tmergeable\treview"
REPLY_WIDTH = 80


def _owner(session: str, entries: dict[str, Entry]) -> str | None:
    return next(
        (
            name
            for name, entry in entries.items()
            if entry.record is not None and session in entry.record.sessions
        ),
        None,
    )


def _row(fields: Iterable[str]) -> str:
    return "\t".join(printable(field).replace("\t", " ") for field in fields)


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "-")[:REPLY_WIDTH]


def _run_start(argv: tuple[str, ...], *, timeout: float, **options) -> subprocess.CompletedProcess:
    """subprocess.run, but past `timeout` SIGTERM first, so the starter can undo its start, and
    SIGKILL GRACE s later.
    """
    with subprocess.Popen(argv, **options) as proc:
        try:
            _, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.communicate(timeout=GRACE)
            except subprocess.TimeoutExpired:
                proc.kill()
            raise
    return subprocess.CompletedProcess(argv, proc.returncode, None, stderr)


def _reconciled(entry: Entry, status: Status) -> State:
    if entry.state not in (State.LIVE, State.GONE):
        return entry.state
    if status.alive:
        return State.LIVE
    if status.outcome in (OutcomeStatus.DONE, OutcomeStatus.FAILED):
        return State.DONE
    if status.outcome == OutcomeStatus.NEEDS_INPUT:
        return State.WAITING
    return State.GONE


def _closed_run(work_dir: str, issue: str) -> tuple[str, str, str, str, str, str]:
    """An issue run's role, client, session id, cwd, agent name and Host from its run record,
    `<work_dir>/work/<issue>/run/delegate.json`; never run.jsonl, which its
    agent can append to.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]+", issue):
        raise ValueError(f"{issue!r}: want an issue identifier")
    run = os.path.join(os.path.expanduser(work_dir), "work", issue, "run")
    path = os.path.join(run, "delegate.json")
    try:
        with open(path, encoding="utf-8") as stream:
            state = json.load(stream)
        role, session = state["role"], state["plan"]["session"]
        client, sid, workdir = session["client"], session["sid"], session["workdir"]
        cwd = session["access"]["cwd"]
        name, host = state["agent_name"], state["host"]
        if not all(isinstance(value, str) and value for value in (name, host)):
            raise TypeError("no agent name or host")
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise RuntimeError(f"{issue}: no run record at {path}: {error!r}") from None
    if not all(isinstance(value, str) and value for value in (role, client, sid, workdir, cwd)):
        raise RuntimeError(f"{issue}: {path}: no role, client, session id or cwd")
    if os.path.realpath(workdir) != os.path.realpath(run):
        raise RuntimeError(f"{issue}: {path} records another run, {workdir}")
    return role, client, sid, cwd, name, host


class Crew:
    def __init__(self, roster: Roster, *, lease: Lease | None = None):
        self.roster = roster
        self._listed: list[Record] | None = None
        roster.bind(lease)

    def _env(self, *, events: bool) -> dict[str, str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {SID_ENV, EVENTS_ENV, *LEASE_ENV}
        }
        if events:
            env[EVENTS_ENV] = self.roster.events
        return env

    def _call(self, argv: tuple[str, ...], text: str) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                argv,
                input=text,
                capture_output=True,
                text=True,
                timeout=TIMEOUT,
                env=self._env(events=False),
            )
        except OSError as error:
            raise RuntimeError(f"{shlex.join(argv)}: {error}") from error
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(
                f"{shlex.join(argv)}: no response within {TIMEOUT} seconds"
            ) from error

    def _execute(self, argv: tuple[str, ...], text: str = "") -> str:
        result = self._call(argv, text)
        if result.returncode:
            reason = result.stderr.strip() or f"exit {result.returncode}"
            raise RuntimeError(f"{shlex.join(argv)}: {reason}")
        return result.stdout

    def _listing(self, argv: tuple[str, ...]) -> list[Record] | None:
        try:
            return [Record.parse(line) for line in self._execute(argv).splitlines()]
        except ValueError as error:
            print(f"crew: {shlex.join(argv)}: invalid Record: {error}", file=sys.stderr)
        except RuntimeError as error:
            print(f"crew: {error}", file=sys.stderr)
        return None

    def _here(self) -> list[Record] | None:
        if self._listed is None:
            self._listed = self._listing(HERE)
        return self._listed

    def _start(
        self,
        argv: tuple[str, ...],
        name: str,
        sid: str,
        *,
        refusal=False,
        reject_missing=False,
        busy=False,
    ) -> Record:
        env = {**self._env(events=True), SID_ENV: sid}
        try:
            with self.roster.open_receipt(name) as receipt:
                result = _run_start(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=receipt,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=START_TIMEOUT,
                    env=env,
                )
                if result.returncode:
                    reason = result.stderr.strip() or f"exit {result.returncode}"
                    if refusal and result.returncode == 2 and not self.roster.receipt_output(name):
                        raise StartRefused(f"{shlex.join(argv)}: {reason}")
                    if (
                        busy
                        and result.returncode == EXIT_BUSY
                        and not self.roster.receipt_output(name)
                    ):
                        raise StartBusy(f"{shlex.join(argv)}: {reason}")
                    raise RuntimeError(f"{shlex.join(argv)}: {reason}")
                sys.stderr.write(result.stderr)
                if reject_missing:
                    raw = self.roster.receipt_output(name)
                    lines = raw.splitlines()
                    try:
                        Record.parse(lines[-1].decode("utf-8") if lines else "")
                    except (ValueError, UnicodeError) as error:
                        raise StartRefused(
                            f"{shlex.join(argv)}: no final Record: {error}"
                        ) from error
                return self.roster.receipt_record(name)
        except OSError as error:
            raise RuntimeError(f"{shlex.join(argv)}: {error}") from error
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(
                f"{shlex.join(argv)}: no response within {START_TIMEOUT} seconds"
            ) from error

    def run(self, spec: Spec, *, resume: str | None = None) -> Record:
        return self._run(spec, resume or str(uuid.uuid4()), shorthand=False, busy=True)

    def launch(
        self,
        kind: str,
        key: str,
        argv: tuple[str, ...],
        *,
        note: str | None = None,
        name: str | None = None,
        resume: str | None = None,
        work_dir: str = STATE,
        task: str | None = None,
        credentials: str | None = None,
    ) -> Record:
        sid = resume or str(uuid.uuid4())
        if kind == "worker":
            name = key
            resumed = ("--resume", resume) if resume else ()
            command = ("ensemble-session", "start", key, *resumed, *argv)
        elif kind == "reopen":
            options = argv[: argv.index("--")] if "--" in argv else argv
            # Session's argparse takes unique abbreviations, so --cw is --cwd.
            words = {w for w in (o.split("=")[0] for o in options) if w.startswith("--") and w[2:]}
            if fixed := {
                f
                for f in ("--client", "--cwd", "--host", "--resume")
                for w in words
                if f.startswith(w)
            }:
                raise ValueError(f"reopen sets {', '.join(sorted(fixed))} from the run record")
            _, client, sid, cwd, name, host = _closed_run(work_dir, key)
            found = self._live({key, name}, sid)
            if found.live:
                raise ValueError(
                    f"{key}: still live ({', '.join(found.live)}); close it before reopen"
                )
            if found.unknown:
                raise ValueError(
                    f"{key}: liveness unknown ({', '.join(found.unknown)}); "
                    "close or forget it before reopen"
                )
            command = (
                "ensemble-session",
                "start",
                name,
                "--host",
                host,
                "--client",
                client,
                "--cwd",
                cwd,
                "--resume",
                sid,
                *argv,
            )
        elif kind == "issue":
            name = key
            command = ("ensemble-orchestrator", "router", "--issue", key, "--tui", *argv)
        elif kind == "role":
            name = name if name is not None else f"{key}-{sid[:8]}"
            selected = ("--task", task) if task is not None else ()
            if credentials is None:
                credentials = self._role_credentials(key, task)
            command = (
                "ensemble-delegate",
                "run",
                "--role",
                key,
                "--detach",
                "--name",
                name,
                *selected,
                "--credentials",
                credentials,
                *argv,
            )
        else:
            raise ValueError(f"unknown launch kind: {kind}")
        return self._run(Spec(name, command, note), sid, shorthand=True, busy=kind == "role")

    def _role_credentials(self, role: str, task: str | None) -> str:
        selected = (f"--task={task}",) if task is not None else ()
        query = (LayerCommand.ORCHESTRATOR, "credentials", "--role", role, *selected)
        result = self._call(query, "")
        if result.returncode:
            error = ValueError if result.returncode == 2 else RuntimeError
            raise error(
                f"{shlex.join(query)}: {result.stderr.strip() or f'exit {result.returncode}'}"
            )
        return result.stdout.removesuffix("\n")

    def _live(self, names: set[str], sid: str) -> Liveness:
        """Roster entries named in names or holding sid, and sessions listed here or below holding
        sid: those that answer alive, and those whose liveness is unknown (status failed, reported
        on stderr; `session listing` when the listing failed).
        """
        candidates = {
            name: entry
            for name, entry in self.roster.entries().items()
            if name in names or sid in (entry.sid, entry.record and entry.record.sid)
        }
        listed = self._listing(DESCENDANTS)
        for record in listed or ():
            if record.sid == sid:
                candidates[f"session {record.name}"] = Entry(record, State.LIVE, None, "")
        answers, failures = self._statuses(candidates)
        for name, reason in failures.items():
            print(f"crew: {name}: {reason}", file=sys.stderr)
        unknown = [*failures, *(["session listing"] if listed is None else [])]
        return Liveness(
            sorted(name for name, status in answers.items() if status.alive), sorted(unknown)
        )

    def _run(self, spec: Spec, sid: str, *, shorthand: bool, busy=False) -> Record:
        self._listed = None
        intent = self.roster.begin(spec, sid=sid)
        try:
            record = self._start(
                spec.argv,
                spec.name,
                sid,
                refusal=shorthand,
                reject_missing=shorthand,
                busy=busy,
            )
            self.roster.commit(spec.name, record)
        except (StartRefused, StartBusy):
            self.roster.remove_if(spec.name, intent)
            raise
        except (RosterError, RuntimeError, ValueError) as error:
            try:
                self.roster.mark_if(spec.name, intent, State.LOST)
            except RosterError as mark_error:
                error.add_note(f"{spec.name}: could not mark lost: {mark_error}")
            raise
        return record

    def _statuses(self, entries: dict[str, Entry]) -> tuple[dict[str, Status], dict[str, str]]:
        """Each entry's Status, else why it has none: one `<ctl> status` per ctl, whose exit 1
        drops only the keys it answered no Status for (utils answer_keys).
        """
        groups: dict[tuple[str, ...], dict[str, Record]] = {}
        owners: dict[tuple[str, tuple[str, ...]], list[str]] = {}
        answers: dict[str, Status] = {}
        failures: dict[str, str] = {}
        for name, entry in entries.items():
            record = entry.record
            if record is None:
                word = StatusState.STARTING if entry.state == State.STARTING else StatusState.GONE
                answers[name] = Status(
                    name=name, alive=False, state=word, outcome=None, attach=None, hidden=False
                )
                continue
            owners.setdefault((record.name, record.ctl), []).append(name)
            groups.setdefault(record.ctl, {}).setdefault(record.name, record)
        for (key, ctl), names in owners.items():
            if any(entries[name].record != groups[ctl][key] for name in names):
                groups[ctl].pop(key)
                for name in names:
                    failures[name] = f"ambiguous Record name {key!r}"
        for ctl, records in groups.items():
            if not records:
                continue
            argv = (*ctl, "status")
            text = "".join(record.to_line() + "\n" for record in records.values())
            try:
                result = self._call(argv, text)
                if result.returncode not in (0, 1):
                    reason = result.stderr.strip() or f"exit {result.returncode}"
                    raise RuntimeError(f"{shlex.join(argv)}: {reason}")
                try:
                    statuses = [Status.parse(line) for line in result.stdout.splitlines()]
                except ValueError as error:
                    raise RuntimeError(f"{shlex.join(argv)}: invalid Status: {error}") from None
                returned = {status.name: status for status in statuses}
                if len(returned) != len(statuses) or not returned.keys() <= records.keys():
                    raise RuntimeError(f"{shlex.join(argv)}: expected one Status per Record")
            except RuntimeError as error:
                for key in records:
                    for name in owners[key, ctl]:
                        failures[name] = str(error)
                continue
            reason = result.stderr.strip() or f"exit {result.returncode}"
            for key in records:
                for name in owners[key, ctl]:
                    if key in returned:
                        answers[name] = replace(returned[key], name=name)
                    else:
                        failures[name] = f"{shlex.join(argv)}: no Status: {reason}"
        return answers, failures

    def status(self, names: Iterable[str] = ()) -> list[Status]:
        """The selected entries' Statuses; StatusError when any failed, holding the others'."""
        entries = self.roster.entries()
        selected = tuple(names) or tuple(sorted(entries))
        missing = set(selected) - entries.keys()
        if missing:
            raise ValueError(f"not in roster: {', '.join(sorted(missing))}")
        answers, failures = self._statuses({name: entries[name] for name in selected})
        statuses = [answers[name] for name in selected if name in answers]
        if failures:
            raise StatusError(
                "\n".join(f"{name}: {failures[name]}" for name in selected if name in failures),
                statuses,
            )
        return statuses

    def _control(self, verb: str, records: Iterable[Record]) -> dict[tuple[str, ...], str]:
        groups: dict[tuple[str, ...], list[Record]] = {}
        failures = {}
        for record in records:
            group = groups.setdefault(record.ctl, [])
            if record not in group:
                group.append(record)
        for ctl, group in groups.items():
            try:
                self._execute((*ctl, verb), "".join(record.to_line() + "\n" for record in group))
            except RuntimeError as error:
                failures[ctl] = str(error)
        return failures

    def view(self, verb: str, names: Iterable[str] = ()) -> dict[str, str]:
        """`<ctl> <verb>` (hide, show) on each named entry's Record, else on each alive showable
        one's, relaying its output; why each entry failed.
        """
        entries = self.roster.entries()
        selected = tuple(dict.fromkeys(names))
        missing = set(selected) - entries.keys()
        if missing:
            raise ValueError(f"not in roster: {', '.join(sorted(missing))}")
        failures: dict[str, str] = {}
        if not selected:
            answers, failures = self._statuses(entries)
            selected = tuple(
                name
                for name, status in sorted(answers.items())
                if status.alive and status.attach is not None
            )
        for name in selected:
            entry = entries[name]
            if entry.record is None:
                failures[name] = f"{entry.state}: no Record"
                continue
            argv = (*entry.record.ctl, verb)
            try:
                result = self._call(argv, entry.record.to_line() + "\n")
            except RuntimeError as error:
                failures[name] = str(error)
                continue
            if result.returncode:
                failures[name] = (
                    f"{shlex.join(argv)}: {result.stderr.strip() or f'exit {result.returncode}'}"
                )
                continue
            sys.stdout.write(result.stdout)
            sys.stderr.write(result.stderr)
        return failures

    def attach(self) -> str:
        self._listed = None
        self.roster.check_lease()
        here = self._here()
        instant = datetime.datetime.now(datetime.UTC)
        for name, entry in self.roster.entries().items():
            age = (instant - datetime.datetime.fromisoformat(entry.started)).total_seconds()
            if age <= RECOVERY_AGE:
                continue
            if entry.state == State.STARTING:
                self.roster.mark_if(name, entry, State.LOST)
            elif entry.state == State.RESUMING and here is not None:
                live = entry.sid is not None and any(entry.sid == found.sid for found in here)
                if not live:
                    self.roster.mark_if(name, entry, State.GONE)
        for record in here or ():
            try:
                cutoff = (instant - datetime.timedelta(seconds=RECOVERY_AGE)).isoformat()
                adopted = self.roster.adopt(record, resuming_before=cutoff)
                if adopted is None:
                    print(f"crew: adopt {record.name}: no matching launch sid", file=sys.stderr)
            except RosterError as error:
                print(f"crew: adopt {record.name}: {error}", file=sys.stderr)
        entries = self.roster.entries()
        answers, failures = self._statuses(entries)
        for name, failure in failures.items():
            print(f"crew: {name}: {failure}", file=sys.stderr)
        shown: list[Record] = []
        for name, entry in tuple(entries.items()):
            if name not in answers:
                continue
            status = answers[name]
            state = _reconciled(entry, status)
            if state != entry.state and not self.roster.mark_if(name, entry, state):
                current = self.roster.entries().get(name)
                if current is None:
                    entries.pop(name)
                else:
                    entries[name] = current
                answers.pop(name)
                continue
            entries[name] = replace(entry, state=state)
            if (
                state == State.LIVE
                and status.attach is not None
                and not status.hidden
                and entry.record is not None
            ):
                shown.append(entry.record)
        for failure in self._control("show", shown).values():
            print(f"crew: {failure}", file=sys.stderr)
        self.roster.rotate_events()
        cursor, generation, backlog = self.roster.backlog()
        output = [HEADER]
        for name, entry in sorted(entries.items()):
            record = entry.record
            status = answers.get(name)
            fields = (
                name,
                "-" if record is None else record.kind,
                "-" if record is None else record.sid or "-",
                "unknown"
                if status is None
                else status.state
                if entry.state == State.LIVE
                else entry.state,
                "hidden" if status is not None and status.hidden else "-",
                entry.note or "-",
                "-" if status is None else status.cwd or "-",
                "-" if status is None else status.attach or "-",
            )
            output.append(_row(fields))
        output.extend(
            f"{cursor + i} {printable(line)}"
            for i, line in enumerate(backlog, 1)
            if EVENT.fullmatch(line)
        )
        output.append(f"N={cursor} GEN={generation}")
        return "\n".join(output) + "\n"

    def report(self) -> str:
        """One row per entry: state, note, ctl content's first line and PR from note or outcome.
        Reads only: marks, adopts and shows nothing, and needs no lease.
        """
        entries = self.roster.entries()
        answers, failures = self._statuses(entries)
        for name, failure in failures.items():
            print(f"crew: {name}: {failure}", file=sys.stderr)
        output = [REPORT_HEADER]
        for name, entry in sorted(entries.items()):
            record = entry.record
            status = answers.get(name)
            state = "unknown" if status is None else _reconciled(entry, status)
            if status is not None and state == State.LIVE:
                state = status.state
            reply = "-"
            if record is not None:
                with contextlib.suppress(RuntimeError):
                    reply = _first_line(
                        self._execute((*record.ctl, "content"), record.to_line() + "\n")
                    )
            pr = NO_PR
            if url := pr_url(entry.note) or pr_url(self._outcome_url(name, record, status)):
                try:
                    pr = cells(
                        json.loads(self._execute(("gh", "pr", "view", url, "--json", VIEW_FIELDS)))
                    )
                except (RuntimeError, ValueError) as error:
                    print(f"crew: {name}: {error}", file=sys.stderr)
                    pr = unknown(url)
            kind = "-" if record is None else record.kind
            output.append(_row((name, kind, state, entry.note or "-", reply, *pr)))
        return "\n".join(output) + "\n"

    def _outcome_url(
        self,
        name: str,
        record: Record | None,
        status: Status | None,
    ) -> str | None:
        if record is None or record.kind != "role" or status is None or status.outcome is None:
            return None
        try:
            outcome = json.loads(self._execute((*record.ctl, "outcome"), record.to_line() + "\n"))
        except (RuntimeError, ValueError) as error:
            print(f"crew: {name}: {error}", file=sys.stderr)
            return None
        url = outcome.get("url") if isinstance(outcome, dict) else None
        return url if isinstance(url, str) else None

    def resume(self, name: str, *, input: str | None = None) -> Record | None:
        self._listed = None
        self.roster.check_lease()
        entries = self.roster.entries()
        if name not in entries:
            manager = os.path.basename(self.roster.directory)
            reopen = ("ensemble-crew", "reopen", f"--manager={manager}", name)
            raise ValueError(f"{name}: not in roster; a closed issue run: {shlex.join(reopen)}")
        entry = entries[name]
        status = None
        if entry.state == State.LIVE and entry.record is not None:
            # only attach marks the roster; an attempt that ended since still reads live
            status = self.status((name,))[0]
            entry = self._marked(name, entry, _reconciled(entry, status))
        if input is not None and entry.state != State.WAITING:
            raise ValueError(f"{name}: --input requires waiting (state {entry.state})")
        if input is None and entry.state == State.WAITING:
            raise RuntimeError(f"{name}: waiting: give --input to answer")
        if entry.state == State.RESUMING:
            return None
        if entry.record is None:
            raise RuntimeError(f"{name}: {entry.state}: no Record to resume")
        if input is None:
            status = status or self.status((name,))[0]
            if status.alive:
                return entry.record
            if entry.state != State.GONE:
                raise RuntimeError(f"{name}: only gone entries can resume (state {entry.state})")
        claimed = self.roster.claim_resume(
            name, state=State.WAITING if input is not None else State.GONE
        )
        if claimed is None:
            return None
        record = claimed.record
        if record is None or claimed.sid is None:
            raise RuntimeError(f"{name}: no Record or sid to resume")
        try:
            argv = record.resume if input is None else (*record.resume, "--input", input)
            resumed = self._start(argv, name, claimed.sid, refusal=input is not None)
            self.roster.commit(name, resumed)
        except StartRefused:
            self.roster.mark_if(name, claimed, State.WAITING)
            raise
        except (RosterError, RuntimeError, ValueError) as error:
            try:
                self.roster.mark_if(name, claimed, State.GONE)
            except RosterError as mark_error:
                error.add_note(f"{name}: could not mark gone: {mark_error}")
            raise
        return resumed

    def _marked(self, name: str, entry: Entry, state: State) -> Entry:
        """`entry` marked `state`; the roster's entry when another writer changed it first."""
        if state == entry.state:
            return entry
        if self.roster.mark_if(name, entry, state):
            return replace(entry, state=state)
        current = self.roster.entries().get(name)
        if current is None:
            raise ValueError(f"{name}: not in roster")
        return current

    def close(self, name: str) -> None:
        self._listed = None
        self.roster.check_lease()
        entries = self.roster.entries()
        key = name if name in entries else _owner(name, entries)
        if key is None:
            raise ValueError(f"{name}: not in roster")
        record = entries[key].record
        if record is not None:
            failures = self._control("close", (record,))
            if failures:
                reason = "\n".join(failures.values())
                error = RuntimeError(reason)
                if (path := self._traced(key, entries[key], reason, error)) is not None:
                    error = RuntimeError(f"{reason}; logged to {path}")
                raise error
        self.roster.remove(key)

    def _traced(self, name: str, entry: Entry, reason: str, error: Exception) -> str | None:
        """close-failures.jsonl's path once it holds this failed close; else None, why on `error`."""
        assert entry.record is not None
        try:
            return self.roster.close_failed(
                name, sid=entry.sid, ctl=entry.record.ctl, reason=reason
            )
        except (OSError, RosterError) as trace:
            error.add_note(f"{name}: close-failures.jsonl not written: {trace}")
            return None

    def close_all(self, *, dry_run: bool = False) -> list[str]:
        self._listed = None
        self.roster.check_lease()
        entries = self.roster.entries()
        records = [entry.record for entry in entries.values() if entry.record is not None]
        here = self._listing(DESCENDANTS)
        records.extend(here or ())
        names = sorted(set(entries) | {record.name for record in records})
        if not dry_run:
            failures = self._control("close", records)
            failed = []
            for name, entry in entries.items():
                if entry.record is None or entry.record.ctl not in failures:
                    self.roster.remove(name)
                else:
                    failed.append((name, entry, entry.record.ctl))
            if failures:
                error = RuntimeError("\n".join(failures.values()))
                for name, entry, ctl in failed:
                    self._traced(name, entry, failures[ctl], error)
                raise error
        if here is None:
            raise RuntimeError("close-all partial: unlisted work may still be running")
        return names

    def forget(self, name: str, *, force: bool = False) -> None:
        self._listed = None
        self.roster.check_lease()
        if name not in self.roster.entries():
            raise ValueError(f"{name}: not in roster")
        if not force and self.status((name,))[0].alive:
            raise RuntimeError(f"{name}: still alive; close it before forgetting")
        self.roster.remove(name)

    def note(self, name: str, text: str) -> None:
        self._listed = None
        self.roster.note(name, text)

    def release(self) -> None:
        self._listed = None
        self.roster.release()

    def next_event(self, *, after: int | None, gen: int) -> tuple[int, str]:
        self._listed = None
        cursor, identity = self.roster.watch_events(after, gen, acknowledge=False)
        while True:
            if identity is None:
                identity = self.roster.event_identity()
            line, event = self.roster.next_event(cursor, gen, identity=identity)
            if event is not None:
                if not EVENT.fullmatch(event):
                    raise RosterError(f"{self.roster.events_path}: invalid event at line {line}")
                return line, printable(event)
            else:
                time.sleep(0.5)

    def event_content(self, event: str) -> str:
        """The owning entry's ctl content (a reply's: its stored text), newline-terminated, else why
        it is unavailable.
        """
        words = event.split(maxsplit=4)
        if len(words) > 2 and words[2] == "reply":
            return self._reply_content(words[3] if len(words) > 3 else "")
        try:
            _, session, _ = event.split(maxsplit=3)[:3]
            entries = self.roster.entries()
            name = _owner(session, entries)
            record = None if name is None else entries[name].record
            if record is None:
                raise ValueError(f"{session}: no owning entry Record")
            content = self._execute((*record.ctl, "content"), record.to_line() + "\n")
        except (OSError, RosterError, RuntimeError, ValueError) as error:
            content = f"content unavailable: {printable(one_line(str(error)))}"
        return content if content.endswith("\n") else content + "\n"

    def _reply_content(self, reply_id: str) -> str:
        why = f"content unavailable: reply {printable(one_line(reply_id))}"
        if not REPLY_ID.fullmatch(reply_id):
            return f"{why}: bad id\n"
        content = read_reply(self.roster.events_path, reply_id)
        if content is None:
            return f"{why}: missing\n"
        return content if content.endswith("\n") else content + "\n"
