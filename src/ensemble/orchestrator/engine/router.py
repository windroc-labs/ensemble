"""Claim or resume issue work with one scheduler tick or an explicit issue.

Usage: ensemble-orchestrator router [--dry-run] [--issue ID]
                             [--tui [--display D] [--events FILE]]
       ensemble-orchestrator router --brake ROLE

Arguments and options:
  (no mode)     One scheduled tick: role max_runs, client_max_runs and usage
                gates; starts at most one role's run. Linear uses the app
                delegate (Agent field); local boards use the role directory.
  --issue ID    Only this issue identifier, e.g. TASK-1; bypass max_runs,
                client_max_runs and usage gates (its run still counts toward
                them).
  --dry-run     Print the plan without claiming issues or launching work;
                a tick still performs usage checks. Not an offline preview:
                read configuration, run inventory, credentials and board data.
--tui         With either: each run it starts or resumes is attended (TUI (text user interface) mode), shown at D (opaque,
              its format: ensemble-session --help; no D: the caller's place; none: started hidden,
              passed on as given), its events written to FILE, else $ENSEMBLE_EVENTS. Exits 2 before
              any Linear call without either, or when the Delegate refuses D or FILE (no place to
              show the TUI, a D its Host cannot use, FILE not writable). A run it starts or resumes
              prints its Record line on stdout, its attach commands on stderr. launchd never passes
              it: a tick resumes a run headless.
--brake ROLE  Print the run's client usage (else ROLE's); exit 0 if a deep-research round may start, 1 if not or
              unknown, 2 no such role.
--help        Print this; exit 0. Bad arguments exit 2.
  -h           Equivalent to --help. --brake must stand alone.
  Valued options occur at most once; boolean flags may repeat with no change.

Prerequisites and effects:
  Requires configured roles and board (Linear: [linear], the [linear.harness]
  OAuth app, whose token a run renews when under 5 days are left (--dry-run
  never; it logs the days left), role apps' Keychain items and access) and the
  selected Delegate/client executable. See
  ensemble-orchestrator --help for configuration, executable lookup and Record schema.
  Execution can claim/move issues, post comments or write back ended outcomes.
  A launch prepares input, records a runs.jsonl row and detaches a LinearRun;
  the router exits while the Delegate harness writes events back to the board.
  Other events go to orchestrator.jsonl (src router). A tick without diagnostics
  writes none; Linear issues without a configured role delegate log claim-skip,
  and usage-blocked candidates log usage-skip. Preview plans print JSON lines
  to stderr. Output files follow Delegate configuration.

Exit conditions:
  Tick: 2 on configuration or host usage probe failure, 1 when Linear is unavailable (each logged
  once a day), otherwise 0.
  --issue: 2 on configuration failure, 1 when Linear is unavailable or on
  unknown run inventory; 2 when busy,
  already live or unclaimable; 0 if an ended outcome was written back. Otherwise
  launch result: 0 detached/bounced, 1 launch failure, 2 issue config error,
  3 transient error. --dry-run: 0 for the plan; setup failures still apply.
  A nonzero --issue (not --dry-run) also prints its orchestrator.jsonl lines about
  the issue that stderr did not show, e.g. router.py: pick-none TASK-1 reason=...
  ENSEMBLE_SID is read once and removed: new run identity, or must match a
  resumed run (mismatch: 2; dry-run: 0). ENSEMBLE_AGENT_SESSION blocks tick and
  --issue execution with 2; --dry-run and --brake remain available.
  --tui exits 2 before Linear calls if events are absent or its Delegate refuses
  display/events (no usable placement, invalid Host value or unwritable file).

Examples (configured Linear access, client and a claimable TASK-1):
  ensemble-orchestrator router --issue TASK-1 --dry-run
  Prints the issue plan; no claim or launch.
  ensemble-orchestrator router --issue TASK-1 --tui --display none --events /tmp/demo-events.jsonl
  Starts attended but hidden; writes events and prints the Record.
  ensemble-orchestrator router --brake engineer
  Requires a configured engineer role; prints usage without starting work.

Recovery and next help:
  Busy: retry later. Live run: ensemble-orchestrator status TASK-1
  View recovery: ensemble-orchestrator attach-command TASK-1
  Interrupted run: ensemble-orchestrator resume --help
  Termination: ensemble-orchestrator close --help
  Fix reported configuration/Keychain errors before retrying.
  Command discovery: ensemble-orchestrator --help
  Role discovery: ensemble-delegate run --list
"""

import contextlib
import fcntl
import functools
import itertools
import json
import os
import re
import subprocess
import sys
import textwrap
import uuid
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from traceback import format_exception

from ensemble import delegate
from ensemble.orchestrator.engine import (
    config,
    inputs,
    related,
    target,
    writeback,
)
from ensemble.orchestrator.engine import log as event_log
from ensemble.orchestrator.engine.flow.types import (
    ISSUE_ID,
    IssueState,
)
from ensemble.orchestrator.engine.gate import BRAKE_5H, Gate, read_usage
from ensemble.orchestrator.engine.linear_run import LinearRun
from ensemble.orchestrator.engine.log import linear_error, log
from ensemble.orchestrator.engine.runs import (
    client_of,
    delegate_of,
    failures,
    latest,
    note,
    parse_log,
    role_of,
    started_task,
    trim,
)
from ensemble.orchestrator.engine.types import (
    OWNERSHIP,
    UNOWNED,
    UUID_RE,
    ClientSource,
    ConfigError,
    InputError,
    Invalid,
    KeychainError,
    LinearError,
    Spec,
    TrackerFileError,
    TrackerKind,
    Transient,
    Unavailable,
    Wiring,
)
from ensemble.orchestrator.trackers.linear.client import has_token, linear_gql
from ensemble.utils import (
    CLIENT_ENV,
    EVENTS_ENV,
    SID_ENV,
    exhausted,
    nested_start,
    one_line,
    printable,
    summary,
)

