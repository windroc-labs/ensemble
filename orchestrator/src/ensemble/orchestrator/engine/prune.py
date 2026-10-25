"""Prune, for each finished issue (Done, Canceled, or any other state of type canceled or duplicate,
such as Duplicate: ops.Team.finished) at least 2 hours ago (linear_tracker.finished_at; unknown: skipped) whose
workdir <work_dir>/work/<ID> its Delegate (its latest recorded run's kind, else the
configured one) reports agent files in (Delegate.checkouts not None):
- closes its recorded run through LinearRun; a failed close leaves the rest for a later tick;
- deletes its agent files, uncommitted work included, through its Delegate (Delegate.clean; its
  refusal skips the rest); the run records and the rest of the workdir stay until then;
- in the local clone of each worktree among its checkouts, unless that clone lies in
  <work_dir>/work or a temp dir: `worktree prune`, then deletes each <ID>-* branch but the default
  that origin/<branch> or origin/<default> holds, else keeps and reports it (git reads refs only,
  so code an agent run planted, such as filters or hooks, never runs outside auto mode's review);
- archives every finished issue of the team, a Done one only when assigned to the pm or engineer
  role account (a researcher's report stays), any other whatever its assignee, none included, unless
  it (reopened since) or a sub-issue, at any depth, is unfinished, every page of them read first
  (Tracker.archive; Linear archives sub-issues with their parent, theirs too): skipped until each
  is finished or archived.

For each stalled issue (any state but Todo, In Progress or a finished one) whose latest move into its
state and latest recorded run are both at least 7 days old, it deletes its agent files' checkouts and
temp files through its Delegate (Delegate.clean scratch; its refusal, a run or agent session still
open included, skips it), keeping the rest and its run records, closing no run; then only `worktree
prune` in each worktree's local clone, keeping every branch (it may back an open PR).

For each issue finished at least 7 days ago whose workdir is a real dir without agent files
(and of a configured kind), it deletes the whole workdir (run records, write-back ledger) once its
Delegate's clean finds no live run (its refusal skips it). An issue without agent files is not
attached; one of a kind no longer configured is neither queried nor logged. Run inventory unknown: an
error, and no issue is pruned. Each step is logged (event_log.log, src prune); a failure is logged
and the rest goes on (run returns 3), but Unavailable is raised. Promote prunes after each of its
ticks; `ensemble-orchestrator prune` (main, HELP) runs one alone.

A tick's requests past its board's setup (Linear: one read): SYNC_PAGES + READ_REQUESTS +
ARCHIVE_REQUESTS at most, the rest waits (prune-deferred); reads cached in <logs_dir>/prune.json,
its cursor the tracker's time.
"""

import functools
import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta

from ensemble.delegate import ConfigError as DelegateConfigError
from ensemble.delegate import SessionError
from ensemble.orchestrator.engine import config, prune_state, runs
from ensemble.orchestrator.engine import log as event_log
from ensemble.orchestrator.engine.flow.types import (
    ISSUE_ID,
    ArchiveRefused,
    IssueState,
)
from ensemble.orchestrator.engine.linear_run import LinearRun
from ensemble.orchestrator.engine.log import linear_error, log
from ensemble.orchestrator.engine.types import (
    TRACKER_ERRORS,
    ConfigError,
    Entry,
    LinearError,
    PruneState,
    Refusal,
    TrackerFileError,
    Unavailable,
)
from ensemble.utils import (
    GUARD,
    SHORT,
    GitError,
    StatusState,
    common_dir,
    one_line,
    remove_tree,
    under,
)
from ensemble.utils import run_git as git

