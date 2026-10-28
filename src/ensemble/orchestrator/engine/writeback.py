"""Write-back: a run's progress, outcome and usage limit as tracker comments, links and moves,
each step once per sid and attempt (the ledger); `deliver`, `replay` (Recover's), `bounce`.
"""

import contextlib
import fcntl
import functools
import hashlib
import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from traceback import format_exception

from ensemble.delegate import DelegateEvent, Outcome, Progress
from ensemble.orchestrator.engine import run_comments, runs
from ensemble.orchestrator.engine.config import LOCAL, TASKS
from ensemble.orchestrator.engine.flow import handoff, workflow
from ensemble.orchestrator.engine.flow.tracker import Tracker
from ensemble.orchestrator.engine.flow.types import (
    ISSUE_ID,
    Issue,
    IssueState,
)
from ensemble.orchestrator.engine.linear_run import LinearRun
from ensemble.orchestrator.engine.log import log
from ensemble.orchestrator.engine.types import (
    MOVES,
    Claim,
    ConfigError,
    Invalid,
    IssueError,
    LinearError,
    Spec,
    Stopped,
    TrackerFileError,
    Unavailable,
    Writers,
)
from ensemble.utils import (
    ConfinedFileError,
    ConfinedReason,
    OutcomeStatus,
    one_line,
    read_confined,
    save,
    timestamp,
)


@dataclass(frozen=True)
class Context:
    ident: str
    issue_id: str
    role: str
    sid: str
    resume: bool
    project: str | None
    workdir: str
    tracker: Tracker  # the claim's or role's app
    humans: tuple[str, ...]
    states: Mapping[str, str]
    repos: Mapping[str, str]
    target: tuple[str, str] | None
    harness: Tracker  # the session comment's app
    # look each comment step up by its ref before posting it, as on resume: its post may
    # have landed with its ledger write lost
    redelivered: bool = False
    host: str = "github.com"
    document: str | None = None
    published: str = ""
    assignee: str = ""
    task: str | None = None


LEDGER = "writeback.json"
# a Stage's first_failure → the tracker state a failed first run moves to
FIRST_FAILURE = {"ready": "todo", "review": "in_review"}
LOCK = "writeback.lock"
MAX_FILE = 1_000_000
PLAN_MARK = "RESUME: phase="
FOOTER = "Answer in a comment, then move this issue back to {state}."
LIMIT = "Usage limit,"
# a failed attempt's own steps; a later valid outcome keeps the normal ones
NO_OUTCOME = "no-outcome:"
INTERRUPTED = ("stopped: Stopped", "stopped: KeyboardInterrupt")
# what a failed tracker read or write raises
ERRORS = (
    LinearError,
    Unavailable,
    OSError,
    subprocess.SubprocessError,
    TrackerFileError,
    IssueError,
)


def context(
    cfg, role, writers: Writers, *, ident, issue_id, sid, resume, project, target, task=None
):
    return Context(
        ident=ident,
        issue_id=issue_id,
        role=role,
        sid=sid,
        resume=resume,
        project=project,
        workdir=cfg.issue_workdir(ident),
        tracker=writers.tracker,
        states=writers.states,
        humans=writers.humans,
        repos=writers.repos,
        target=target,
        harness=writers.harness,
        host=cfg.repo_host(role),
        document=cfg.docs.local_out(role, ident, sid),
        published=_published(cfg.roles[role].run.output, cfg.repo_host(role)),
        assignee=writers.assignee,
        task=task,
    )


def _published(output, host):
    if output["type"] != "github":
        return ""
    return f"https://{host}/{output['repo']}/blob/{output['branch']}/"


def for_run(cfg, role, spec: Spec, writers: Writers):
    return context(
        cfg,
        role,
        writers,
        ident=spec.issue,
        issue_id=spec.uuid,
        sid=spec.sid,
        resume=spec.mode == "resume",
        project=spec.project,
        target=None,
        task=runs.claim(cfg.runs_log, spec.issue, spec.sid).task
        if spec.mode == "resume"
        else spec.task,
    )


def for_claim(cfg, claim: Claim, writers: Writers, *, attempt: int) -> Context:
    """Write through writers resolved from the recorded role's current configuration."""
    return replace(
        context(
            cfg,
            claim.role,
            writers,
            ident=claim.issue,
            issue_id=claim.uuid,
            sid=claim.sid,
            resume=attempt > 1,
            project=claim.project,
            target=claim.target,
            task=claim.task,
        ),
        redelivered=True,
    )