STALE = timedelta(hours=2)
LIVE = timedelta(minutes=30)
CAP = 4
NOT_QUEUED = "not a {state} issue {ownership}"
SKEW = timedelta(minutes=5)
CAP_COMMENT = "Tried 4 times without finishing; needs a look."
INTERRUPTED = "The previous agent run was interrupted. Moving this issue back to the {state} queue."
BUSY = "another router is running"
USAGE = (
    "usage: ensemble-orchestrator router [--dry-run] [--issue ID] "
    "[--tui [--display D] [--events FILE]] | --brake ROLE"
)
FLAGS = {"--dry-run": "dry", "--tui": "tui"}
VALUED = {"--issue": "issue", "--display": "display", "--events": "events"}


@dataclass(frozen=True)
class Attended:
    """A --tui router's runs: shown at display (passed on as given), events (an absolute path)."""

    display: str | None
    events: str


def latest_sid(entries, issue):
    entry = latest(entries).get(issue)
    return None if entry is None else entry.sid


def sid_times(entries, sid):
    return [e.ts for e in entries if e.sid == sid]


def first_line_time(entries, sid):
    return next(
        (e.ts for e in entries if e.sid == sid and e.kind == "start"), sid_times(entries, sid)[0]
    )


def attempt_count(entries, issue, since=None):
    return sum(1 for e in entries if e.issue == issue and (since is None or e.ts > since))


def live_sessions(cfg, entries, *, say):
    """A run whose view cannot load counts as not alive, logged once; inventory unknown: None."""
    live = {role: [] for role in cfg.roles}
    for ident, entry in sorted(latest(entries).items()):
        if entry.role not in live:
            continue
        try:
            if LinearRun.attach(ident, cfg=cfg, sid=entry.sid).delegate.alive():
                live[entry.role].append(ident)
        except (ConfigError, delegate.ConfigError) as error:
            say("recover-skip", ident, once=True, sid=entry.sid, reason=one_line(error))
        except delegate.SessionError:
            return None
    return live


def full_roles(roles, live):
    return {r for r in roles if len(live[r]) >= roles[r].max_runs}


def live_clients(entries, live):
    """Each client's live runs: live's runs counted by the client their latest entry records."""
    found = latest(entries)
    return Counter(found[i].client for ids in live.values() for i in ids)


def full_clients(limits, counts):
    return frozenset(c for c, n in limits.items() if counts[c] >= n)


def rank(issue):
    return issue["priority"] or 5  # 0 = no priority = lowest


def task_for(pick, role, role_tasks, ready, *, owner="assignee"):
    if pick.error is not None:
        return None, pick.error
    if pick.name is None:
        return None, None
    if pick.name not in role_tasks:
        return None, (
            f'Task "{pick.name}" is not one of {role}\'s tasks ({", ".join(role_tasks)}). '
            f"Fix the task or the {owner}, then move the issue back to {ready()}."
        )
    return pick.name, None


def client_for(pick, default) -> tuple[str | None, ClientSource, str | None]:
    if pick.error is not None:
        return None, "label", pick.error
    if pick.name is None:
        return default, "default", None
    return pick.name, "label", None