QUARANTINE = timedelta(hours=2)
RETENTION = timedelta(days=7)
STALL = timedelta(days=7)
IDENT_RE = re.compile(ISSUE_ID)
DELETES = {False: "agent dir", True: "checkouts and temp files"}  # by remove_agent's scratch
ARCHIVE_ROLES = ("pm", "engineer")
SYNC_PAGES, READ_REQUESTS, ARCHIVE_REQUESTS = 3, 10, 20  # a tick's Linear requests, by phase
BACKSTOP = timedelta(days=1)  # an entry the feed never names is read again after it
OVERLAP = timedelta(minutes=10)  # Linear's lists lag a change
CACHE = "prune.json"
TOO_BIG = "archive: too many sub-issues to read in one tick"
HELP = """Clean finished issues' recorded runs and files, then archive eligible issues.

Usage: ensemble-orchestrator prune [--dry-run]

Arguments and options:
  --dry-run   Print the cleanup plan without closing, deleting or archiving.
              Read the board, configuration, local runs/checkouts and
              Delegate status; on Linear this is not an offline or
              credentials-free preview.
  -h, --help  Print this help alone; exit 0.
  --dry-run may repeat with no further effect; other arguments exit 2.

Prerequisites and effects:
  Requires configured Delegate settings and access to the configured
  work_dir/local clones; on Linear ([orchestrator] tracker = "linear") also
  Linear access and linear.harness_key Keychain credentials. A local board
  ($ENSEMBLE_BOARD_DIR) needs no account; no job runs it on a timer: run it
  (or promote) on demand. It archives by moving issue files flat into
  <board>/archive/; an unreadable board exits 2 (prune.py: <path>: <reason>).
  Configuration and executable lookup: ensemble-orchestrator --help
  For issues Done/Canceled/Duplicate (any canceled-type state) at least 2
  hours (the latest of their move into one and Linear's canceledAt and
  completedAt; unknown age is skipped), close recorded runs and
  delete agent files, including uncommitted work, through their Delegate.
  Close/cleanup refusal preserves later steps for a future tick. Unknown run
  inventory prevents run/file pruning; archival still proceeds.
  Prune worktree records in each checkout's local clone and remove pushed
  ISSUE-* branches already contained in origin/branch or origin/default;
  preserve the default branch and unpushed branches. No fetch is performed.
  Clones under work_dir/work or temporary directories are skipped.
  Archive team issues finished at least 2 hours ago: Canceled/Duplicate
  whatever the assignee (none or researcher too), Done only when assigned to
  pm/engineer role accounts (researcher reports stay). Unknown finish times
  are skipped, and so is an issue with an unfinished sub-issue at any depth
  (archiving it archives its sub-issues and theirs).
  For issues in any other state but Todo/In Progress, 7 days after both their
  latest move into it and their latest recorded run, delete their checkouts
  and temp files through their Delegate, keeping the other agent files and the
  run records; an open run or agent session skips it. Prune worktree records
  only; every branch is kept.
  After 7 days, remove whole workdirs without agent files, including run records
  and delivery ledgers, only when their configured Delegate confirms no live
  run. Unconfigured run kinds are skipped.
  One setup read (team and role accounts), then at most 33 Linear requests: 3
  change-feed pages, 10 issue reads, 20 archive requests; work past a cap
  waits for a later run, and an issue whose sub-issues alone need more is
  not archived. logs/prune.json caches what was read, read again when Linear
  reports a change or after a day (a dry run never writes it). An issue
  Linear no longer has is skipped; one moved to another team finishes by
  its state there.
  Events go to orchestrator.jsonl (src prune); preview JSON lines go to stderr.
  Exit 0: completed; 1: Linear unavailable (logged once a day), unknown
  inventory or failed step after remaining steps; 2: configuration error
  (logged once a day), an unreadable local board or bad arguments.
  Promote also runs this cleanup after each tick.

Example (configured Linear access and finished issues):
  ensemble-orchestrator prune --dry-run
  Inspect the plan before running ensemble-orchestrator prune to delete/archive.

Recovery and next help:
  Resolve logged close/cleanup refusals or unavailable inventory, then retry
  the preview. Back up agent work before allowing cleanup.
  Promotion and its cleanup: ensemble-orchestrator promote --help
  Command discovery: ensemble-orchestrator --help
"""


class Spent(Exception):
    pass


class Budget:
    """gql, counted: a request past limit raises Spent unsent."""

    def __init__(self, gql):
        self.gql, self.left = gql, 0

    def limit(self, n):
        self.left = n

    def spend(self):
        if self.left <= 0:
            raise Spent
        self.left -= 1

    def __call__(self, query, **v):
        self.spend()
        return self.gql(query, **v)