def say(lead, text):
    if not lead:
        return text
    if not text:
        return lead.removesuffix(":")
    return f"{lead}\n{text}" if "\n" in text else f"{lead} {text}"


def approve_line(repo, approved):
    if repo is None:
        return f"**🔴 To approve, move this issue to {approved} with a comment `Repo: <owner>/<name>` naming the target repo.**"
    return (
        f"**🔴 Target repo:** `{repo}` **(from the project mapping). To approve, move this issue to {approved}; "
        "to use another repo, comment** `Repo: <owner>/<name>` **first.**"
    )


def local_time(epoch):
    try:
        return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _error(ctx, step, e):
    log(
        "writeback",
        "step-error",
        ctx.ident,
        sid=ctx.sid,
        step=step,
        error=one_line(e),
        traceback="".join(format_exception(e)),
    )


def _guarded_move(ctx, issue, ident, to, frm):
    now = ctx.tracker.move(issue, MOVES[to], frm=MOVES[frm])
    if now not in (None, ctx.states[to]):
        log("writeback", "skip", ident, reason=f"move to {to}: issue is {now}")


def _attempt(record):
    return (
        isinstance(record, dict)
        and record.keys() <= {"started_at", "seq", "ended_at"}
        and isinstance(record.get("started_at"), str)
        and type(record.get("seq")) is int
        and record["seq"] >= 0
        and isinstance(record.get("ended_at", ""), str)
    )


def _entry(value):
    if not isinstance(value, dict) or not value.keys() <= {"steps", "attempts"}:
        return False
    steps, attempts = value.get("steps", []), value.get("attempts", {})
    return (
        isinstance(steps, list)
        and all(isinstance(s, str) for s in steps)
        and isinstance(attempts, dict)
        and all(n.isdigit() and _attempt(r) for n, r in attempts.items())
    )


def _ledger(ctx):
    """{sid: {"steps": [...], "attempts": {"<n>": {"started_at", "seq", "ended_at"}}}}; else {}."""
    try:
        # O_NONBLOCK: a FIFO there would block a plain open forever
        with os.fdopen(
            os.open(os.path.join(ctx.workdir, LEDGER), os.O_RDONLY | os.O_NONBLOCK), "rb"
        ) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    valid = isinstance(data, dict) and all(_entry(v) for v in data.values())
    return data if valid else {}


@contextlib.contextmanager
def _exclusive(ctx):
    """The run's write-back lock; yields False, logged, if it cannot open."""
    try:
        os.makedirs(ctx.workdir, exist_ok=True)
        fd = os.open(
            os.path.join(ctx.workdir, LOCK),
            os.O_WRONLY | os.O_CREAT | os.O_NONBLOCK,
            0o600,
        )
    except OSError as e:
        _error(ctx, "lock", e)
        yield False
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield True
    finally:
        os.close(fd)


def _done(ctx):
    return _ledger(ctx).get(ctx.sid, {}).get("steps", [])


def _step(ctx, step, write, ref=None):
    if step in _done(ctx):
        return None
    try:
        # on resume or redelivery an unledgered post may have landed with its ledger write lost
        found = (
            ref is not None
            and (ctx.resume or ctx.redelivered)
            and ctx.tracker.find_comment(ctx.issue_id, ref) is not None
        )
        if not found:
            write()
        ledger = _ledger(ctx)
        ledger.setdefault(ctx.sid, {}).setdefault("steps", []).append(step)
        save(os.path.join(ctx.workdir, LEDGER), json.dumps(ledger))
    except ERRORS as e:
        _error(ctx, step, e)
        return e
    log("writeback", "step", ctx.ident, sid=ctx.sid, step=step, found=found or None)
    return None


def _post(ctx, step, body):
    ref = (ctx.sid, step)
    return _step(ctx, step, lambda: ctx.tracker.comment(ctx.issue_id, body, ref), ref)