class Board:
    def __init__(
        self, gql, entries, cfg, *, now, dry, keychain, wiring: Wiring, gate=None, say=None
    ):
        self.gql, self.entries, self.now, self.dry = gql, entries, now, dry
        self.cfg, self.keychain, self.wiring, self.gate = cfg, keychain, wiring, gate
        self.owner_field = "delegate" if cfg.tracker is TrackerKind.LINEAR else "assignee"
        self.say: Callable[..., None] = say or functools.partial(log, "router", dry=dry)
        self.ready, self.blocked, self.live, self.statuses = None, {}, set(), {}
        self.status_errors, self.replays = {}, {}
        self.credential_errors = {}
        self.failures = failures(cfg.runs_log)
        self.tracker = wiring.connect(gql, cfg, say=self.say)

    def todo(self):
        if self.ready is None:
            found = self.tracker.ready()
            for cycle in self.tracker.dependency_cycles():
                self.say(
                    "dependency-cycle",
                    cycle[0],
                    once=True,
                    reason=f"blocked_by cycle: {' -> '.join(cycle)}; remove cyclic blocked_by links",
                )
            self.blocked = {i["identifier"]: list(b) for i, b in found if b}
            for ident, b in self.blocked.items():
                self.say("blocked", ident, once=True, by=b)
            self.ready = [i for i, _ in found if i["identifier"] not in self.blocked]
        return self.ready

    def is_blocked(self, ident):
        self.todo()
        return ident in self.blocked

    def role(self, issue):
        return self.tracker.role(issue)

    def credential_error(self, role):
        """Check a role once per tick; one unavailable app cannot abort the board walk."""
        if role not in self.credential_errors:
            error = None
            if role not in self.cfg.roles:
                error = f"role {role!r} is not configured"
            else:
                try:
                    identity = self.wiring.identity(self.cfg, role)
                    if identity is not None:
                        self.keychain(identity)
                except KeychainError as exc:
                    error = one_line(exc)
            self.credential_errors[role] = error
            if error:
                self.say("config-error", identity=role, reason=error)
        return self.credential_errors[role]

    def available(self, issue, full):
        return issue["identifier"] not in self.live and self.role(issue) not in full

    def later(self, issue):
        return -self.cfg.stages.get(self.role(issue), 0)

    def attempts(self, issue):
        tried = [*self.entries, *self.failures]
        n = attempt_count(tried, issue["identifier"])
        return (
            n
            if n < CAP
            else attempt_count(
                tried,
                issue["identifier"],
                self.tracker.moved_at(issue["id"], IssueState.READY, by="other"),
            )
        )

    def current_sid(self, issue):
        sid = latest_sid(self.entries, issue["identifier"])
        if not sid:
            return None
        moved = self.tracker.moved_at(issue["id"], IssueState.RUNNING)
        return sid if moved is None or first_line_time(self.entries, sid) >= moved - SKEW else None

    def comment_and_move(self, issue, body, to: IssueState, frm: IssueState, step):
        if self.dry:
            return
        left = self.tracker.comment_and_move(
            issue["id"], body, to=to, frm=frm, notify=to == IssueState.REVIEW
        )
        if left is not None:
            self.say("move-skip", issue["identifier"], step=step, state=left)

    def bounce(self, issue, body, frm=IssueState.READY):
        """An issue that cannot be run: comment body and move it from frm to In Review."""
        if self.dry:
            self.say("would comment", issue["identifier"], body=body, to="in_review")
        self.comment_and_move(issue, body, IssueState.REVIEW, frm, "claim")

    def status(self, ident, sid):
        if sid not in self.statuses:
            try:
                st = LinearRun.attach(ident, cfg=self.cfg, sid=sid).delegate.session_status()
                self.statuses[sid] = None if st.resumable == delegate.Resumable.UNKNOWN else st
            except ConfigError as error:
                self.status_errors[sid] = one_line(error)
                self.statuses[sid] = None
            except (delegate.ConfigError, delegate.SessionError):
                self.statuses[sid] = None
        return self.statuses[sid]

    def unknown(self, ident, sid):
        self.say(
            "recover-skip",
            ident,
            once=True,
            sid=sid,
            reason=self.status_errors.get(sid, "session unknown"),
        )
        return False

    def recover(self, live_ids=()):
        self.live = set(live_ids)
        mine = [
            (i, self.current_sid(i))
            for i in self.tracker.assigned(IssueState.RUNNING)
            if i["identifier"] not in self.live
        ]
        mine.sort(
            key=lambda p: (
                p[1] is None,
                rank(p[0]),
                self.later(p[0]),
                first_line_time(self.entries, p[1]) if p[1] else self.now,
            )
        )
        return [
            (issue, sid)
            for issue, sid in mine
            if (
                self.cfg.tracker is not TrackerKind.LINEAR
                or not self.credential_error(
                    role_of(self.entries, sid) if sid else self.role(issue)
                )
            )
            and self.settle(issue, sid)
        ]

    def recent_session(self, ident):
        latest = latest_sid(self.entries, ident)
        if latest is None:
            return False
        status = self.status(ident, latest)
        if status is None:
            self.unknown(ident, latest)
            return True
        return status.last_active is not None and status.last_active > (self.now - LIVE).timestamp()

    def replay(self, issue, sid):
        """The ended run's outcome its ledger has not completed, written back in process instead of
        a resume (FR-41): None when there is none; else whether its write-back is now complete.
        """
        ident = issue["identifier"]
        try:
            outcome = LinearRun.attach(ident, cfg=self.cfg, sid=sid).delegate.outcome()
        except (ConfigError, delegate.ConfigError, delegate.SessionError):
            return None  # status says why
        if outcome is None:
            return None
        try:
            done = writeback.replay(
                self.cfg,
                ident,
                sid,
                outcome,
                writers=functools.partial(self.wiring.claimed, self.gql, self.cfg),
                dry=self.dry,
            )
        except (ConfigError, ValueError) as error:
            self.say("recover-skip", ident, once=True, sid=sid, reason=one_line(error))
            return False
        if done is not None:
            self.say("recover", ident, to="writeback", sid=sid)
        return done

    def settle(self, issue, sid, *, wait=True):
        ident = issue["identifier"]
        if wait and self.recent_session(ident):
            return False
        if sid and (replayed := self.replay(issue, sid)) is not None:
            self.replays[ident] = replayed
            return False
        entered = (
            self.tracker.moved_at(issue["id"], IssueState.RUNNING) if wait and not sid else None
        )
        if sid and self.attempts(issue) >= CAP:
            self.say("recover", ident, to="in_review", reason=f"reached {CAP} attempts")
            self.comment_and_move(
                issue, CAP_COMMENT, IssueState.REVIEW, IssueState.RUNNING, "recover"
            )
        elif sid:
            st = self.status(ident, sid)
            if st is None:
                return self.unknown(ident, sid)
            if st.resumable == delegate.Resumable.YES:
                role, logged = (
                    self.role(issue),
                    next(e.role for e in reversed(self.entries) if e.sid == sid),
                )
                if logged == role:
                    return True
                why = f"role ({logged}) is not the {self.owner_field}'s ({role})"
                self.say("recover", ident, to="in_review", reason=why)
                self.comment_and_move(
                    issue,
                    f"The interrupted agent run's {why}; needs a look.",
                    IssueState.REVIEW,
                    IssueState.RUNNING,
                    "recover",
                )
            elif not wait or sid_times(self.entries, sid)[-1] < self.now - LIVE:
                self.say("recover", ident, to="todo", reason="not resumable", sid=sid)
                self.comment_and_move(
                    issue,
                    INTERRUPTED.format(state=self.tracker.state_name(IssueState.READY)),
                    IssueState.READY,
                    IssueState.RUNNING,
                    "recover",
                )
        elif not wait or entered is None or entered < self.now - STALE:
            self.say(
                "recover",
                ident,
                to="todo",
                reason="no session",
                entered=entered.isoformat() if entered else None,
            )
            self.comment_and_move(
                issue,
                INTERRUPTED.format(state=self.tracker.state_name(IssueState.READY)),
                IssueState.READY,
                IssueState.RUNNING,
                "recover",
            )
        return False

    def queue(self, full=()):
        return sorted(
            (i for i in self.todo() if self.available(i, full)),
            key=lambda i: (rank(i), self.later(i), i["createdAt"]),
        )

    def plan(self, run, live=None):
        """Log the planned run: ("new", queue length) or ("resume", spec, sid), sid the issue's
        session; a resumption spec of mode new is a restart, logged here as it is about to start.
        """
        if run[0] == "new":
            self.say("plan", mode="new", queue=run[1], live=live)
            return
        spec, sid = run[1:]
        if spec.mode == "resume":
            tier = self.cfg.task_tier(started_task(self.cfg.runs_log, spec.issue, sid))
            self.say("plan", spec.issue, mode="resume", sid=sid, tier=tier, live=live)
            return
        tier = self.cfg.task_tier(spec.task)
        self.say("plan", spec.issue, mode="new", client=spec.client, tier=tier, live=live)
        self.say(
            "restart",
            spec.issue,
            sid=sid,
            client=spec.client,
            client_source=spec.client_source,
            resumed=client_of(self.entries, sid),
        )

    def gated(self, issue, role, client, source, frm=IssueState.READY):
        """Whether the usage gate blocks a new run of client; an issue-chosen client's quota
        configuration error bounces the issue from frm instead.
        """
        ident = issue["identifier"]
        try:
            return self.gate is not None and self.gate.blocked(
                role,
                ident,
                key=(self.cfg.delegate_kind, client),
                reject_config=source == "label",
            )
        except delegate.ConfigError as error:
            self.say("launch-error", ident, client=client, error=one_line(error))
            self.bounce(
                issue,
                _label_client_message(client, error, self.tracker.state_name(IssueState.READY)),
                frm,
            )
            return True

    def resumption(self, issue, sid, new_sid):
        """The run that continues the issue's session sid: its resume, or a new session (new_sid) on
        the client the issue chooses when that differs, as a session resumes only in its own
        client. An invalid task or client choice keeps the resume.
        """
        ident, role, resumed = (
            issue["identifier"],
            role_of(self.entries, sid)
            if self.cfg.tracker is TrackerKind.LINEAR
            else self.role(issue),
            client_of(self.entries, sid),
        )
        if role not in self.cfg.roles:
            raise ConfigError(f"{ident}: session {sid}'s role {role!r} is not configured")
        current = self.tracker.recheck(issue["id"])
        task, task_error = task_for(
            current.task,
            role,
            self.cfg.roles[role].tasks,
            lambda: self.tracker.state_name(IssueState.READY),
            owner=self.owner_field,
        )
        client, source, client_error = client_for(current.client, self.cfg.roles[role].run.client)
        if client_error or task_error or client == resumed:
            if self.dry:
                self.say("preview", ident, client=resumed, mode="resume", sid=sid)
            return Spec.from_issue(
                issue, role=role, mode="resume", sid=sid, task=None, client=resumed
            )
        return Spec.from_issue(
            issue,
            role=role,
            mode="new",
            # a launcher's sid naming the old session: a fresh one, which other_sid refuses
            sid=str(uuid.uuid4()) if new_sid == sid else new_sid,
            task=task,
            client=client,
            client_source=source,
        )

    def first(self, found):
        """found's first item not None; a dry run goes through all of found, previewing each."""
        selected = None
        for item in found:
            if item is not None and selected is None:
                selected = item
                if not self.dry:
                    break
        return selected

    def take(self, only=None, full=(), clients=()):
        queue = self.queue(full)
        if only:
            queue = [i for i in queue if i["identifier"] == only]

        def pick(issue):
            ident = issue["identifier"]
            if self.attempts(issue) >= CAP:
                self.say("claim-skip", ident, to="in_review", reason=f"reached {CAP} attempts")
                self.comment_and_move(
                    issue, CAP_COMMENT, IssueState.REVIEW, IssueState.READY, "pick"
                )
                return None
            self.say("pick", ident, queue=len(queue))
            return self.claim(issue, full, clients)

        if (taken := self.first(map(pick, queue))) is not None:
            return taken
        if queue:
            self.say("pick-none", reason="nothing claimable")
        else:
            self.say("pick-none", only, reason=self.unqueued(only) if only else "queue empty")
        return None

    def unqueued(self, ident):
        """Why ident is not queued, with the tracker's refusal."""
        current = self.tracker.recheck(ident)
        if self.cfg.tracker is TrackerKind.LINEAR and current.ready and current.role is None:
            self.say("claim-skip", ident, once=True, reason=UNOWNED[TrackerKind.LINEAR])
        reason = NOT_QUEUED.format(
            state=self.tracker.state_name(IssueState.READY), ownership=OWNERSHIP[self.cfg.tracker]
        )
        reasons = [] if current.ready and current.role else [reason]
        reasons += [current.refused] if current.refused else []
        return "; ".join(reasons) or reason

    def claim(self, issue, full, clients=()):
        ident = issue["identifier"]
        # re-check right before claiming so a concurrent change isn't overwritten
        current = self.tracker.recheck(issue["id"])
        role = current.role
        if not current.ready:
            skip = f"no longer {self.tracker.state_name(IssueState.READY)}"
        elif role is None:
            skip = UNOWNED[self.cfg.tracker]
        elif role in full:
            skip = f"{role} is full"
        elif current.refused:
            skip = current.refused
        elif reason := self.credential_error(role):
            skip = reason
        else:
            skip = None
        if skip:
            self.say("claim-skip", ident, reason=skip)
            return None
        task, task_error = task_for(
            current.task,
            role,
            self.cfg.roles[role].tasks,
            lambda: self.tracker.state_name(IssueState.READY),
            owner=self.owner_field,
        )
        client, source, client_error = client_for(current.client, self.cfg.roles[role].run.client)
        if self.dry:
            self.say(
                "preview",
                ident,
                client=client or "invalid",
                client_source=source,
                tier=self.cfg.task_tier(task),
            )
        if comments := [error for error in (task_error, client_error) if error]:
            if client_error is None:
                reason = "bad task label"
            elif task_error is None:
                reason = "bad Agent label"
            else:
                reason = "bad task and Agent labels"
            self.say("claim-skip", ident, to="in_review", reason=reason)
            self.bounce(issue, "\n\n".join(comments))
            return None
        if client in clients:
            self.say("claim-skip", ident, reason=f"{client} is full")
            return None
        if self.gated(issue, role, client, source):
            return None
        taken = {**issue, "assignee": current.assignee, "project": current.home}
        if self.dry:
            return taken, task, role, client, source
        self.say("claim", ident, role=role, task=task, client=client, client_source=source)
        self.tracker.claim(issue["id"])
        return taken, task, role, client, source