class Pruner:
    def __init__(self, board, gql, now, *, dry, run=None, work, cfg, writable=None):
        self.now, self.dry, self.work = now, dry, work
        self.budget = Budget(gql)
        self.tracker = board(self.budget)
        self.run_git = run or config.shell
        self.cfg = cfg
        self.writable = config.writable(work) if writable is None else writable
        self.errors, self.views, self.classes, self.by_issue = 0, {}, {}, None
        self.finished, self.anchor, self.cold = None, None, False
        self.say = functools.partial(log, "prune", dry=dry)
        self.cache = os.path.join(cfg.logs_dir, CACHE)
        self.state, unreadable = prune_state.load(self.cache)
        if unreadable:
            self.say("prune-skip", once=True, reason=f"{CACHE} unreadable: {unreadable}")

    def error(self, issue=None, **fields):
        self.errors += 1
        self.say("prune-error", issue, **fields)

    def clean_clone(self, ident, common, *, branches=True):
        clone = os.path.dirname(common)
        if root := under(common, self.writable):
            self.say(
                "prune-skip",
                ident,
                once=True,
                reason=f"{clone} is under {root}, so no git runs there",
            )
            return
        if self.dry:
            pushed = f" and pushed {ident}-* branches" if branches else ""
            self.say("prune-plan", ident, what=f"prune {clone}'s worktree records{pushed}")
            return
        at = ["git", *GUARD, "-C", clone, f"--git-dir={common}"]
        try:
            git(clone, at[-1], "worktree", "prune", run=self.run_git, pre=GUARD)
            if not branches:
                return
            head = self.run_git(
                [*at, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD"], SHORT
            )
            default = head.stdout.strip().removeprefix("origin/") if head.returncode == 0 else None
            listed = git(
                clone,
                at[-1],
                "for-each-ref",
                "--format=%(refname:short)",
                f"refs/heads/{ident}-*",
                run=self.run_git,
                pre=GUARD,
            )
            for branch in listed.split():
                if branch == default:
                    continue
                if any(
                    self.run_git(
                        [
                            *at,
                            "merge-base",
                            "--is-ancestor",
                            f"refs/heads/{branch}",
                            f"refs/remotes/origin/{b}",
                        ],
                        SHORT,
                    ).returncode
                    == 0
                    for b in dict.fromkeys(filter(None, (branch, default)))
                ):
                    git(clone, at[-1], "branch", "-D", branch, run=self.run_git, pre=GUARD)
                    self.say("prune-removed", ident, what=f"branch {branch} in {clone}")
                else:
                    self.say(
                        "prune-skip",
                        ident,
                        once=True,
                        reason=f"branch {branch} in {clone} kept: not pushed",
                    )
        except (GitError, OSError, subprocess.TimeoutExpired) as e:
            self.error(ident, error=one_line(e))

    def inventory(self):
        """A run whose view cannot load has nothing to close, logged once; inventory unknown: None."""
        found = {}
        try:
            latest = {ident: runs.latest(rows)[ident] for ident, rows in self.log().items()}
        except (OSError, ValueError):
            return None
        for ident, entry in sorted(latest.items()):
            if entry.role not in self.cfg.roles or not self.has_agent(ident, quiet=True):
                continue
            try:
                view = LinearRun.attach(ident, cfg=self.cfg, sid=entry.sid)
                if view.status().state == StatusState.UNKNOWN:
                    return None
            except (config.ConfigError, DelegateConfigError, OSError, ValueError) as error:
                self.say("prune-skip", ident, once=True, sid=entry.sid, reason=one_line(error))
                continue
            except SessionError:
                return None
            found[ident] = view
        return found

    def close_sessions(self, ident):
        view = self.views.get(ident)
        if view is None:
            return True
        if self.dry:
            self.say("prune-plan", ident, session=view.sid, what="close the run")
            return True
        try:
            view.delegate.close()
        except (
            config.ConfigError,
            DelegateConfigError,
            SessionError,
            OSError,
            ValueError,
        ) as error:
            self.error(ident, session=view.sid, error=one_line(error))
            return False
        self.say("prune-closed", ident, session=view.sid)
        return True

    def archive(self):
        """A trusted refusal skips its plan; ARCHIVE_REQUESTS spent, the rest waits a tick."""
        self.budget.limit(ARCHIVE_REQUESTS)
        try:
            found = self.tracker.finished(ARCHIVE_ROLES)
        except Spent:
            self.say("prune-deferred", archive="all")
            return
        except Unavailable:
            raise
        except TRACKER_ERRORS as e:
            self.error(error=f"archive: {one_line(e)}")
            return
        self.finished = {issue.ident for issue in found}
        self.see(issue.updated for issue in found)
        due = []
        for issue in found:
            if issue.since is None:
                self.say(
                    "prune-skip", issue.ident, once=True, reason="archive: finish time unknown"
                )
            elif self.now - issue.since >= QUARANTINE:
                due.append(issue)
        left = self.budget.left
        if due and left < 2:
            self.say("prune-deferred", archive=len(due))
            return
        for n, issue in enumerate(due):
            alone = self.budget.left == left
            try:
                self.archive_one(issue)
            except Spent:
                if alone:  # its plan alone outgrows a tick: skipped like a refusal
                    versions = {issue.ident: issue.updated}
                    self.state.refused[issue.ident] = Refusal(versions, TOO_BIG, self.now)
                    self.say("prune-skip", issue.ident, once=True, reason=TOO_BIG)
                if rest := len(due) - n - alone:
                    self.say("prune-deferred", archive=rest)
                return

    def archive_one(self, issue):
        ident, refused = issue.ident, self.state.refused
        if ident in refused and self.trusted(refused[ident].read):
            self.say("prune-skip", ident, once=True, reason=refused[ident].reason)
            return
        plan, archived = None, True
        try:
            if self.dry:
                plan = self.tracker.archive_plan(issue.key)
                if plan.outcome == "changed":
                    self.budget.spend()  # the write a real tick sends
            else:
                archived = self.tracker.archive(issue.key)
        except ArchiveRefused as e:
            plan = e.plan
        except Unavailable:
            raise
        except TRACKER_ERRORS as e:
            self.error(ident, error=f"archive: {one_line(e)}")
            return
        refused.pop(ident, None)
        if plan is not None and plan.outcome == "refused":
            versions = {ident: issue.updated} | {s.ident: s.updated for s in plan.unfinished}
            refused[ident] = Refusal(versions, _kept(plan), self.now)
            self.say("prune-skip", ident, once=True, reason=_kept(plan))
        elif self.dry:
            self.say("prune-plan", ident, what="archive")
        elif archived:
            self.say("prune-archived", ident)

    def run(self):
        found, idents = self.inventory(), None
        self.views = found or {}
        self.sync()
        if found is None:
            self.error(error="run inventory unknown")
        else:
            idents = self.listed()
            snapshots = self.fresh(idents or [])
            for ident in idents or []:
                if ident in snapshots:
                    self.prune_issue(ident, snapshots[ident])
        self.archive()
        if not self.dry:
            self.save(idents)
        return 3 if self.errors else 0

    def listed(self):
        """None: unreadable."""
        try:
            return [
                n
                for n in sorted(os.listdir(self.work))
                if IDENT_RE.fullmatch(n) and (self.has_agent(n) or self.bare(n))
            ]
        except OSError:
            return None

    def sync(self):
        """Before any read, drops what the feed names as changed since read, or all when it is
        unknown. The cursor: the feed's latest time, not the host's."""
        changes = None
        self.budget.limit(SYNC_PAGES)
        if self.state.synced is not None:
            try:
                changes = self.tracker.changed(self.state.synced - OVERLAP, SYNC_PAGES)
            except Unavailable:
                raise
            except TRACKER_ERRORS as e:
                self.error(error=f"change feed: {one_line(e)}")
            else:
                self.anchor = changes.latest
        if changes is None or changes.issues is None:
            self.state, self.cold = PruneState(), True
            return
        named, state = changes.issues, self.state
        state.issues = {
            i: e
            for i, e in state.issues.items()
            if not _newer(named, i, e.snapshot and e.snapshot.updated)
        }
        state.refused = {
            i: r
            for i, r in state.refused.items()
            if not any(_newer(named, j, t) for j, t in r.versions.items())
        }

    def see(self, times):
        """A cold tick's anchor: its first read carrying a time."""
        if self.cold and self.anchor is None:
            self.anchor = max(filter(None, times), default=None)

    def trusted(self, read):
        return read <= self.now and self.now - read < BACKSTOP

    def fresh(self, idents):
        """Each issue's snapshot, from its trusted entry, else read now while READ_REQUESTS last:
        least recently read first, by batch unless the batch left it out before."""
        issues, unbatched = self.state.issues, self.state.unbatched
        found = {
            i: issues[i].snapshot for i in idents if i in issues and self.trusted(issues[i].read)
        }
        stale = sorted(
            (i for i in idents if i not in found),
            key=lambda i: (i in issues, issues[i].read if i in issues else self.now),
        )
        batched = [i for i in stale if i not in unbatched]
        singles = [i for i in stale if i in unbatched]
        failed = set()
        self.budget.limit(READ_REQUESTS)
        try:
            size = self.tracker.batch
            for at in range(0, len(batched), size):
                batch = batched[at : at + size]
                got = self.read_into(batch, failed, self.tracker.snapshots)
                found |= got
                left = [i for i in batch if i not in got and i not in failed]
                unbatched.update(left)
                singles += left
            for ident in singles:
                found |= self.read_into([ident], failed, lambda i: {i[0]: self.tracker.read(i[0])})
        except Spent:
            self.say("prune-deferred", issues=len(set(stale) - set(found) - failed))
        return found

    def read_into(self, idents, failed, how):
        """how(idents)'s snapshots, cached; on an error none, idents logged and failed."""
        try:
            got = how(idents)
        except Unavailable:
            raise
        except TRACKER_ERRORS as e:
            for ident in idents:
                self.error(ident, error=f"issue read: {one_line(e)}")
            failed.update(idents)
            return {}
        self.state.issues |= {i: Entry(s, self.now) for i, s in got.items()}
        self.see(s.updated for s in got.values() if s)
        return got

    def save(self, idents):
        state = self.state
        if idents is not None:
            state.issues = {i: e for i, e in state.issues.items() if i in idents}
            state.unbatched &= set(idents)
        if self.finished is not None:
            state.refused = {i: r for i, r in state.refused.items() if i in self.finished}
        state.synced = max(filter(None, (state.synced, self.anchor)), default=None)
        try:
            prune_state.save(self.cache, state)
        except OSError as e:
            self.error(error=f"{CACHE}: {one_line(e)}")

    def prune_issue(self, ident, found):
        if found is None:
            self.say("prune-skip", ident, once=True, reason="not on the board")
            return
        if found.state in (IssueState.READY, IssueState.RUNNING):
            return
        if found.state not in (IssueState.DONE, IssueState.CANCELED):
            self.prune_stalled(ident, found.since)
            return
        since = found.since
        if since is None:
            self.say("prune-skip", ident, once=True, reason="finish time unknown")
        elif self.has_agent(ident, quiet=True):
            if self.now - since >= QUARANTINE and self.close_sessions(ident):
                self.remove_agent(ident)
        elif self.now - since >= RETENTION:
            self.remove_workdir(ident)

    def prune_stalled(self, ident, since):
        if since is None:
            self.say("prune-skip", ident, once=True, reason="stall time unknown")
            return
        since = max([since, *(e.ts for e in self.log().get(ident, ()))])
        if self.now - since >= STALL and self.has_agent(ident, quiet=True):
            self.remove_agent(ident, scratch=True)

    def log(self):
        """runs.jsonl's entries by issue, read once per tick."""
        if self.by_issue is None:
            by_issue = {}
            for entry in runs.parse_log(self.cfg.runs_log):
                by_issue.setdefault(entry.issue, []).append(entry)
            self.by_issue = by_issue
        return self.by_issue

    def delegate(self, ident):
        """The issue's Delegate class, once per tick: its latest recorded run's kind, else the
        configured one; ConfigError (a kind no longer configured) is kept.
        """
        if ident not in self.classes:
            try:
                rows = self.log().get(ident)
                self.classes[ident] = (
                    LinearRun.kind_cls(
                        self.cfg, runs.delegate_of(rows, runs.latest(rows)[ident].sid)
                    )
                    if rows
                    else self.cfg.delegate_cls
                )
            except (config.ConfigError, OSError, ValueError) as error:
                self.classes[ident] = error
        return self.classes[ident]

    def has_agent(self, ident, *, quiet=False):
        found, workdir = self.delegate(ident), os.path.join(self.work, ident)
        if isinstance(found, Exception):
            if not quiet and self.cfg.delegate_cls.checkouts(workdir) is not None:
                self.say("prune-skip", ident, once=True, reason=one_line(found))
            return False
        return found.checkouts(workdir) is not None

    def bare(self, ident):
        """A real workdir without agent files, of a configured kind."""
        workdir = os.path.join(self.work, ident)
        return (
            not isinstance(self.delegate(ident), Exception)
            and os.path.isdir(workdir)
            and not os.path.islink(workdir)
        )

    def remove_workdir(self, ident):
        workdir = os.path.join(self.work, ident)
        try:
            self.delegate(ident).clean(workdir, dry=self.dry)
            if not self.dry:
                remove_tree(workdir)
        except (config.ConfigError, DelegateConfigError) as error:
            self.say("prune-skip", ident, once=True, reason=one_line(error))
            return
        except (SessionError, OSError) as error:
            self.error(ident, error=f"remove: {one_line(error)}")
            return
        if self.dry:
            self.say("prune-plan", ident, what="delete the workdir")
        else:
            self.say("prune-removed", ident, what="workdir")

    def remove_agent(self, ident, *, scratch=False):
        found, workdir = self.delegate(ident), os.path.join(self.work, ident)
        commons = {c for wt in found.checkouts(workdir) or () if (c := common_dir(wt))}
        view = self.views.get(ident)
        try:
            if scratch:
                if not found.clean(workdir, dry=self.dry, scratch=True):
                    return
            elif not (self.dry and view is not None and view.delegate.alive()):
                found.clean(workdir, dry=self.dry)
        except (config.ConfigError, DelegateConfigError) as error:
            self.say("prune-skip", ident, once=True, reason=one_line(error))
            return
        except (SessionError, OSError) as error:
            self.error(ident, error=f"clean: {one_line(error)}")
            return
        if self.dry:
            self.say("prune-plan", ident, what=f"delete the {DELETES[scratch]}")
        else:
            self.say("prune-removed", ident, what="scratch" if scratch else "agent dir")
        for common in sorted(commons):
            self.clean_clone(ident, common, branches=not scratch)


def _newer(named, ident, version):
    return ident in named and (version is None or named[ident] > version)


def _kept(plan):
    """Why the archive rule keeps plan's issue: its first unfinished sub-issue, else itself."""
    sub = next((s for s in plan.unfinished if s.key != plan.key), None)
    if sub is not None:
        return f"archive: sub-issue {sub.ident} not finished"
    return f"archive: {plan.ident} not finished"


def main(argv, *, trackers, gql, now=None, config_path=None, pruner=Pruner):
    if argv in (["--help"], ["-h"]):
        print(HELP)
        return 0
    if any(a != "--dry-run" for a in argv):
        print(HELP, file=sys.stderr)
        return 2
    dry = "--dry-run" in argv
    os.environ["PATH"] = config.path()
    try:
        cfg = config.load(path=config_path)
    except ConfigError as error:
        config.report_error("prune", error, dry=dry)
        return 2
    event_log.use_logs(cfg.logs_dir)
    try:
        trackers[cfg.tracker].credentials(cfg, "prune", dry=dry)
        board = trackers[cfg.tracker].board(gql, cfg)
        code = pruner(
            board, gql, now or datetime.now(UTC), dry=dry, work=cfg.workdirs, cfg=cfg
        ).run()
    except TrackerFileError as e:
        print(f"prune.py: {e}", file=sys.stderr)
        return 2
    except LinearError as e:
        return event_log.config_error("prune", e, dry=dry)
    except Unavailable as e:
        return linear_error("prune", e, dry=dry)
    return 1 if code else 0