def progress(ctx, event: Progress) -> bool:
    # raises only Stopped
    try:
        if not isinstance(event, Progress):
            return True
        task = workflow.for_task(TASKS, ctx.role, ctx.task)
        if event.name == "start":
            return _post(ctx, "start", say(task.start, event.text)) is None
        digest = hashlib.sha256(event.text.encode()).hexdigest()[:12]
        return (
            _post(ctx, f"progress:{event.name}:{digest}", f"Progress ({event.name}): {event.text}")
            is None
        )
    except Exception as e:  # noqa: BLE001  # A sink failure must not stop the run.
        _error(ctx, "sink", e)
        return False


def _read_file(path, workdir):
    try:
        return read_confined(path, workdir, limit=MAX_FILE)
    except ConfinedFileError as error:
        reasons = {
            ConfinedReason.REGULAR: "not a regular file",
            ConfinedReason.SUFFIX: "not a .md file",
            ConfinedReason.OUTSIDE: "not under the workdir",
            ConfinedReason.TOO_LARGE: f"over {MAX_FILE} bytes",
        }
        raise ValueError(reasons[error.reason]) from None


def _url(ctx, url):
    if not url or TASKS[ctx.role].input != "build":
        return url
    on = (f"https://{ctx.host}/{ctx.target[0]}/{ctx.target[1]}/",) if ctx.target else ()
    if any(url.lower().startswith(p.lower()) for p in (*on, ctx.published) if p):
        return url
    log(
        "writeback",
        "skip",
        ctx.ident,
        reason=f"url not on {'/'.join(ctx.target) if ctx.target else 'a target repo'}",
    )
    return ""


def _scope(attempt: int) -> str:
    """Outcome steps' prefix `attempt:<n>:`; none at attempt 1 (older ledgers)."""
    return "" if attempt == 1 else f"attempt:{attempt}:"


def finish(ctx, outcome: Outcome, *, steps="", move=True) -> bool:
    # never raises: on Stopped returns False, so the session comment's end is still written
    try:
        return _finish(ctx, outcome, steps, move=move)
    except (Exception, Stopped) as e:  # noqa: BLE001  # Report the session after failed steps.
        _error(ctx, "finish", e)
        return False


def _completion_body(ctx, o):
    task = workflow.for_task(TASKS, ctx.role, ctx.task)
    url = "" if task.input == "decompose" else _url(ctx, o.url)
    if ctx.document and task.input != "decompose":
        url = Path(ctx.document).as_uri() if o.status == OutcomeStatus.DONE else ""
    if o.status == OutcomeStatus.DONE:
        lead, text, state = (
            task.done,
            o.summary,
            "done" if task.input == "decompose" else "in_review",
        )
        if task.input == "build" and text and not task.deliverable:
            text = "Agent report before delivery:\n\n" + "\n".join(
                f"> {line}" for line in text.splitlines()
            )
    elif o.status == OutcomeStatus.NEEDS_INPUT:
        questions = "\n".join(f"{n}. {q}" for n, q in enumerate(o.questions, 1))
        footer = FOOTER.format(state=ctx.tracker.state_name(IssueState.READY))
        text = f"{questions}\n\n{footer}" + (f" {task.hint}" if task.hint else "")
        lead, state = task.question, "in_review"
    else:
        lead, text, state = (
            task.failed,
            o.summary,
            "in_review" if ctx.resume else FIRST_FAILURE[task.first_failure],
        )
    body = say(lead, text)
    if url and (
        o.status == OutcomeStatus.FAILED or o.status == OutcomeStatus.DONE and task.input != "build"
    ):
        body += f"\n\n{url}"
    if task.approve and o.status == OutcomeStatus.DONE:
        body += f"\n\n{approve_line(ctx.repos.get(ctx.project), ctx.tracker.state_name(IssueState.APPROVED))}"

    return body, state, url


def _post_files(ctx, o, body):
    posts = []
    for path in o.files:
        name = os.path.basename(path)
        try:
            data = _read_file(path, ctx.workdir)
        except (OSError, ValueError) as e:
            body += f"\n\nCould not post the file `{name}`: {one_line(e)}"
            continue
        text = data.decode("utf-8", errors="replace")
        posts.append(
            (
                "Plan" if PLAN_MARK in text else "Spec",
                name,
                text,
                hashlib.sha256(data).hexdigest()[:12],
            )
        )
    for kind, name, text, digest in sorted(posts, key=lambda p: p[0] == "Plan"):
        if err := _post(ctx, f"file:{name}:{digest}", f"**{kind}** `{name}`\n\n---\n\n{text}"):
            body += f"\n\nCould not post the {kind} `{name}`: {one_line(err)}"
    return body