def begin(cfg, spec: Spec) -> Spec:
    """A new run of an issue with a recorded run closes that run first (its retained TUI), by its
    sid: a newer failed launch leaves the issue no run, not that one.
    """
    recorded = latest(parse_log(cfg.runs_log)).get(spec.issue)
    if spec.mode == "new" and recorded is not None:
        try:
            LinearRun.attach(spec.issue, cfg=cfg, sid=recorded.sid).delegate.close()
        except (ConfigError, delegate.ConfigError, delegate.SessionError) as error:
            log("router", "tui-error", spec.issue, msg=one_line(error))
        else:
            log("router", "tui-closed", spec.issue)
    return spec


def record(cfg, spec: Spec, *, role, now, target=None) -> str:
    """The run's runs.jsonl row, an attempt toward CAP: a start row holds the claim's context (issue
    uuid, project, target, role), which its write-back reads, and its task, which picks
    a resume's tier; returns its Delegate kind.
    """
    if spec.mode == "new":
        kind, claim = (
            cfg.delegate_kind,
            {
                "uuid": spec.uuid,
                "project": spec.project,
                "target": target,
                "task": spec.task,
            },
        )
    else:
        kind, claim = delegate_of(parse_log(cfg.runs_log), spec.sid), {}
    note(
        cfg.runs_log,
        "start" if spec.mode == "new" else "resume",
        issue=spec.issue,
        sid=spec.sid,
        role=role,
        client=spec.client,
        delegate_kind=kind,
        now=now,
        **claim,
    )
    return kind