def _report(ctx, body):
    """The saved deliverable (`run/out.md` or the document), else None."""
    path = ctx.document or os.path.join(ctx.workdir, "run", "out.md")
    try:
        text = _read_file(path, os.path.dirname(path)).decode("utf-8", errors="replace")
        if not text.strip():
            raise ValueError("empty")
    except (OSError, ValueError) as e:
        return (
            None,
            body + f"\n\nCould not post the report `{os.path.basename(path)}`: {one_line(e)}",
        )
    return text, body


def _attach(ctx, url, title, attached):
    if url not in attached:
        ctx.tracker.link(ctx.issue_id, url, title)


def _assign(ctx):
    """A failure is a note, as notify's."""
    try:
        ctx.tracker.assign(ctx.issue_id, ctx.assignee)
    except (LinearError, Unavailable, OSError, subprocess.SubprocessError) as e:
        return f"\n\nCould not assign {ctx.assignee}: {one_line(e)}"
    return ""


def _complete_source(ctx):
    source_handoff = handoff.parse_handoff(ctx.tracker.description(ctx.ident))
    if source_handoff is None:
        raise IssueError(f"{ctx.ident}: missing source PRD handoff")
    source = ctx.tracker.on_board(source_handoff.source)
    if source is None:
        raise IssueError(f"{source_handoff.source}: source PRD not on this board")
    state = ctx.tracker.move(source, IssueState.DONE, frm=IssueState.APPROVED)
    if state not in (None, ctx.states["done"]):
        raise IssueError(f"{source_handoff.source}: source PRD left Handoff (state {state})")


def _finish(ctx, o, steps="", *, move=True):
    task, issue = workflow.for_task(TASKS, ctx.role, ctx.task), ctx.issue_id
    body, state, url = _completion_body(ctx, o)

    try:
        attached = ctx.tracker.links(issue)
    except ERRORS as e:
        _error(ctx, "read", e)
        return False

    if task.files and o.status != OutcomeStatus.NEEDS_INPUT:
        body = _post_files(ctx, o, body)
    report = None
    if task.deliverable and o.status == OutcomeStatus.DONE:
        report, body = _report(ctx, body)

    if (
        task.retitle
        and o.status == OutcomeStatus.DONE
        and _step(
            ctx,
            f"{steps}retitle",
            lambda: ctx.tracker.retitle(
                issue, f"{task.title_prefix}: {o.title}" if task.title_prefix else o.title
            ),
        )
    ):
        return False
    if state == "in_review" and move:
        if task.assign and o.status == OutcomeStatus.NEEDS_INPUT and ctx.assignee:
            body += _assign(ctx)
        body += ctx.tracker.notify(issue, ctx.humans)
    if _post(ctx, f"{steps}comment", body):
        return False
    if report is not None and _post(ctx, f"{steps}deliverable", report):
        return False

    if (
        o.status == OutcomeStatus.DONE
        and url
        and not ctx.document
        and _step(ctx, f"attach:{url}", lambda: _attach(ctx, url, o.title, attached))
    ):
        return False

    if (
        move
        and task.input == "decompose"
        and o.status == OutcomeStatus.DONE
        and _step(ctx, f"{steps}source:done", lambda: _complete_source(ctx))
    ):
        return False

    if move and _step(
        ctx,
        f"{steps}move:{state}",
        lambda: _guarded_move(ctx, issue, ctx.ident, state, "in_progress"),
    ):
        return False
    # complete: Recover replays a valid outcome until this is ledgered
    return _step(ctx, f"{steps}outcome", lambda: None) is None