def fail(ident, kind, reason, rc):
    log("router", kind, ident, reason=reason)
    return rc


def outer(
    a,
    cfg,
    *,
    gql,
    run,
    keychain,
    wiring: Wiring,
    now=datetime.now,
    attended: Attended | None = None,
):
    """Prepares the issue's input, then detaches its LinearRun, attended (tui mode) when given.
    Its runs.jsonl row, an attempt toward CAP, comes once Recover can read the run back: a resume's
    first, a new run's once its LinearRun (its Delegate's state) is built, before its detach; a new
    run failing before then leaves a `failed` row, an attempt that Recover cannot read back.
    """
    rc = None
    try:
        rc = _outer(
            a, cfg, gql=gql, run=run, keychain=keychain, wiring=wiring, now=now, attended=attended
        )
        return rc
    finally:
        if a.mode == "new" and rc != 0 and a.sid not in {e.sid for e in parse_log(cfg.runs_log)}:
            note(cfg.runs_log, "failed", issue=a.issue, sid=a.sid, now=now)


def _outer(a, cfg, *, gql, run, keychain, wiring, now, attended):
    os.environ["PATH"] = config.path()
    if reason := a.identity_error():
        print(f"router.py: {reason}", file=sys.stderr)
        return 2
    if a.mode == "resume" and cfg.tracker is TrackerKind.LINEAR:
        a = replace(a, role=role_of(parse_log(cfg.runs_log), a.sid))
    role = a.role
    if role is not None and role not in cfg.roles:
        return fail(a.issue, "config-error", f"role {role!r} is not configured", 2)
    tasks = cfg.roles[role].tasks if role is not None else ()
    reason = a.role_error(role, tasks, cfg.tracker)
    if role is None or reason:
        return fail(a.issue, "config-error", reason, 2)
    identity = wiring.identity(cfg, role)
    kind = record(cfg, a, role=role, now=now) if a.mode == "resume" else cfg.delegate_kind
    try:
        if identity is not None:
            keychain(identity)
    except KeychainError as error:
        return fail(a.issue, "config-error", one_line(error), 2)
    if a.mode == "resume":
        try:
            st = LinearRun.attach(a.issue, cfg=cfg, sid=a.sid).delegate.session_status()
            why = None if st.resumable == delegate.Resumable.YES else st.resumable
        except (ConfigError, delegate.ConfigError, delegate.SessionError) as e:
            why = one_line(e)
        if why:
            return fail(a.issue, "transient", f"session {a.sid} not resumable ({why})", 3)
    build = config.TASKS[role].input == "build"
    reader = wiring.reader(gql, cfg)
    try:
        issue = reader.issue(a.issue)
    except writeback.ERRORS as e:
        return fail(a.issue, "transient", f"issue read: {one_line(e)}", 3)
    a = replace(a, uuid=issue.id)
    writers = wiring.writers(gql, cfg, identity)
    ctx = writeback.for_run(cfg, role, a, writers)
    workdir = cfg.issue_workdir(a.issue)
    delegate_cls = LinearRun.kind_cls(cfg, kind)
    if build:
        stage = config.TASKS[role]
        try:
            repo = target.check(
                issue,
                writers.repos,
                run=run,
                delegate=delegate_cls,
                workdir=workdir,
                roles=cfg.roles,
                host=cfg.repo_host(role),
                ref=stage.ref,
                mapped_only=stage.mapped_only,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            return fail(a.issue, "transient", f"repo check: {one_line(e)}", 3)
        if isinstance(repo, Transient) or (isinstance(repo, Invalid) and a.mode == "resume"):
            return fail(a.issue, "transient", repo.reason, 3)  # never bounce a mid-build agent run
        if isinstance(repo, Invalid):
            try:
                writeback.bounce(ctx, issue, repo)
            except writeback.ERRORS as e:
                return fail(a.issue, "transient", f"bounce: {one_line(e)}", 3)
            log("router", "bounce", a.issue, reason=repo.reason)
            return 0
    else:
        host = next(
            (cfg.repo_host(name) for name in cfg.roles if config.TASKS[name].input == "build"),
            cfg.docs.host,
        )
        repo = target.research_repo(issue, writers.repos, host=host)
    slug = f"{repo.owner}/{repo.name}" if build and repo else None
    if repo is not None:
        repo = target.with_clone(
            repo,
            a.issue,
            clones=cfg.local_clones,
            delegate=delegate_cls,
            workdir=workdir,
            run=run,
        )
    try:
        sources = inputs.gather(issue, role, cfg.docs, run=run)
    except (InputError, OSError, subprocess.TimeoutExpired) as e:
        return fail(a.issue, "transient", f"docs: {one_line(e)}", 3)
    found = None
    if a.mode == "new" and latest(parse_log(cfg.runs_log)).get(a.issue) is None:
        try:
            found = related.search(reader, issue, cfg, target=repo, run=run)
        except (LinearError, Unavailable, InputError, OSError, subprocess.SubprocessError) as e:
            log("router", "related-skip", a.issue, reason=one_line(e))
    text = inputs.render(
        issue,
        role,
        sources,
        target=repo,
        docs=cfg.docs,
        related=found,
    ).replace("\x00", "")
    # a lone surrogate cannot be encoded
    text = text.encode("utf-8", "replace").decode()
    try:
        try:
            linear_run = LinearRun(
                a.issue,
                text,
                workdir,
                cfg=cfg,
                role=role,
                sid=a.sid,
                task=a.task,
                client=a.client,
                resume=a.mode == "resume",
                delegate_kind=kind,
                # a resume's task is its start row's: the Delegate gets none, the tier stays
                tier=cfg.task_tier(
                    a.task if a.mode == "new" else started_task(cfg.runs_log, a.issue, a.sid)
                ),
                **(
                    {"mode": "tui", "display": attended.display, "events": attended.events}
                    if attended
                    else {}
                ),
            )
        except delegate.ConfigError as error:
            if a.mode == "new" and a.client_source == "label":
                return _label_client_error(a, ctx, error)
            raise
        if a.mode == "new":
            record(cfg, a, role=role, target=slug, now=now)
        detached = linear_run.detach()
    except (ConfigError, delegate.ConfigError, delegate.SessionError) as e:
        log("router", "launch-error", a.issue, error=one_line(e))
        return 1
    if attended:
        announce(a.issue, linear_run, detached)
    return 0


def _label_client_message(client, error, ready):
    first = str(error).split("\n", 1)[0]
    return (
        f'Client "{client}" chosen on the issue, but the run could not be built: {first}\n\n'
        f"Fix the issue's client or the client configuration, then move this issue back to {ready}."
    )


def _label_client_error(spec, ctx, error):
    log("router", "launch-error", spec.issue, client=spec.client, error=one_line(error))
    try:
        ctx.tracker.comment_and_move(
            ctx.issue_id,
            _label_client_message(spec.client, error, ctx.tracker.state_name(IssueState.READY)),
            to=IssueState.REVIEW,
            frm=IssueState.RUNNING,
            notify=True,
        )
    except writeback.ERRORS as write_error:
        return fail(spec.issue, "transient", f"client error write-back: {one_line(write_error)}", 3)
    return 1


def announce(ident, linear_run, detached):
    """An attended run's Record line on stdout; on stderr its attach command (its TUI's) and, per
    session, the command printing that session's.
    """
    print(detached.to_line(), flush=True)
    try:
        command = linear_run.delegate.attach_command()
    except (delegate.ConfigError, delegate.SessionError) as e:
        command = f"unknown ({one_line(e)})"
    if command:
        print(f"router.py: {ident}: attach: {command}", file=sys.stderr)
    for name in detached.sessions:
        print(
            f"router.py: {ident}: {name}: ensemble-session attach-command {name}", file=sys.stderr
        )


@contextlib.contextmanager
def lock(runs, dry):
    if dry:
        yield True
        return
    os.makedirs(os.path.dirname(runs), exist_ok=True)
    with open(
        os.path.join(os.path.dirname(runs), "router.lock"), "a"
    ) as f:  # not inherited by children (PEP 446)
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            free = True
        except BlockingIOError:
            free = False
        yield free


class Held:
    """A tick's walk log: plan, pick, usage-skip, pick-none and skip lines wait, written before the
    next other line. At the walk's end, waiting usage-skips mean a usage-blocked walk: only they are
    written; else all are. A dry run and an exception wait for nothing.
    """

    WAIT = {"plan", "pick", "usage-skip", "pick-none", "skip"}

    def __init__(self, dry):
        self.dry, self.waiting = dry, []

    def __call__(self, kind, issue=None, **fields):
        if not self.dry and kind in self.WAIT:
            self.waiting.append((kind, issue, fields))
            return
        self.write()
        log("router", kind, issue, dry=self.dry, **fields)

    def write(self, kinds=WAIT):
        for kind, issue, fields in self.waiting:
            if kind in kinds:
                log("router", kind, issue, **fields)
        self.waiting = []

    def __enter__(self):
        return self

    def __exit__(self, error, *_):
        blocked = error is None and any(w[0] == "usage-skip" for w in self.waiting)
        self.write({"usage-skip"} if blocked else self.WAIT)


def other_sid(spec, launch_sid):
    """Whether the run's session is not the launcher's `launch_sid`, said."""
    if launch_sid is None or spec.sid == launch_sid:
        return False
    print(
        f"router.py: ${SID_ENV} {launch_sid} differs from {spec.issue}'s run {spec.sid}",
        file=sys.stderr,
    )
    return True


def choose(board, gate, live_ids, full, counts, *, clients, new_sid):
    unclaimed = False

    def resumes():
        for issue, sid in board.recover(live_ids):
            if not board.available(issue, full):
                continue
            spec, role = board.resumption(issue, sid, new_sid), board.role(issue)
            if spec.client in clients:
                reason = f"{spec.client} is full"
                board.say("recover-skip", issue["identifier"], once=True, sid=sid, reason=reason)
                continue
            if spec.mode == "new":
                blocked = board.gated(
                    issue, role, spec.client, spec.client_source, IssueState.RUNNING
                )
            else:
                key = (delegate_of(board.entries, sid), spec.client)
                blocked = gate.blocked(
                    role,
                    issue["identifier"],
                    key=key,
                )
            if not blocked:
                board.plan(("resume", spec, sid), counts)
                yield spec

    def claimed():
        nonlocal unclaimed
        if not (queue := board.queue(full)):
            return
        board.plan(("new", len(queue)), counts)
        if not (taken := board.take(full=full, clients=clients)):
            unclaimed = True
            return
        issue, task, role, client, source = taken
        yield Spec.from_issue(
            issue,
            role=role,
            mode="new",
            sid=new_sid,
            task=task,
            client=client,
            client_source=source,
        )

    spec = board.first(itertools.chain(resumes(), claimed()))
    if spec is None and unclaimed:
        board.say("skip", reason="nothing claimed")
    return spec


def tick(opts, cfg, gql, *, start, keychain, wiring, clock=datetime.now, launch_sid=None):
    now = clock().astimezone(UTC)
    dry, runs = opts["dry"], cfg.runs_log
    say = functools.partial(log, "router", dry=dry)
    with lock(runs, dry) as free:
        if not free:
            say("skip", reason=BUSY)
            return 0
        entries = parse_log(runs)
        live = live_sessions(cfg, entries, say=say)
        if live is None:
            say("skip", reason="agent runs unknown")
            return 0
        full = full_roles(cfg.roles, live)
        clients = full_clients(cfg.client_max_runs, live_clients(entries, live))
        counts = {
            r: f"{len(ids)}/{cfg.roles[r].max_runs}" for r, ids in sorted(live.items()) if ids
        } or None
        if len(full) == len(cfg.roles):
            if dry:
                say("skip", reason="all roles full", live=counts)
            return 0
        if not dry:
            try:
                trim(runs, now)
            except (OSError, UnicodeDecodeError) as e:
                say("skip", reason="trim failed", error=one_line(e))
        with Held(dry) as walk:
            gate = Gate(runs, now, dry=dry, say=walk, cfg=cfg)
            board = Board(
                gql,
                entries,
                cfg,
                now=now,
                dry=dry,
                keychain=keychain,
                wiring=wiring,
                gate=gate,
                say=walk,
            )
            live_ids = {i for ids in live.values() for i in ids}
            new_sid = launch_sid or str(uuid.uuid4())
            spec = choose(board, gate, live_ids, full, counts, clients=clients, new_sid=new_sid)
            if not dry and spec is not None and other_sid(spec, launch_sid):
                return 2
            if spec is None or dry:
                return 0
            walk.write()  # the outer logs past the walk
            return start_run(start, begin(cfg, spec), walk)


def run_issue(opts, cfg, gql, start, *, keychain, wiring, clock=datetime.now, launch_sid=None):
    now = clock().astimezone(UTC)
    ident, dry, runs = opts["issue"], opts["dry"], cfg.runs_log
    stop = 0 if dry else 2
    with lock(runs, dry) as free:
        if not free:
            log("router", "skip", ident, reason=BUSY, told=True)
            print(f"router.py: {BUSY}; try again", file=sys.stderr)
            return stop
        entries = parse_log(runs)
        live = live_sessions(cfg, entries, say=functools.partial(log, "router", dry=dry))
        if live is None:
            print("router.py: agent runs unknown; try again", file=sys.stderr)
            return 0 if dry else 1
        if any(ident in ids for ids in live.values()):
            sid = latest(entries)[ident].sid
            view = LinearRun.attach(ident, cfg=cfg, sid=sid)
            try:
                attach = view.delegate.attach_command()
            except delegate.SessionError as e:
                attach = f"unknown ({one_line(e)})"
            shown = f": {attach}" if attach else ""
            print(f"router.py: {ident} has a live agent run{shown}", file=sys.stderr)
            return stop
        entries = parse_log(runs)
        board = Board(gql, entries, cfg, now=now, dry=dry, keychain=keychain, wiring=wiring)
        if issue := next(
            (i for i in board.tracker.assigned(IssueState.RUNNING) if i["identifier"] == ident),
            None,
        ):
            sid = board.current_sid(issue)
            if cfg.tracker is TrackerKind.LINEAR and board.credential_error(
                role_of(entries, sid) if sid else board.role(issue)
            ):
                return stop
            if not board.settle(issue, sid, wait=False):
                return 0 if board.replays.get(ident) else stop
            spec = board.resumption(issue, sid, launch_sid or str(uuid.uuid4()))
            board.plan(("resume", spec, sid))
            if not dry and other_sid(spec, launch_sid):
                return 2
        elif board.is_blocked(ident) or not (taken := board.take(ident)):
            return stop
        else:
            issue, task, role, client, source = taken
            spec = Spec.from_issue(
                issue,
                role=role,
                mode="new",
                sid=launch_sid or str(uuid.uuid4()),
                task=task,
                client=client,
                client_source=source,
            )
        if dry:
            return 0
        return start(begin(cfg, spec))


def brake(role, cfg):
    client = os.environ.get(CLIENT_ENV) or cfg.roles[role].run.client
    if not cfg.usage_limit.get(client, True):
        return 0
    usage = read_usage(role, cfg, client=client)
    print(summary(usage))
    return 1 if usage is None or exhausted(usage, BRAKE_5H) else 0


def options(argv):
    opts = {"brake": None, "dry": False, "tui": False}
    opts.update(dict.fromkeys(VALUED.values()))
    if "--brake" in argv:
        ok = len(argv) == 2 and argv[0] == "--brake" and not argv[1].startswith("-")
        return {**opts, "brake": argv[1]} if ok else None
    i = 0
    while i < len(argv):
        a, value = argv[i], argv[i + 1] if i + 1 < len(argv) else None
        if a in FLAGS:
            opts[FLAGS[a]] = True
        elif a in VALUED and opts[VALUED[a]] is None and value not in (None, *VALUED, *FLAGS):
            opts[VALUED[a]] = value
            i += 1
        else:
            return None
        i += 1
    if not opts["tui"] and (opts["display"] is not None or opts["events"] is not None):
        return None
    return opts


def attended_runs(opts, cfg):
    """A --tui router's Attended, its display and events checked up front; None without --tui.
    ConfigError: no events file, or the Delegate refuses them.
    """
    if not opts["tui"]:
        return None
    events = opts["events"] or os.environ.get(EVENTS_ENV) or None
    if events is None:
        raise ConfigError(f"--tui needs --events FILE or ${EVENTS_ENV}")
    attended = Attended(opts["display"], os.path.abspath(events))
    try:
        LinearRun.check_tui(cfg=cfg, display=attended.display, events=attended.events)
    except delegate.ConfigError as error:
        raise ConfigError(str(error)) from None
    return attended


def _help_text():
    return re.sub(
        r"(?m)^--(?:tui|brake)\b[^\n]*(?:\n {14}[^\n]+)*",
        lambda match: textwrap.fill(
            " ".join(line.strip() for line in match[0].splitlines()),
            width=80,
            subsequent_indent=" " * 14,
            break_long_words=False,
            break_on_hyphens=False,
        ),
        __doc__ or "",
    )


def main(
    argv,
    *,
    trackers: Mapping[TrackerKind, Wiring],
    now=datetime.now,
    config_path=None,
    gql=None,
    run=None,
    keychain=None,
    start=None,
    delegate_cls=None,
):
    gql = gql or linear_gql
    run = run or config.shell
    keychain = keychain or has_token
    if argv in (["--help"], ["-h"]):
        print(_help_text())
        return 0
    opts = options(argv)
    if opts is None:
        print(USAGE, file=sys.stderr)
        return 2
    starts = opts["brake"] is None and not opts["dry"]
    if starts and (refused := nested_start()):
        print(f"router.py: {refused}", file=sys.stderr)
        return 2
    if opts["issue"] is not None and not re.fullmatch(ISSUE_ID, opts["issue"]):
        print(f"router.py: bad issue id: {opts['issue']}", file=sys.stderr)
        return 2
    launch_sid = os.environ.pop(SID_ENV, "") or None
    if launch_sid is not None and not UUID_RE.fullmatch(launch_sid):
        print(f"router.py: bad ${SID_ENV}: {launch_sid}", file=sys.stderr)
        return 2
    os.environ["PATH"] = config.path()
    with event_log.captured() as said:
        try:
            cfg = config.load(path=config_path)
        except ConfigError as error:
            config.report_error("router", error, dry=opts["dry"])
            rc = 2
        else:
            event_log.use_logs(cfg.logs_dir)
            rc = _route(
                opts,
                cfg,
                launch_sid,
                trackers=trackers,
                now=now,
                gql=gql,
                run=run,
                keychain=keychain,
                start=start,
                delegate_cls=delegate_cls,
            )
    if opts["issue"] is not None and rc and not opts["dry"]:
        why_refused(opts["issue"], said)
    return rc


def why_refused(ident, said):
    """Why an --issue run started nothing: its logged lines about ident, or about no issue, past
    the walk's plan and pick, that stderr did not show (not a tty, or deduped), as their logger's
    own text, else `router.py: <kind> [<issue>] <field>=<value>…`.
    """
    for line, shown, text in said:
        if shown or line["kind"] in ("plan", "pick") or line.get("issue") not in (None, ident):
            continue
        if text is None:
            fields = [
                f"{k}={v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)}"
                for k, v in line.items()
                if k not in ("ts", "src", "kind", "issue", "traceback")
            ]
            words = [line["kind"], *([line["issue"]] if "issue" in line else []), *fields]
            text = f"router.py: {' '.join(words)}"
        with contextlib.suppress(OSError):  # a gone tmux pane's EIO
            print(printable(text), file=sys.stderr, flush=True)


def _route(opts, cfg, launch_sid, *, trackers, now, gql, run, keychain, start, delegate_cls):
    if delegate_cls is not None:
        cfg = replace(cfg, delegates={**cfg.delegates, cfg.delegate_kind: delegate_cls})
    wiring = trackers[cfg.tracker]
    if opts["brake"] is not None:
        role = opts["brake"]
        if role not in cfg.roles:
            print(f"router.py: no role {role}", file=sys.stderr)
            return 2
        return brake(role, cfg)
    try:
        attended = attended_runs(opts, cfg)
    except ConfigError as error:
        print(f"router.py: {error}", file=sys.stderr)
        return 2
    if start is None:
        start = functools.partial(
            outer,
            cfg=cfg,
            gql=gql,
            run=run,
            keychain=keychain,
            wiring=wiring,
            now=now,
            attended=attended,
        )
    try:
        wiring.credentials(cfg, "router", dry=opts["dry"])
        if opts["issue"]:
            return run_issue(
                opts,
                cfg,
                gql,
                start,
                keychain=keychain,
                wiring=wiring,
                clock=now,
                launch_sid=launch_sid,
            )
        return tick(
            opts,
            cfg,
            gql,
            start=start,
            keychain=keychain,
            wiring=wiring,
            clock=now,
            launch_sid=launch_sid,
        )
    except ConfigError as error:
        config.report_error("router", error, dry=opts["dry"])
        return 2
    except LinearError as e:
        return event_log.config_error("router", e, dry=opts["dry"])
    except TrackerFileError as e:
        print(f"router.py: {e}", file=sys.stderr)
        return 2
    except Unavailable as e:
        return linear_error("router", e, dry=opts["dry"])


def start_run(start, args, say):
    try:
        result = start(args)
    except (Exception, SystemExit) as error:  # noqa: BLE001  # A recorded launch may fail.
        say(
            "launch-error",
            args.issue,
            error=one_line(error),
            traceback="".join(format_exception(error)),
        )
    else:
        say("launch", args.issue, exit=result)
    return 0