def _session(ctx, cfg, client, attempt, *, now, end=None) -> bool:
    """The attempt's session comment: running at Begun, its `end` at Finished, each once."""
    record = _ledger(ctx).get(ctx.sid, {}).get("attempts", {}).get(str(attempt))
    record = record or {"started_at": now(), "seq": 0}
    if "ended_at" in record or end is None and record["seq"]:
        return True
    rec = run_comments.base(
        sid=ctx.sid,
        workdir=ctx.workdir,
        client=client,
        started_at=record["started_at"],
        info=lambda: LinearRun.attach(ctx.ident, cfg=cfg, sid=ctx.sid).delegate.session_status(),
        seq=record["seq"],
    )
    if end is None:
        error = run_comments.post(ctx.ident, rec, tracker=ctx.harness)
    else:
        ended_at = now()
        rc, outcome, limited = end
        error = run_comments.post(
            ctx.ident,
            rec,
            tracker=ctx.harness,
            rc=rc,
            ended_at=ended_at,
            outcome=outcome,
            limit=limited,
        )
        if error is None:
            record = {**record, "ended_at": ended_at}
            error = run_comments.repair(ctx.ident, ctx.issue_id, rec, tracker=ctx.harness)
    if error is None or "ended_at" in record:
        record = {**record, "seq": rec["seq"]}
    try:
        ledger = _ledger(ctx)
        ledger.setdefault(ctx.sid, {}).setdefault("attempts", {})[str(attempt)] = record
        save(os.path.join(ctx.workdir, LEDGER), json.dumps(ledger))
    except OSError as e:
        error = error or one_line(e)
    if error:
        log("writeback", "registry-error", ctx.ident, sid=ctx.sid, error=error)
    return error is None


def _limited(ctx, attempt, resets_at, *, move=True) -> bool:
    """A usage limit: its comment once per sid and guarded move to In Review per attempt."""
    at = local_time(resets_at)
    step = f"limit-move:{attempt}"
    if step in _done(ctx):
        return True
    if (
        _post(
            ctx,
            "limit",
            say(LIMIT, f"resets at {at}." if at else "reset time unknown.")
            + ctx.tracker.notify(ctx.issue_id, ctx.humans),
        )
        is not None
    ):
        return False
    moved_ok = (
        not move
        or _step(
            ctx,
            step,
            lambda: _guarded_move(ctx, ctx.issue_id, ctx.ident, "in_review", "in_progress"),
        )
        is None
    )
    return moved_ok


def _claim(cfg, issue, sid) -> Claim:
    claim = runs.claim(cfg.runs_log, issue, sid)
    if claim.role not in TASKS:
        raise ConfigError(f"{issue}: role {claim.role!r} has no write-back")
    return claim


def _replayed_attempt(begun, rows) -> int:
    """The latest Begun's attempt, the next when a start or resume row follows it (its Begun
    lost); without a Begun, the rows' count.
    """
    if not begun:
        return len(rows)
    last = max(int(n) for n in begun)
    since = datetime.fromisoformat(begun[str(last)]["started_at"]).astimezone()
    return last + any(e.ts > since for e in rows)


def replay(
    cfg,
    issue: str,
    sid: str,
    outcome: Outcome,
    *,
    writers: Callable[[Claim], Writers],
    dry=False,
    now=timestamp,
) -> bool | None:
    """Recover's write-back of the ended (issue, sid) run's outcome, each step once: None when
    its ledger has it complete, else whether it now is (dry: True, nothing written).
    """
    rows = [e for e in runs.parse_log(cfg.runs_log) if (e.issue, e.sid) == (issue, sid)]
    claim = _claim(cfg, issue, sid)
    ctx = for_claim(cfg, claim, writers(claim), attempt=len(rows))
    attempt = _replayed_attempt(_ledger(ctx).get(sid, {}).get("attempts", {}), rows)
    ctx, scope = replace(ctx, resume=attempt > 1), _scope(attempt)
    if f"{scope}outcome" in _done(ctx):
        return None
    if dry:
        return True
    with _exclusive(ctx) as held:
        if not held:
            return False
        done = f"{scope}outcome" in _done(ctx) or finish(ctx, outcome, steps=scope)
        end = (None, outcome, False)
        _session(ctx, cfg, claim.client, attempt, now=now, end=end)
        return done


def deliver(
    cfg,
    issue: str,
    workdir: str,
    event: DelegateEvent,
    *,
    writers: Callable[[Claim], Writers],
    now=timestamp,
) -> bool:
    """Writes the (issue, event.sid) run's event back as its start row's claim, each step once;
    False when a write failed; ConfigError or ValueError before any write.
    """
    if not re.fullmatch(ISSUE_ID, issue):
        raise ConfigError(f"bad issue id: {issue}")
    if os.path.abspath(workdir) != os.path.abspath(cfg.issue_workdir(issue)):
        raise ConfigError(f"--workdir {workdir}: {issue}'s is {cfg.issue_workdir(issue)}")
    attempt = event.attempt
    if attempt is None:  # DelegateEvent requires one
        raise ValueError("attempt: missing")
    claim = _claim(cfg, issue, event.sid)
    ctx = for_claim(cfg, claim, writers(claim), attempt=attempt)
    with _exclusive(ctx) as held:
        return held and _deliver(ctx, cfg, claim, event, now=now)


def _deliver(ctx, cfg, claim, event, *, now) -> bool:
    attempt = event.attempt
    session = functools.partial(_session, ctx, cfg, claim.client, attempt, now=now)
    if event.kind == "begun":
        registered = session()
        return _running(ctx, attempt) and registered
    if event.kind == "progress":
        return _running(ctx, attempt) and progress(
            ctx, Progress(event.name or "", event.text or "")
        )
    # Finished: outcome (once), limit, session end; stale: no move
    outcome, move = event.outcome, not _stale(ctx, attempt)
    if (
        outcome is None
        and event.limit is None
        and event.error
        and not event.error.startswith(INTERRUPTED)
    ):
        failed = Outcome(OutcomeStatus.FAILED, "The agent run failed", event.error)
        steps = f"{NO_OUTCOME}{attempt}:"
        ok = f"{steps}outcome" in _done(ctx) or finish(ctx, failed, steps=steps, move=move)
    else:
        steps = _scope(attempt)
        ok = (
            outcome is None
            or f"{steps}outcome" in _done(ctx)
            or finish(ctx, outcome, steps=steps, move=move)
        )
    if event.limit is not None:
        ok = _limited(ctx, attempt, event.limit["resets_at"], move=move) and ok
    elif outcome is None:
        log("writeback", "no-outcome", ctx.ident, sid=ctx.sid, error=event.error or "no result")
    ended = session(end=(event.returncode, outcome, event.limit is not None))
    log("writeback", "end", ctx.ident, sid=ctx.sid, exit=event.returncode)
    return ended and ok


def _stale(ctx, attempt) -> bool:
    """A later attempt is ledgered."""
    return any(int(n) > attempt for n in _ledger(ctx).get(ctx.sid, {}).get("attempts", {}))


def _running(ctx, attempt) -> bool:
    ended = _ledger(ctx).get(ctx.sid, {}).get("attempts", {}).get(str(attempt), {})
    if _stale(ctx, attempt) or "ended_at" in ended:
        return True

    def activate():
        ctx.tracker.move(ctx.issue_id, IssueState.RUNNING)

    return _step(ctx, f"running:{attempt}", activate) is None


def bounce(ctx, issue: Issue, invalid: Invalid) -> None:
    reason = invalid.reason
    tracker, h = ctx.tracker, issue.handoff
    src = tracker.on_board(h.source) if h else None
    if h is not None and src:
        text = (
            f"Repo check failed: {reason}. To build it, move {h.source} to "
            f"{tracker.state_name(IssueState.APPROVED)} again with a comment "
            + (
                "`Ref: <branch or tag>` naming the ref."
                if invalid.line == "Ref"
                else "`Repo: <owner>/<name>` naming the target repo."
            )
        ) + (
            f" Or fix {LOCAL}'s [orchestrator.linear.project_repos] entry."
            if invalid.mapped
            else ""
        )
        with contextlib.suppress(*ERRORS):
            tracker.unarchive(src)
        notes = tracker.notify(src, ctx.humans)
        tracker.comment(src, text + notes)
        _guarded_move(ctx, src, h.source, "in_review", "done")
        tracker.comment(ctx.issue_id, text)
        _guarded_move(ctx, ctx.issue_id, ctx.ident, "canceled", "in_progress")
    else:
        notes = tracker.notify(ctx.issue_id, ctx.humans)
        tracker.comment(
            ctx.issue_id,
            say(
                "Question:",
                f"repo check failed: {reason}. Fix the description's `{invalid.line}:` "
                f"line, then move this issue back to {tracker.state_name(IssueState.READY)}.",
            )
            + notes,
        )
        _guarded_move(ctx, ctx.issue_id, ctx.ident, "in_review", "in_progress")
