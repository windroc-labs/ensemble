"""Shared writeback output expectations."""

import functools
import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ensemble.delegate import DelegateEvent, Outcome, Progress
from ensemble.delegate.engine.types import Begun, Finished, Result
from ensemble.orchestrator import cli
from ensemble.orchestrator.engine import (
    config,
    log,
    router,
    runs,
    writeback,
)
from ensemble.orchestrator.engine.flow import handoff
from ensemble.orchestrator.engine.flow.types import (
    Issue,
)
from ensemble.orchestrator.engine.linear_run import LinearRun
from ensemble.orchestrator.engine.types import (
    Claim,
    ConfigError,
    Invalid,
    IssueError,
    LinearError,
    Stopped,
    TrackerFileError,
    TrackerKind,
    Writers,
)
from ensemble.orchestrator.trackers.linear import (
    linear_tracker,
    ops,
)
from ensemble.orchestrator.trackers.linear.linear_tracker import LinearTracker
from ensemble.session import SessionStatus
from ensemble.utils import OutcomeStatus
from tests.orchestrator.case import SID, Case
from tests.orchestrator.linear_run_case import SID as RUN_SID
from tests.orchestrator.linear_run_case import ReadyDelegate, RunCase
from tests.orchestrator.unit import board_ids, run_fixtures
from tests.orchestrator.unit.clock import logs_at
from tests.orchestrator.unit.run_fixtures import logged, show
from tests.orchestrator.unit.writeback_fixtures import APPROVE_MAPPED, DOC, PR, PRD

ID, UUID = "ENG-7", "11111111-2222-4333-8444-555555555555"

OTHER_SID = "9c1d2e3f-4a5b-4c6d-8e7f-a0b1c2d3e4f5"

SRC_UUID = "99999999-8888-4777-8666-555555555555"

OTHER_ISSUE = "77777777-6666-4555-8444-333333333333"

HUMANS = ("ann@example.com", "bob@example.com")

STATES = board_ids.STATES

PROJECT = "121166b1-191a-4461-bec4-42f1c2dc0ddd"

REPOS = {PROJECT: "acme/ensemble"}

TARGET = ("acme", "ensemble")

TREE = "https://github.com/acme/ensemble/tree/ENG-7-session-registry"

SPEC_TEXT = "# Spec\n\nBuild the registry.\n"

PLAN_TEXT = "# Plan\n\nRESUME: phase=2\n\n- [ ] task 1\n"

FOOTER = "Answer in a comment, then move this issue back to Todo."

APPROVE = "**🔴 To approve, move this issue to Handoff with a comment `Repo: <owner>/<name>` naming the target repo.**"

NOT_FOUND = LinearError("linear api error: [{'message': 'Entity not found: Comment'}]")

START = Progress("start", "x")

T = datetime(2026, 10, 11, 10, 0, tzinfo=UTC).timestamp()

NAMES = {
    ops.M_SUBSCRIBE: "subscribe",
    ops.M_COMMENT: "comment",
    ops.M_COMMENT_ID: "comment",
    ops.Q_COMMENT: "find",
    ops.M_STATE: "state",
    linear_tracker.M_TITLE: "title",
    linear_tracker.M_ASSIGN: "assign",
    linear_tracker.M_ATTACH: "attach",
    linear_tracker.M_UNARCHIVE: "unarchive",
    linear_tracker.Q_ATTACHED: "read",
    ops.Q_ISSUE_STATE: "reread",
    linear_tracker.Q_ID: "id",
}

FIELDS = {
    "subscribe": "issueSubscribe",
    "comment": "commentCreate",
    "state": "issueUpdate",
    "title": "issueUpdate",
    "assign": "issueUpdate",
    "attach": "attachmentLinkURL",
    "unarchive": "issueUnarchive",
}


def mine(body, issue=UUID):
    return {"body": body, "issue": issue, "mine": True}


READ = ("read", {"i": UUID})


def reread(i=UUID):
    return ("reread", {"i": i})


def sub(e, i=UUID):
    return ("subscribe", {"i": i, "e": e})


def cid(step, sid=SID):
    return ops.client_id(sid, step)


def post(step, b, sid=SID):
    return ("comment", {"c": cid(step, sid), "i": UUID, "b": b})


def find(step):
    return ("find", {"c": cid(step)})


def comment(b, i=UUID):
    return ("comment", {"i": i, "b": b})


def move(s, i=UUID):
    return ("state", {"i": i, "s": STATES[s]})


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def spec_comment(name="ENG-7-spec.md", text=SPEC_TEXT):
    return post(f"file:{name}:{sha(text)}", f"**Spec** `{name}`\n\n---\n\n{text}")


def plan_comment(name="ENG-7-plan.md", text=PLAN_TEXT):
    return post(f"file:{name}:{sha(text)}", f"**Plan** `{name}`\n\n---\n\n{text}")


def expect(
    *,
    body,
    state,
    files=(),
    title=None,
    subscribe=True,
    attach=None,
    attach_title=None,
    resume=False,
):

    def posted(call):
        return [("find", {"c": call[1]["c"]}), call] if resume else [call]

    calls = [READ, *(c for f in files for c in posted(f))]
    if title:
        calls.append(("title", {"i": UUID, "t": title}))
    if subscribe:
        calls += [sub(e) for e in HUMANS]
    calls += posted(post("comment", body))
    if attach:
        calls.append(("attach", {"i": UUID, "u": attach, "t": attach_title}))
    return calls + [reread(), move(state)]


HANDOFF = (
    "Handoff from PRD-3: https://linear.app/t/issue/PRD-3\n\n## Source\n- PRD: x\n\n"
    "## Instructions\nBuild it.\n\n## Comments\n* Ann:\n  > Repo: acme/other"
)


def issue(description):
    return Issue(
        UUID,
        ID,
        f"https://linear.app/t/issue/{ID}",
        "ENG: Session registry",
        description,
        "2026-09-01T00:00:00.000Z",
        None,
        (),
        (),
        (),
        handoff=handoff.parse_handoff(description),
    )


class Gql:
    def __init__(
        self,
        states=(STATES["in_progress"],),
        attachments=(),
        fail=lambda _name, _v: None,
        issue=None,
        comments=None,
    ):
        self.calls, self.states, self.attachments, self.fail, self.issue = (
            [],
            list(states),
            attachments,
            fail,
            issue,
        )
        self.comments = dict(comments or {})

    def __call__(self, query, **v):
        if query == linear_tracker.Q_STATE_NAME:
            return {"workflowState": {"name": board_ids.STATE_NAMES[v["i"]]}}
        name = NAMES[query]
        self.calls.append((name, v))
        r = self.fail(name, v)
        if isinstance(r, BaseException):
            raise r
        if name == "read":
            return {"issue": {"attachments": {"nodes": [{"url": u} for u in self.attachments]}}}
        if name == "reread":
            s = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            return {"issue": {"state": {"id": s}}}
        if name == "id":
            return {"issue": self.issue}
        if name == "find":
            c = self.comments.get(v["c"])
            if c is None:
                raise NOT_FOUND
            user = {"isMe": c["mine"]}
            return {
                "comment": {
                    "id": v["c"],
                    "body": c["body"],
                    "issue": {"id": c["issue"]},
                    "user": user,
                }
            }
        if name == "comment" and "c" in v and r is not False:
            self.comments[v["c"]] = mine(v["b"], v["i"])
        return {FIELDS[name]: {"success": r is not False}}


def tracker(gql):
    return LinearTracker(gql, states=STATES, team_id=board_ids.TEAM)


FILE_ERROR = TrackerFileError("/board/engineer/running/ENG-7.md", "changed while writing")


class Refusing:
    """A tracker whose comment and links read raise error."""

    def __init__(self, error):
        self.error = error

    def comment(self, *_):
        raise self.error

    def links(self, _key):
        raise self.error


class Base(Case):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        log.use_logs(os.path.join(self.tmp, "logs"))

    def ctx(self, role="engineer", *, gql=None):
        workdir = tempfile.mkdtemp(dir=self.tmp)
        gql = gql or Gql()
        return writeback.Context(
            ident=ID,
            issue_id=UUID,
            role=role,
            sid=SID,
            resume=False,
            project=None,
            workdir=workdir,
            tracker=tracker(gql),
            humans=HUMANS,
            states=STATES,
            repos=REPOS,
            target=TARGET,
            harness=LinearTracker(gql),
        )

    def write(self, ctx, rel, text):
        path = os.path.join(ctx.workdir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return path

    def files(self, ctx):
        return (
            self.write(ctx, "src/ensemble/docs/ENG-7-spec.md", SPEC_TEXT),
            self.write(ctx, "src/ensemble/docs/ENG-7-plan.md", PLAN_TEXT),
        )

    def lines(self):
        events = logged()
        self.assertEqual(
            {(e["src"], e.get("sid", SID)) for e in events} - {("writeback", SID)}, set()
        )
        return [show({k: v for k, v in e.items() if k != "sid"}) for e in events]

    def ledger(self, ctx):
        with open(os.path.join(ctx.workdir, "writeback.json")) as f:
            return json.load(f)

    def steps(self, ctx):
        return {sid: entry["steps"] for sid, entry in self.ledger(ctx).items()}

    def no_ledger(self, ctx):
        self.assertFalse(os.path.exists(os.path.join(ctx.workdir, "writeback.json")))


class ProgressReporting(Base):
    def test_start_once_per_sid(self):
        gql = Gql()
        ctx = self.ctx(gql=gql)
        start = Progress("start", "first build of the PRD")
        writeback.progress(ctx, start)
        writeback.progress(ctx, start)
        self.assertEqual(gql.calls, [post("start", "Build started: first build of the PRD")])
        self.assertEqual(self.steps(ctx), {SID: ["start"]})
        self.assertEqual(self.lines(), [f"step {ID} step=start"])
        writeback.progress(replace(ctx, sid=OTHER_SID), start)
        self.assertEqual(
            gql.calls[1:], [post("start", "Build started: first build of the PRD", OTHER_SID)]
        )

    def test_start_comment_is_the_roles_lead_and_the_runs_pick(self):
        pick = "light-build: a template wording tweak, like TASK-226."
        for role, text, body in (
            ("researcher", "budget 2 rounds", "Research started: budget 2 rounds"),
            ("pm", "budget 2 rounds", "PRD started: budget 2 rounds"),
            ("engineer", pick, f"Build started: {pick}"),
        ):
            with self.subTest(role=role, text=text):
                gql = Gql()
                writeback.progress(self.ctx(role, gql=gql), Progress("start", text))
                self.assertEqual(gql.calls, [post("start", body)])

    def test_ignores_begun_and_finished(self):
        gql = Gql()
        ctx = self.ctx(gql=gql)
        writeback.progress(ctx, Begun(SID))
        writeback.progress(
            ctx, Finished(Result(0, Outcome(status="done", summary="x", title="Queue options")))
        )
        self.assertEqual(gql.calls, [])
        self.assertEqual(self.lines(), [])

    def test_posts_every_other_mark_once_per_text(self):
        gql = Gql()
        ctx = self.ctx(gql=gql)
        for text in ("12 passing", "12 passing", "13 passing"):
            writeback.progress(ctx, Progress("tests", text))
        steps = [f"progress:tests:{sha('12 passing')}", f"progress:tests:{sha('13 passing')}"]
        self.assertEqual(
            gql.calls,
            [
                post(steps[0], "Progress (tests): 12 passing"),
                post(steps[1], "Progress (tests): 13 passing"),
            ],
        )
        self.assertEqual(self.steps(ctx), {SID: steps})

    def test_swallows_system_exit_and_retries_later(self):
        gql = Gql(fail=lambda _name, _v: LinearError("linear api error: down"))
        ctx = self.ctx(gql=gql)
        writeback.progress(ctx, START)
        self.assertEqual(
            self.lines(), [f"step-error {ID} step=start error=LinearError: linear api error: down"]
        )
        ok = Gql()
        writeback.progress(replace(ctx, tracker=tracker(ok)), START)
        self.assertEqual(ok.calls, [post("start", "Build started: x")])

    def test_a_tracker_file_or_issue_error_fails_the_step(self):
        for error, text in (
            (
                FILE_ERROR,
                "TrackerFileError: /board/engineer/running/ENG-7.md: changed while writing",
            ),
            (IssueError("ENG-7: archived"), "IssueError: ENG-7: archived"),
        ):
            with self.subTest(text=text):
                ctx = replace(self.ctx(), tracker=Refusing(error))
                self.assertFalse(writeback.progress(ctx, START))
                self.assertEqual(self.lines()[-1], f"step-error {ID} step=start error={text}")
                self.no_ledger(ctx)

    def test_signal_exit_passes_through(self):
        gql = Gql(fail=lambda _name, _v: Stopped(129))
        ctx = self.ctx(gql=gql)
        with self.assertRaises(Stopped) as cm:
            writeback.progress(ctx, START)
        self.assertEqual(cm.exception.code, 129)
        self.no_ledger(ctx)
        self.assertEqual(self.lines(), [])

    def test_never_raises(self):
        gql = Gql(fail=lambda _name, _v: RuntimeError("boom"))
        open(os.path.join(self.tmp, "file"), "w").close()
        with logs_at(os.path.join(self.tmp, "file", "logs")):
            writeback.progress(self.ctx(gql=gql), START)
        writeback.progress(replace(self.ctx(), role="nope"), START)
        self.assertEqual(self.lines(), [f"step-error {ID} step=sink error=KeyError: 'nope'"])
        (record,) = logged()
        self.assertIn("in progress", record["traceback"])
        self.assertIn("KeyError: 'nope'", record["traceback"])


class CompletionFailures(Base):
    def test_a_tracker_file_error_reading_links_fails_the_finish(self):
        ctx = replace(self.ctx(), tracker=Refusing(FILE_ERROR))
        self.assertFalse(
            writeback.finish(
                ctx, Outcome(status="done", title="ENG-7: x", summary="Ready.", url=PR)
            )
        )
        self.assertEqual(
            self.lines(),
            [
                f"step-error {ID} step=read "
                "error=TrackerFileError: /board/engineer/running/ENG-7.md: changed while writing"
            ],
        )

    def test_unknown_role_logged(self):
        self.assertFalse(
            writeback.finish(
                self.ctx("nope"), Outcome(status="done", summary="x", title="Queue options")
            )
        )
        self.assertEqual(self.lines(), [f"step-error {ID} step=finish error=KeyError: 'nope'"])
        (record,) = logged()
        self.assertIn("in finish", record["traceback"])
        self.assertIn("KeyError: 'nope'", record["traceback"])

    def test_signal_exit_stops_the_steps(self):
        for name in ("read", "subscribe", "comment", "attach"):
            with self.subTest(name=name):
                gql = Gql(fail=lambda n, _v, *, name=name: Stopped(143) if n == name else None)
                ctx = self.ctx(gql=gql)
                self.assertFalse(
                    writeback.finish(
                        ctx, Outcome(status="done", title="ENG-7: x", summary="Ready.", url=PR)
                    )
                )
                self.assertEqual(gql.calls[-1][0], name)
                self.assertEqual(
                    self.lines()[-1], f"step-error {ID} step=finish error=Stopped: 143"
                )
                self.assertNotIn("state", [c[0] for c in gql.calls])


class IssueBounce(Base):
    DONE = (STATES["done"], STATES["in_progress"])

    def src(self, team=board_ids.TEAM):
        return {"id": SRC_UUID, "team": {"id": team}}

    def text(self, reason, tail=""):
        return (
            f"Repo check failed: {reason}. To build it, move PRD-3 to Handoff again with a comment "
            f"`Repo: <owner>/<name>` naming the target repo.{tail}"
        )

    def question(self, reason):
        return (
            "Question: repo check failed: "
            + reason
            + ". Fix the description's `Repo:` line, then move this issue back to Todo."
        )

    def test_handoff_path(self):
        gql = Gql(issue=self.src(), states=self.DONE)
        reason = "acme/x: no push permission"
        writeback.bounce(self.ctx(gql=gql), issue(HANDOFF), Invalid(reason))
        t = self.text(reason)
        self.assertEqual(
            gql.calls,
            [
                ("id", {"i": "PRD-3"}),
                ("unarchive", {"i": SRC_UUID}),
                sub(HUMANS[0], SRC_UUID),
                sub(HUMANS[1], SRC_UUID),
                comment(t, SRC_UUID),
                reread(SRC_UUID),
                move("in_review", SRC_UUID),
                comment(t),
                reread(),
                move("canceled"),
            ],
        )
        self.assertEqual(self.lines(), [])

    def test_skipped_moves_logged(self):
        gql = Gql(issue=self.src(), states=(STATES["handoff"], STATES["todo"]))
        writeback.bounce(self.ctx(gql=gql), issue(HANDOFF), Invalid("r"))
        t = self.text("r")
        self.assertEqual(
            gql.calls[-4:], [comment(t, SRC_UUID), reread(SRC_UUID), comment(t), reread()]
        )
        self.assertEqual(
            self.lines(),
            [
                f"skip PRD-3 reason=move to in_review: issue is {STATES['handoff']}",
                f"skip {ID} reason=move to canceled: issue is {STATES['todo']}",
            ],
        )

    def test_mapped_reason_names_project_repos(self):
        gql = Gql(issue=self.src())
        reason = "project mapping acme/x: not found or no access (HTTP 404)"
        writeback.bounce(self.ctx(gql=gql), issue(HANDOFF), Invalid(reason, mapped=True))
        self.assertIn(
            comment(
                self.text(
                    reason,
                    " Or fix ~/.ensemble/config.local.toml's [orchestrator.linear.project_repos] entry.",
                )
            ),
            gql.calls,
        )

    def test_unarchive_failure_ignored(self):
        for err in (LinearError("linear api error: not archived"), False):
            with self.subTest(err=err):
                gql = Gql(
                    issue=self.src(),
                    states=self.DONE,
                    fail=lambda name, _v, *, err=err: err if name == "unarchive" else None,
                )
                writeback.bounce(self.ctx(gql=gql), issue(HANDOFF), Invalid("r"))
                self.assertEqual(gql.calls[-1], move("canceled"))

    def test_subscribe_failure_noted_move_made(self):
        gql = Gql(
            issue=self.src(),
            states=self.DONE,
            fail=lambda name, v: (
                LinearError("linear api error: boom")
                if name == "subscribe" and v["e"] == HUMANS[1]
                else None
            ),
        )
        writeback.bounce(self.ctx(gql=gql), issue(HANDOFF), Invalid("r"))
        t = self.text("r")
        self.assertIn(
            comment(
                t + "\n\nCould not subscribe bob@example.com: LinearError: linear api error: boom",
                SRC_UUID,
            ),
            gql.calls,
        )
        self.assertEqual(
            gql.calls[-5:],
            [reread(SRC_UUID), move("in_review", SRC_UUID), comment(t), reread(), move("canceled")],
        )

    def test_question_path(self):
        reason = "unreadable Repo line: 'nope'"
        for name, description, src, lookup in (
            ("not a handoff", "Repo: nope\n\nBuild it.", None, []),
            (
                "source in another team",
                HANDOFF,
                self.src(team="00000000-0000-4000-8000-0000000000ff"),
                [("id", {"i": "PRD-3"})],
            ),
            ("source missing", HANDOFF, None, [("id", {"i": "PRD-3"})]),
        ):
            with self.subTest(name):
                gql = Gql(issue=src)
                writeback.bounce(self.ctx(gql=gql), issue(description), Invalid(reason))
                self.assertEqual(
                    gql.calls,
                    [
                        *lookup,
                        sub(HUMANS[0]),
                        sub(HUMANS[1]),
                        comment(self.question(reason)),
                        reread(),
                        move("in_review"),
                    ],
                )

    def test_a_ref_bounce_names_the_ref_line(self):
        reason = "Ref: 'gone': no such branch or tag in acme/x"
        gql = Gql(issue=None)
        writeback.bounce(self.ctx(gql=gql), issue("Ref: gone"), Invalid(reason, line="Ref"))
        self.assertEqual(
            gql.calls,
            [
                sub(HUMANS[0]),
                sub(HUMANS[1]),
                comment(
                    f"Question: repo check failed: {reason}. Fix the description's `Ref:` line, "
                    "then move this issue back to Todo."
                ),
                reread(),
                move("in_review"),
            ],
        )
        gql = Gql(issue=self.src(), states=self.DONE)
        writeback.bounce(self.ctx(gql=gql), issue(HANDOFF), Invalid(reason, line="Ref"))
        self.assertIn(
            comment(
                f"Repo check failed: {reason}. To build it, move PRD-3 to Handoff again with a "
                "comment `Ref: <branch or tag>` naming the ref."
            ),
            gql.calls,
        )

    def test_question_subscribe_failure_noted(self):
        gql = Gql(
            fail=lambda name, v: False if name == "subscribe" and v["e"] == HUMANS[0] else None
        )
        writeback.bounce(self.ctx(gql=gql), issue("x"), Invalid("r"))
        self.assertEqual(
            gql.calls[-3:],
            [
                comment(
                    self.question("r")
                    + "\n\nCould not subscribe ann@example.com: LinearError: issueSubscribe: success: false"
                ),
                reread(),
                move("in_review"),
            ],
        )

    def test_signal_exit_raises(self):
        for name in ("unarchive", "subscribe"):
            with self.subTest(name=name):
                gql = Gql(
                    issue=self.src(),
                    fail=lambda n, _v, *, name=name: Stopped(129) if n == name else None,
                )
                with self.assertRaises(Stopped):
                    writeback.bounce(self.ctx(gql=gql), issue(HANDOFF), Invalid("r"))
                self.assertEqual(gql.calls[-1][0], name)

    def test_other_failures_raise(self):
        gql = Gql(issue=self.src(), fail=lambda name, _v: False if name == "comment" else None)
        with self.assertRaises(LinearError):
            writeback.bounce(self.ctx(gql=gql), issue(HANDOFF), Invalid("r"))
        self.assertEqual(gql.calls[-1], comment(self.text("r"), SRC_UUID))
        gql = Gql(
            fail=lambda name, _v: LinearError("linear api error: x") if name == "state" else None
        )
        with self.assertRaises(LinearError):
            writeback.bounce(self.ctx(gql=gql), issue("x"), Invalid("r"))


WRITERS = Writers(tracker(Gql()), LinearTracker(Gql()), STATES, HUMANS, {PROJECT: "acme/ensemble"})


class WritebackContext(run_fixtures.Base):
    def test_context_resolves_build_host_fallback_and_explicit_override(self):
        cfg = config.load()
        cfg = replace(cfg, docs=replace(cfg.docs, host="ghe.example.com"))
        for explicit, expected in (
            (None, "ghe.example.com"),
            ("github.com", "github.com"),
            ("build.example.com", "build.example.com"),
        ):
            with self.subTest(explicit=explicit):
                role = cfg.roles["engineer"]
                output = {"type": "pull-request"} | ({"host": explicit} if explicit else {})
                current = replace(
                    cfg,
                    roles=dict(cfg.roles)
                    | {"engineer": replace(role, run=replace(role.run, output=output))},
                )
                ctx = writeback.context(
                    current,
                    "engineer",
                    WRITERS,
                    ident=ID,
                    issue_id=UUID,
                    sid=SID,
                    resume=False,
                    project=PROJECT,
                    target=TARGET,
                )
                self.assertEqual(ctx.host, expected)
                self.assertEqual(ctx.published, "")
                self.assertEqual(
                    writeback._url(ctx, f"https://{expected}/acme/ensemble/pull/1"),
                    f"https://{expected}/acme/ensemble/pull/1",
                )

    def test_context_names_a_github_outputs_blob_prefix(self):
        cfg = config.load()
        role = cfg.roles["engineer"]
        output = {"type": "github", "repo": "windroc/docs", "branch": "main", "dir": "V/"}
        cfg = replace(
            cfg,
            roles=dict(cfg.roles)
            | {"engineer": replace(role, run=replace(role.run, output=output))},
        )
        ctx = writeback.context(
            cfg,
            "engineer",
            WRITERS,
            ident=ID,
            issue_id=UUID,
            sid=SID,
            resume=False,
            project=PROJECT,
            target=TARGET,
        )
        self.assertEqual(ctx.published, "https://github.com/windroc/docs/blob/main/")

    def test_local_document_reference_is_constructed_for_launch_and_recovery(self):
        cfg = config.load()
        directory = os.path.join(cfg.work_dir, "docs", "researcher")
        cfg = replace(cfg, docs=replace(cfg.docs, dirs={"researcher": directory}))
        for resume in (False, True):
            with self.subTest(resume=resume):
                ctx = writeback.context(
                    cfg,
                    "researcher",
                    WRITERS,
                    ident=ID,
                    issue_id=UUID,
                    sid=SID,
                    resume=resume,
                    project=PROJECT,
                    target=None,
                )
                body, _, url = writeback._completion_body(
                    ctx, Outcome(status="done", title="Report", summary="Ready.")
                )
                expected = Path(directory, f"document-{ID}-{SID}.md").as_uri()
                self.assertEqual(url, expected)
                self.assertIn(expected, body)

    def test_the_one_context_of_a_role_from_its_writers(self):
        cfg = config.load()
        for resume, target in ((False, TARGET), (True, None)):
            with self.subTest(resume=resume):
                ctx = writeback.context(
                    cfg,
                    "engineer",
                    WRITERS,
                    ident=ID,
                    issue_id=UUID,
                    sid=SID,
                    resume=resume,
                    project=PROJECT,
                    target=target,
                )
                want = writeback.Context(
                    ident=ID,
                    issue_id=UUID,
                    role="engineer",
                    sid=SID,
                    resume=resume,
                    project=PROJECT,
                    workdir=os.path.join(self.work, "work", ID),
                    tracker=WRITERS.tracker,
                    humans=HUMANS,
                    states=STATES,
                    repos={PROJECT: "acme/ensemble"},
                    target=target,
                    harness=WRITERS.harness,
                )
                self.assertEqual(ctx, want)

    def test_a_run_writes_with_the_writers_it_is_given(self):
        cfg = config.load()
        spec = SimpleNamespace(issue=ID, uuid=UUID, sid=SID, mode="resume", project=PROJECT)
        ctx = writeback.for_run(cfg, "engineer", spec, WRITERS)
        self.assertIs(ctx.tracker, WRITERS.tracker)
        self.assertIs(ctx.harness, WRITERS.harness)
        self.assertEqual(
            (ctx.ident, ctx.issue_id, ctx.sid, ctx.resume, ctx.project, ctx.target),
            (ID, UUID, SID, True, PROJECT, None),
        )


class DurableRunNotes(Case):
    def test_one_durable_json_line_per_note(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        path = os.path.join(tmp, "logs", "runs.jsonl")
        at = "2026-10-11T18:00:00+08:00"
        with mock.patch.object(os, "fsync", wraps=os.fsync) as spy:
            runs.note(path, "limit", issue=ID, sid=SID, client="claude", resets_at=None)
            runs.note(path, "limit", issue=ID, sid=OTHER_SID, client="claude", resets_at=at)
        self.assertEqual(spy.call_count, 4)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertTrue(text.endswith("\n"))
        rows = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(
            [list(r) for r in rows], [["ts", "kind", "issue", "sid", "client", "resets_at"]] * 2
        )
        for r in rows:
            ts = datetime.fromisoformat(r["ts"])
            self.assertIsNotNone(ts.utcoffset())
            self.assertEqual(r["ts"], ts.isoformat(timespec="seconds"))
        self.assertEqual(
            [{k: v for k, v in r.items() if k != "ts"} for r in rows],
            [
                {"kind": "limit", "issue": ID, "sid": SID, "client": "claude", "resets_at": None},
                {
                    "kind": "limit",
                    "issue": ID,
                    "sid": OTHER_SID,
                    "client": "claude",
                    "resets_at": at,
                },
            ],
        )


class CompletionSteps(Base):
    def test_attach_skipped_when_already_attached(self):
        gql = Gql(attachments=[PR])
        ctx = self.ctx(gql=gql)
        self.assertTrue(
            writeback.finish(
                ctx, Outcome(status="done", title="ENG-7: x", summary="Ready.", url=PR)
            )
        )
        self.assertEqual(
            gql.calls,
            expect(
                body="Build ready:\nAgent report before delivery:\n\n> Ready.", state="in_review"
            ),
        )
        self.assertIn(f"attach:{PR}", self.steps(ctx)[SID])

    def test_a_finish_without_its_move_neither_moves_nor_notifies(self):
        gql = Gql(attachments=[PR])
        ctx = self.ctx(gql=gql)
        done = Outcome(status="done", title="ENG-7: x", summary="Ready.", url=PR)
        self.assertTrue(writeback.finish(ctx, done, move=False))
        body = "Build ready:\nAgent report before delivery:\n\n> Ready."
        self.assertEqual(gql.calls, expect(body=body, state="in_review", subscribe=False)[:-2])
        self.assertEqual(self.steps(ctx)[SID], ["comment", f"attach:{PR}", "outcome"])

    def test_already_linked_errors_count_as_attached(self):
        for text in (
            "url has already been linked to issue",
            "Duplicate attachment",
            "Unable to create issue attachment",
        ):
            with self.subTest(text=text):
                err = LinearError(f"linear api error: [{{'message': '{text}'}}]")
                gql = Gql(fail=lambda name, _v, *, err=err: err if name == "attach" else None)
                ctx = self.ctx(gql=gql)
                self.assertTrue(
                    writeback.finish(
                        ctx, Outcome(status="done", title="ENG-7: x", summary="Ready.", url=PR)
                    )
                )
                self.assertEqual(gql.calls[-1], move("in_review"))
                self.assertIn(f"attach:{PR}", self.steps(ctx)[SID])

    def test_other_attach_error_stops(self):
        gql = Gql(
            fail=lambda name, _v: (
                LinearError("linear api error: boom") if name == "attach" else None
            )
        )
        ctx = self.ctx(gql=gql)
        self.assertFalse(
            writeback.finish(
                ctx, Outcome(status="done", title="ENG-7: x", summary="Ready.", url=PR)
            )
        )
        self.assertEqual(gql.calls[-1][0], "attach")
        self.assertEqual(
            self.lines()[-1],
            f"step-error {ID} step=attach:{PR} error=LinearError: linear api error: boom",
        )

    def test_no_move_from_another_state(self):
        for state, skip in (
            ("in_review", None),
            ("handoff", f"skip {ID} reason=move to in_review: issue is {STATES['handoff']}"),
        ):
            with self.subTest(state=state):
                gql = Gql(states=[STATES[state]])
                ctx = self.ctx(gql=gql)
                self.assertTrue(
                    writeback.finish(
                        ctx,
                        Outcome(status="failed", title="ENG-7: x", summary="push not permitted"),
                    )
                )
                self.assertEqual(gql.calls[-1], reread())
                self.assertEqual(
                    [line for line in self.lines() if line.startswith("skip")],
                    [skip] if skip else [],
                )
                self.assertEqual(
                    self.lines()[-2:], [f"step {ID} step=move:in_review", f"step {ID} step=outcome"]
                )
                self.assertEqual(self.steps(ctx)[SID][-2:], ["move:in_review", "outcome"])

    def test_subscribe_failures_noted_and_finish_continues(self):
        def fail(name, v):
            if name == "subscribe":
                return False if v["e"] == HUMANS[0] else LinearError("linear api error: boom")

        gql = Gql(fail=fail)
        ctx = self.ctx(gql=gql)
        self.assertTrue(
            writeback.finish(
                ctx, Outcome(status="failed", title="ENG-7: x", summary="push not permitted")
            )
        )
        body = (
            "Build failed: push not permitted"
            "\n\nCould not subscribe ann@example.com: LinearError: issueSubscribe: success: false"
            "\n\nCould not subscribe bob@example.com: LinearError: linear api error: boom"
        )
        self.assertEqual(gql.calls, expect(body=body, state="in_review"))

    def test_rejected_spec_post_noted(self):
        err = LinearError("linear api error: body too long")
        gql = Gql(
            fail=lambda name, v: (
                err if name == "comment" and v["b"].startswith("**Spec**") else None
            )
        )
        ctx = self.ctx(gql=gql)
        spec, plan = self.files(ctx)
        self.assertTrue(
            writeback.finish(
                ctx,
                Outcome(
                    status="done",
                    title="ENG-7: x",
                    summary="Ready.",
                    url=PR,
                    files=list([spec, plan]),
                ),
            )
        )
        body = "Build ready:\nAgent report before delivery:\n\n> Ready.\n\nCould not post the Spec `ENG-7-spec.md`: LinearError: linear api error: body too long"
        self.assertEqual(
            gql.calls,
            expect(
                files=[spec_comment(), plan_comment()],
                body=body,
                state="in_review",
                attach=PR,
                attach_title="ENG-7: x",
            ),
        )
        self.assertIn(
            (
                f"step-error {ID} step=file:ENG-7-spec.md:{sha(SPEC_TEXT)} error=LinearError: linear api error: body too long"
            ),
            self.lines(),
        )


class OutcomeFiles(Base):
    def finish_with(self, ctx, path):
        gql = Gql()
        writeback.finish(
            replace(ctx, tracker=tracker(gql)),
            Outcome(
                status="failed", title="ENG-7: x", summary="push not permitted", files=list([path])
            ),
        )
        posted = [c for c in gql.calls if c[0] == "comment"]
        return gql, posted[-1][1]["b"]

    def outside(self, ctx):
        outside = os.path.join(self.tmp, "outside.md")
        with open(outside, "w") as f:
            f.write("secret")
        return outside

    def linked_dir(self, ctx):
        os.makedirs(os.path.join(self.tmp, "away"))
        with open(os.path.join(self.tmp, "away", "x.md"), "w") as f:
            f.write("secret")
        os.symlink(os.path.join(self.tmp, "away"), os.path.join(ctx.workdir, "away"))
        return os.path.join(ctx.workdir, "away", "x.md")

    def fifo(self, ctx):
        os.mkfifo(os.path.join(ctx.workdir, "pipe.md"))
        return os.path.join(ctx.workdir, "pipe.md")

    def test_skipped(self):
        for name, make, reason in (
            ("not md", lambda ctx: self.write(ctx, "notes.txt", "x"), "ValueError: not a .md file"),
            ("outside the workdir", self.outside, "ValueError: not under the workdir"),
            ("outside through a linked dir", self.linked_dir, "ValueError: not under the workdir"),
            (
                "too big",
                lambda ctx: self.write(ctx, "big.md", "x" * 1_000_001),
                "ValueError: over 1000000 bytes",
            ),
            ("fifo, without blocking", self.fifo, "ValueError: not a regular file"),
        ):
            with self.subTest(name):
                ctx = self.ctx()
                path = make(ctx)
                gql, body = self.finish_with(ctx, path)
                self.assertEqual(
                    [c for c in gql.calls if c[0] == "comment"], [post("comment", body)]
                )
                note = f"Build failed: push not permitted\n\nCould not post the file `{os.path.basename(path)}`: {reason}"
                self.assertEqual(body, note)

    def test_posted(self):
        def invalid_utf8(ctx):
            path = os.path.join(ctx.workdir, "spec.md")
            with open(path, "wb") as f:
                f.write(b"ok \xff")
            return path

        for name, make, data, posted in (
            (
                "at the limit",
                lambda ctx: self.write(ctx, "big.md", "x" * 1_000_000),
                b"x" * 1_000_000,
                f"**Spec** `big.md`\n\n---\n\n{'x' * 1_000_000}",
            ),
            (
                "invalid utf-8 replaced",
                invalid_utf8,
                b"ok \xff",
                "**Spec** `spec.md`\n\n---\n\nok �",
            ),
        ):
            with self.subTest(name):
                ctx = self.ctx()
                path = make(ctx)
                gql, _ = self.finish_with(ctx, path)
                step = f"file:{os.path.basename(path)}:{hashlib.sha256(data).hexdigest()[:12]}"
                self.assertEqual(gql.calls[1], post(step, posted))


class EngineeringUrls(Base):
    def test_local_completion_posts_reference_without_external_attachment(self):
        gql = Gql()
        ctx = self.ctx("researcher", gql=gql)
        path = self.write(ctx, "documents/result.md", "Report")
        ctx = replace(ctx, document=path)
        self.assertTrue(
            writeback.finish(ctx, Outcome(status="done", title="Report", summary="Ready."))
        )
        self.assertEqual(
            gql.calls, expect(body=f"Ready.\n\n{Path(path).as_uri()}", state="in_review")
        )

    def test_incomplete_local_outcomes_do_not_publish_output_reference(self):
        ctx = replace(self.ctx("researcher"), document="/docs/not-complete.md")
        for status in ("failed", "needs_input"):
            with self.subTest(status=status):
                body, _, url = writeback._completion_body(
                    ctx,
                    Outcome(
                        status=status,
                        title="Incomplete",
                        summary="Stopped.",
                        questions=["Which?"],
                        url="/docs/not-complete.md",
                    ),
                )
                self.assertNotIn("not-complete.md", body)
                self.assertEqual(url, "")

    def test_enterprise_completion_accepts_only_target_host(self):
        ctx = replace(self.ctx(), host="ghe.example.com")
        url = PR.replace("github.com", "ghe.example.com")
        self.assertEqual(writeback._url(ctx, url), url)
        self.assertEqual(writeback._url(ctx, PR), "")
        self.assertEqual(
            writeback._url(ctx, url.replace("ghe.example.com/", "ghe.example.com.evil/")), ""
        )

    def test_an_engineering_url_must_be_on_the_target(self):
        evil = "https://github.com/acme/ensemble-evil/pull/1"
        for name, target, o, calls, skip in (
            (
                "other repo dropped",
                TARGET,
                Outcome(status="done", title="ENG-7: x", summary="Ready.", url=evil),
                expect(
                    body="Build ready:\nAgent report before delivery:\n\n> Ready.",
                    state="in_review",
                ),
                f"skip {ID} reason=url not on acme/ensemble",
            ),
            (
                "case-insensitive match",
                ("Acme", "Ensemble"),
                Outcome(status="done", title="ENG-7: x", summary="Ready.", url=PR),
                expect(
                    body="Build ready:\nAgent report before delivery:\n\n> Ready.",
                    state="in_review",
                    attach=PR,
                    attach_title="ENG-7: x",
                ),
                None,
            ),
            (
                "no target drops every url",
                None,
                Outcome(status="failed", title="ENG-7: x", summary="push not permitted", url=TREE),
                expect(body="Build failed: push not permitted", state="in_review"),
                f"skip {ID} reason=url not on a target repo",
            ),
        ):
            with self.subTest(name):
                gql = Gql()
                before = len(logged())
                writeback.finish(replace(self.ctx(gql=gql), target=target), o)
                self.assertEqual(gql.calls, calls)
                self.assertEqual(
                    [line for line in self.lines()[before:] if line.startswith("skip ")],
                    [skip] if skip else [],
                )

    def test_a_build_url_on_its_own_github_output_is_kept(self):
        report = "https://github.com/windroc/docs/blob/main/Verification/x-ENG-7-check.md"
        ctx = replace(self.ctx(), published="https://github.com/windroc/docs/blob/main/")
        for target in (TARGET, None):
            with self.subTest(target=target):
                self.assertEqual(writeback._url(replace(ctx, target=target), report), report)
        for url in (
            report.replace("/blob/main/", "/blob/dev/"),
            report.replace("windroc/docs/", "windroc/docs-evil/"),
        ):
            with self.subTest(url=url):
                self.assertEqual(writeback._url(ctx, url), "")


class ApprovalMessages(Base):
    def test_say(self):
        self.assertEqual(writeback.say("", "just text"), "just text")
        self.assertEqual(writeback.say("Build started:", ""), "Build started")
        self.assertEqual(
            writeback.say("Build started:", "first build of the PRD"),
            "Build started: first build of the PRD",
        )
        self.assertEqual(writeback.say("Build ready:", "a\nb"), "Build ready:\na\nb")

    def test_local_completion_messages_use_local_states(self):
        from ensemble.orchestrator.trackers.local.local_tracker import LocalTracker

        local = LocalTracker("unused", (), prefix="LOC")
        ctx = replace(self.ctx(), tracker=local)
        body, _, _ = writeback._completion_body(
            ctx, Outcome("needs_input", "Question", "", questions=["Which repo?"])
        )
        self.assertIn("move this issue back to ready/.", body)
        body, _, _ = writeback._completion_body(
            replace(ctx, role="pm"), Outcome("done", "Design", "Ready")
        )
        self.assertIn("move this issue to approved/", body)

    def test_approve_line(self):
        self.assertEqual(writeback.approve_line(None, "Handoff"), APPROVE)
        self.assertEqual(writeback.approve_line("acme/ensemble", "Handoff"), APPROVE_MAPPED)

    def test_queries_verbatim(self):
        self.assertEqual(
            linear_tracker.M_ASSIGN,
            (
                "mutation($i: String!, $a: String!) { issueUpdate(id: $i, input: { assigneeId: $a }) { success } }"
            ),
        )
        self.assertEqual(
            linear_tracker.M_TITLE,
            (
                "mutation($i: String!, $t: String!) { issueUpdate(id: $i, input: { title: $t }) { success } }"
            ),
        )
        self.assertEqual(
            linear_tracker.M_ATTACH,
            (
                "mutation($i: String!, $u: String!, $t: String) { attachmentLinkURL(issueId: $i, url: $u, title: $t) { success } }"
            ),
        )
        self.assertEqual(
            linear_tracker.M_UNARCHIVE,
            "mutation($i: String!) { issueUnarchive(id: $i) { success } }",
        )
        self.assertEqual(
            linear_tracker.Q_ATTACHED,
            "query($i: String!) { issue(id: $i) { attachments(first: 50) { nodes { url } } } }",
        )
        self.assertEqual(
            linear_tracker.Q_ID, "query($i: String!) { issue(id: $i) { id team { id } } }"
        )


class CompletionMessages(Base):
    def test_build_summary_is_an_earlier_report_not_the_current_pr_state(self):
        outcome = Outcome("done", "Fix", "PR #9 is a draft.\n\nChecks pending.", url=PR)
        body, state, url = writeback._completion_body(self.ctx(), outcome)
        self.assertEqual(
            body,
            "Build ready:\nAgent report before delivery:\n\n"
            "> PR #9 is a draft.\n> \n> Checks pending.",
        )
        self.assertEqual((state, url), ("in_review", PR))
        body, _, _ = writeback._completion_body(self.ctx(), replace(outcome, summary=""))
        self.assertEqual(body, "Build ready")

    def outcomes(self, role, spec, plan):
        kind, files = config.TASKS[role].input, [plan, spec]
        if kind == "research":
            return {
                "done": Outcome(
                    status="done",
                    summary="Three queues compared.\nRedis streams fit best.",
                    url=DOC,
                    files=list(files),
                    title="Queue options",
                ),
                "needs_input": Outcome(
                    status="needs_input",
                    questions=list(["Which repo?", "Which version?"]),
                    url=DOC,
                    files=list(files),
                    title="Queue options",
                    summary="",
                ),
                "failed": Outcome(
                    status="failed",
                    summary="gh api failed",
                    url=DOC,
                    files=list(files),
                    title="Queue options",
                ),
            }
        if kind == "design":
            return {
                "done": Outcome(
                    status="done",
                    title="Session Registry",
                    summary="PRD for the registry.",
                    url=PRD,
                    files=list(files),
                ),
                "needs_input": Outcome(
                    status="needs_input",
                    title="Session Registry",
                    questions=list(["Who uses it?", "Web or CLI?"]),
                    files=list(files),
                    summary="",
                ),
                "failed": Outcome(
                    status="failed",
                    title="Session Registry",
                    summary="Could not publish.",
                    files=list(files),
                ),
            }
        return {
            "done": Outcome(
                status="done",
                title="ENG-7: Session registry",
                summary="Adds the registry.\nVerify: python3 -m unittest",
                url=PR,
                files=list(files),
            ),
            "needs_input": Outcome(
                status="needs_input",
                title="ENG-7: Session registry",
                questions=list(["Keep the old API?"]),
                files=list(files),
                summary="",
            ),
            "failed": Outcome(
                status="failed",
                title="ENG-7: Session registry",
                summary="push not permitted",
                url=TREE,
                files=list(files),
            ),
        }

    def golden(self, role, status, resume):
        research = (
            "1. Which repo?\n2. Which version?\n\n"
            + FOOTER
            + " To change the target repo, edit the description's `Repo:` line."
        )
        files = [spec_comment(), plan_comment()] if config.TASKS[role].files else []
        table = {
            ("research", "done"): expect(
                resume=resume,
                body=f"Three queues compared.\nRedis streams fit best.\n\n{DOC}",
                state="in_review",
                attach=DOC,
                attach_title="Queue options",
            ),
            ("research", "needs_input"): expect(resume=resume, body=research, state="in_review"),
            ("research", "failed"): (
                expect(resume=resume, body=f"gh api failed\n\n{DOC}", state="in_review")
                if resume
                else expect(
                    resume=resume, body=f"gh api failed\n\n{DOC}", state="todo", subscribe=False
                )
            ),
            ("design", "done"): expect(
                resume=resume,
                title="PRD: Session Registry",
                body=f"PRD for the registry.\n\n{PRD}\n\n{APPROVE}",
                state="in_review",
                attach=PRD,
                attach_title="Session Registry",
            ),
            ("design", "needs_input"): expect(
                resume=resume,
                body=f"1. Who uses it?\n2. Web or CLI?\n\n{FOOTER}",
                state="in_review",
            ),
            ("design", "failed"): expect(
                resume=resume, body="Could not publish.", state="in_review"
            ),
            ("build", "done"): expect(
                resume=resume,
                files=files,
                state="in_review",
                body="Build ready:\nAgent report before delivery:\n\n> Adds the registry.\n> Verify: python3 -m unittest",
                attach=PR,
                attach_title="ENG-7: Session registry",
            ),
            ("build", "needs_input"): expect(
                resume=resume,
                body=f"Question:\n1. Keep the old API?\n\n{FOOTER}",
                state="in_review",
            ),
            ("build", "failed"): expect(
                resume=resume,
                files=files,
                state="in_review",
                body=f"Build failed: push not permitted\n\n{TREE}",
            ),
        }
        return table[(config.TASKS[role].input, status)]

    def test_every_role_status_and_mode(self):
        self.assertEqual(set(config.TASKS), {"researcher", "pm", "engineer"})
        for role in config.TASKS:
            for status in OutcomeStatus:
                for resume in (False, True):
                    with self.subTest(role=role, status=status, resume=resume):
                        gql = Gql()
                        build = config.TASKS[role].input == "build"
                        ctx = replace(
                            self.ctx(role, gql=gql), resume=resume, target=TARGET if build else None
                        )
                        spec, plan = self.files(ctx)
                        self.assertTrue(
                            writeback.finish(ctx, self.outcomes(role, spec, plan)[status])
                        )
                        self.assertEqual(gql.calls, self.golden(role, status, resume))

    def test_log_and_ledger(self):
        ctx = self.ctx()
        spec, plan = self.files(ctx)
        writeback.finish(ctx, self.outcomes("engineer", spec, plan)["done"])
        steps = [
            f"file:ENG-7-spec.md:{sha(SPEC_TEXT)}",
            f"file:ENG-7-plan.md:{sha(PLAN_TEXT)}",
            "comment",
            f"attach:{PR}",
            "move:in_review",
            "outcome",
        ]
        self.assertEqual(self.lines(), [f"step {ID} step={s}" for s in steps])
        self.assertEqual([e["sid"] for e in logged()], [SID] * len(steps))
        self.assertEqual(self.ledger(ctx), {SID: {"steps": steps}})

    def test_design_retitle_and_mapped_approve_line(self):
        gql = Gql()
        ctx = replace(self.ctx("pm", gql=gql), target=None, project=PROJECT)
        writeback.finish(
            ctx,
            Outcome(
                status="done", title="Session Registry", summary="PRD for the registry.", url=PRD
            ),
        )
        self.assertEqual(
            gql.calls,
            expect(
                title="PRD: Session Registry",
                body=f"PRD for the registry.\n\n{PRD}\n\n{APPROVE_MAPPED}",
                state="in_review",
                attach=PRD,
                attach_title="Session Registry",
            ),
        )
        self.assertEqual(
            self.steps(ctx),
            {SID: ["retitle", "comment", f"attach:{PRD}", "move:in_review", "outcome"]},
        )

    def test_retitle_without_a_title_prefix_keeps_the_bare_title(self):
        config.TASKS.load(
            board_ids.workflow_file(board_ids.PACKAGED_FLOW.replace('title_prefix = "PRD"\n', ""))
        )
        gql = Gql()
        ctx = replace(self.ctx("pm", gql=gql), target=None, project=PROJECT)
        writeback.finish(
            ctx,
            Outcome(
                status="done", title="Session Registry", summary="PRD for the registry.", url=PRD
            ),
        )
        self.assertEqual(
            gql.calls,
            expect(
                title="Session Registry",
                body=f"PRD for the registry.\n\n{PRD}\n\n{APPROVE_MAPPED}",
                state="in_review",
                attach=PRD,
                attach_title="Session Registry",
            ),
        )


USER = "c0000000-0000-4000-8000-000000000041"


class QuestionAssignee(Base):
    """An assign stage's question assigns its issue to the configured user before its comment."""

    QUESTION = Outcome("needs_input", "Question", "", questions=["Which repo?"])
    BODY = f"1. Which repo?\n\n{FOOTER}"

    def ctx(self, role="pm", *, gql=None):
        return super().ctx(role, gql=gql)

    def calls(self, body, *, assign=True):
        calls = expect(body=body, state="in_review")
        return [calls[0], ("assign", {"i": UUID, "a": USER}), *calls[1:]] if assign else calls

    def test_question_assigns_the_issue_to_the_user(self):
        gql = Gql()
        self.assertTrue(writeback.finish(replace(self.ctx(gql=gql), assignee=USER), self.QUESTION))
        self.assertEqual(gql.calls, self.calls(self.BODY))

    def test_no_assignee_no_assignment(self):
        gql = Gql()
        self.assertTrue(writeback.finish(self.ctx(gql=gql), self.QUESTION))
        self.assertEqual(gql.calls, self.calls(self.BODY, assign=False))

    def test_a_stage_without_assign_never_assigns(self):
        gql = Gql()
        ctx = replace(self.ctx("engineer", gql=gql), assignee=USER)
        self.assertTrue(writeback.finish(ctx, self.QUESTION))
        self.assertEqual(
            gql.calls, expect(body=f"Question:\n1. Which repo?\n\n{FOOTER}", state="in_review")
        )

    def test_done_and_failed_never_assign(self):
        for o in (
            Outcome("done", "Registry", "PRD for it.", url=PRD),
            Outcome("failed", "Registry", "Could not publish."),
        ):
            with self.subTest(status=o.status):
                gql = Gql()
                writeback.finish(replace(self.ctx(gql=gql), assignee=USER, resume=True), o)
                self.assertNotIn("assign", [name for name, _ in gql.calls])

    def test_assign_failure_noted_move_made(self):
        gql = Gql(fail=lambda name, _v: False if name == "assign" else None)
        self.assertTrue(writeback.finish(replace(self.ctx(gql=gql), assignee=USER), self.QUESTION))
        self.assertEqual(
            gql.calls,
            self.calls(
                self.BODY + f"\n\nCould not assign {USER}: LinearError: issueUpdate: success: false"
            ),
        )

    def test_signal_exit_raises(self):
        gql = Gql(fail=lambda name, _v: Stopped(129) if name == "assign" else None)
        self.assertFalse(writeback.finish(replace(self.ctx(gql=gql), assignee=USER), self.QUESTION))
        self.assertEqual(gql.calls[-1][0], "assign")

    def test_context_takes_the_writers_assignee(self):
        cfg = config.load()
        ctx = writeback.context(
            cfg,
            "pm",
            replace(WRITERS, assignee=USER),
            ident=ID,
            issue_id=UUID,
            sid=SID,
            resume=False,
            project=PROJECT,
            target=None,
        )
        self.assertEqual(ctx.assignee, USER)

    def test_local_board_never_reassigns(self):
        from ensemble.orchestrator.trackers.local.local_tracker import LocalTracker

        board = tempfile.mkdtemp(dir=self.tmp)
        local = LocalTracker(board, (), prefix="LOC")
        self.assertIsNone(local.assign("LOC-1", USER))
        self.assertEqual(os.listdir(board), [])


class CompletionMarker(Base):
    DONE = Outcome(status="failed", title="ENG-7: x", summary="push not permitted")

    def test_outcome_follows_the_move(self):
        ctx = self.ctx()
        self.assertTrue(writeback.finish(ctx, self.DONE))
        self.assertEqual(self.steps(ctx), {SID: ["comment", "move:in_review", "outcome"]})

    def test_no_outcome_after_a_failed_move_until_a_retry_moves(self):
        broken = Gql(
            fail=lambda name, _v: LinearError("linear api error: down") if name == "state" else None
        )
        ctx = self.ctx(gql=broken)
        self.assertFalse(writeback.finish(ctx, self.DONE))
        self.assertEqual(self.steps(ctx), {SID: ["comment"]})
        again = Gql()
        self.assertTrue(writeback.finish(replace(ctx, tracker=tracker(again)), self.DONE))
        self.assertEqual(again.calls[-2:], [reread(), move("in_review")])
        self.assertEqual(self.steps(ctx), {SID: ["comment", "move:in_review", "outcome"]})

    def test_no_outcome_after_an_earlier_failed_step(self):
        ctx = self.ctx(gql=Gql(fail=lambda name, _v: False if name == "comment" else None))
        self.assertFalse(writeback.finish(ctx, self.DONE))
        self.no_ledger(ctx)


class CompletionLedger(Base):
    def test_second_finish_skips_ledgered_steps(self):
        ctx = self.ctx()
        spec, plan = self.files(ctx)
        done = Outcome(
            status="done", title="ENG-7: x", summary="Ready.", url=PR, files=list([spec, plan])
        )
        self.assertTrue(writeback.finish(ctx, done))
        again = Gql()
        self.assertTrue(writeback.finish(replace(ctx, tracker=tracker(again)), done))
        self.assertEqual(again.calls, [READ, sub(HUMANS[0]), sub(HUMANS[1])])

    def test_stops_at_first_error_and_resumes_there(self):
        broken = Gql(
            fail=lambda name, v: (
                False if name == "comment" and v["b"].startswith("Build ready") else None
            )
        )
        ctx = self.ctx(gql=broken)
        spec, plan = self.files(ctx)
        done = Outcome(
            status="done", title="ENG-7: x", summary="Ready.", url=PR, files=list([spec, plan])
        )
        self.assertFalse(writeback.finish(ctx, done))
        self.assertEqual(
            broken.calls[-1],
            post("comment", "Build ready:\nAgent report before delivery:\n\n> Ready."),
        )
        self.assertEqual(
            self.lines()[-1],
            f"step-error {ID} step=comment error=LinearError: commentCreate: success: false",
        )
        again = Gql()
        self.assertTrue(writeback.finish(replace(ctx, tracker=tracker(again)), done))
        self.assertEqual(
            again.calls,
            expect(
                body="Build ready:\nAgent report before delivery:\n\n> Ready.",
                state="in_review",
                attach=PR,
                attach_title="ENG-7: x",
            ),
        )

    def test_read_error_stops(self):
        gql = Gql(
            fail=lambda name, _v: LinearError("linear api error: down") if name == "read" else None
        )
        ctx = self.ctx(gql=gql)
        self.assertFalse(
            writeback.finish(
                ctx, Outcome(status="done", title="ENG-7: x", summary="Ready.", url=PR)
            )
        )
        self.assertEqual(gql.calls, [READ])
        self.assertEqual(
            self.lines(), [f"step-error {ID} step=read error=LinearError: linear api error: down"]
        )

    def test_other_sid_does_not_skip(self):
        ctx = self.ctx()
        self.write(
            ctx,
            "writeback.json",
            json.dumps({OTHER_SID: {"steps": ["comment", "move:in_review"]}}),
        )
        gql = Gql()
        writeback.finish(
            replace(ctx, tracker=tracker(gql)),
            Outcome(status="failed", title="ENG-7: x", summary="push not permitted"),
        )
        self.assertEqual(
            gql.calls,
            expect(body="Build failed: push not permitted", state="in_review"),
        )
        self.assertEqual(
            self.steps(ctx),
            {
                OTHER_SID: ["comment", "move:in_review"],
                SID: ["comment", "move:in_review", "outcome"],
            },
        )

    def test_unreadable_ledger_is_empty(self):
        for text in (
            "not json",
            json.dumps([1]),
            json.dumps({SID: "comment move:in_review"}),
            json.dumps({SID: [1]}),
            json.dumps({SID: ["comment", "move:in_review"]}),
            json.dumps({SID: {"steps": "comment move:in_review"}}),
            json.dumps({SID: {"steps": [1]}}),
            json.dumps({SID: {"steps": ["comment", "move:in_review"], "done": True}}),
        ):
            with self.subTest(text=text):
                gql = Gql()
                ctx = self.ctx(gql=gql)
                self.write(ctx, "writeback.json", text)
                writeback.finish(
                    ctx, Outcome(status="failed", title="ENG-7: x", summary="push not permitted")
                )
                self.assertEqual(
                    gql.calls,
                    expect(body="Build failed: push not permitted", state="in_review"),
                )

    def test_fifo_ledger_does_not_block(self):
        gql = Gql()
        ctx = self.ctx(gql=gql)
        os.mkfifo(os.path.join(ctx.workdir, "writeback.json"))
        self.assertTrue(
            writeback.finish(
                ctx, Outcome(status="failed", title="ENG-7: x", summary="push not permitted")
            )
        )
        self.assertEqual(self.steps(ctx), {SID: ["comment", "move:in_review", "outcome"]})

    def test_ledger_write_failure_leaves_no_temp(self):
        ctx = self.ctx()
        with mock.patch.object(os, "replace", side_effect=OSError("disk full")):
            writeback.progress(ctx, START)
        self.assertEqual(self.lines(), [f"step-error {ID} step=start error=OSError: disk full"])
        self.assertEqual(ctx.tracker.gql.calls, [post("start", "Build started: x")])
        self.assertEqual(os.listdir(ctx.workdir), [])


class ResumedComments(Base):
    def test_a_new_run_never_queries(self):
        gql = Gql()
        ctx = self.ctx(gql=gql)
        writeback.progress(ctx, START)
        self.assertTrue(
            writeback.finish(
                ctx, Outcome(status="failed", title="ENG-7: x", summary="push not permitted")
            )
        )
        self.assertNotIn("find", [c[0] for c in gql.calls])
        self.assertEqual(
            [c for c in gql.calls if c[0] == "comment"],
            [
                post("start", "Build started: x"),
                post("comment", "Build failed: push not permitted"),
            ],
        )

    def test_a_resume_skips_ledgered_steps_without_a_query(self):
        ctx = self.ctx()
        done = Outcome(status="failed", title="ENG-7: x", summary="push not permitted")
        writeback.progress(ctx, START)
        self.assertTrue(writeback.finish(ctx, done))
        again = Gql()
        resumed = replace(ctx, tracker=tracker(again), resume=True)
        writeback.progress(resumed, START)
        self.assertTrue(writeback.finish(resumed, done))
        self.assertEqual(again.calls, [READ, sub(HUMANS[0]), sub(HUMANS[1])])

    def test_a_resume_finds_a_post_whose_ledger_write_was_lost(self):
        gql = Gql()
        ctx = self.ctx(gql=gql)
        with mock.patch.object(os, "replace", side_effect=OSError("disk full")):
            writeback.progress(ctx, START)
        self.assertEqual(gql.calls, [post("start", "Build started: x")])
        self.assertEqual(self.lines(), [f"step-error {ID} step=start error=OSError: disk full"])
        self.no_ledger(ctx)
        gql.calls.clear()
        writeback.progress(replace(ctx, resume=True), START)
        self.assertEqual(gql.calls, [find("start")])
        self.assertEqual(self.steps(ctx), {SID: ["start"]})
        self.assertEqual(self.lines()[-1], f"step {ID} step=start found=true")

    def test_a_resumed_finish_counts_a_found_comment_as_posted(self):
        body = "Build failed: push not permitted"
        gql = Gql(comments={cid("comment"): mine(body)})
        ctx = replace(self.ctx(gql=gql), resume=True)
        self.assertTrue(
            writeback.finish(
                ctx, Outcome(status="failed", title="ENG-7: x", summary="push not permitted")
            )
        )
        self.assertEqual(
            gql.calls,
            [READ, sub(HUMANS[0]), sub(HUMANS[1]), find("comment"), reread(), move("in_review")],
        )
        self.assertEqual(self.steps(ctx), {SID: ["comment", "move:in_review", "outcome"]})
        self.assertEqual(
            self.lines(),
            [
                f"step {ID} step=comment found=true",
                f"step {ID} step=move:in_review",
                f"step {ID} step=outcome",
            ],
        )

    def test_a_resume_after_a_failed_post_finds_then_posts_once(self):
        broken = Gql(fail=lambda name, _v: False if name == "comment" else None)
        ctx = self.ctx(gql=broken)
        done = Outcome(status="failed", title="ENG-7: x", summary="push not permitted")
        self.assertFalse(writeback.finish(ctx, done))
        again = Gql()
        self.assertTrue(writeback.finish(replace(ctx, tracker=tracker(again), resume=True), done))
        self.assertEqual(
            again.calls,
            expect(body="Build failed: push not permitted", state="in_review", resume=True),
        )
        self.assertEqual(len([c for c in again.calls if c[0] == "comment"]), 1)

    def test_a_find_error_is_the_steps_error(self):
        squatted = f"LinearError: comment {cid('comment')} is not ours"
        for name, comments, fail, error in (
            ("another issue's", {cid("comment"): mine("x", OTHER_ISSUE)}, None, squatted),
            (
                "another author's",
                {cid("comment"): {"body": "x", "issue": UUID, "mine": False}},
                None,
                squatted,
            ),
            (
                "unreadable",
                {},
                LinearError("linear api error: down"),
                "LinearError: linear api error: down",
            ),
        ):
            with self.subTest(name):
                gql = Gql(
                    comments=comments,
                    fail=lambda n, _v, *, fail=fail: fail if n == "find" else None,
                )
                ctx = replace(self.ctx(gql=gql), resume=True)
                self.assertFalse(
                    writeback.finish(
                        ctx,
                        Outcome(status="failed", title="ENG-7: x", summary="push not permitted"),
                    )
                )
                self.assertEqual(gql.calls, [READ, sub(HUMANS[0]), sub(HUMANS[1]), find("comment")])
                self.assertEqual(self.lines()[-1], f"step-error {ID} step=comment error={error}")
                self.no_ledger(ctx)


RUN_ISSUE = "TASK-1"

ROLE_SERVICE = "linear-app-engineer-token"

STARTED, RESTARTED = "2026-10-14T09:00:00+08:00", "2026-10-14T10:00:00+08:00"
BEFORE = datetime.fromisoformat(STARTED) - timedelta(hours=1)  # a row, then its run's Begun

LATER, LATEST = "2026-10-14T11:00:00+08:00", "2026-10-14T12:00:00+08:00"

RESUME_COMMAND = f"cd /run && fake --resume {RUN_SID}"

RUN_SESSION = board_ids.comment_id("harness", RUN_SID, "session")
TURN_FAILED = "the client's turn failed: ContextOverflowError (compacting)"

CLAIM = Claim(
    issue=RUN_ISSUE,
    sid=RUN_SID,
    role="engineer",
    uuid=UUID,
    project="p1",
    target=("acme", "ensemble"),
    client="fake",
    delegate_kind="fake",
)

EVENT_OPS = {
    linear_tracker.M_UPDATE: "update",
    ops.M_COMMENT_ID: "comment",
    ops.Q_COMMENT: "find",
    ops.Q_ISSUE_STATE: "reread",
    ops.M_STATE: "state",
    ops.M_SUBSCRIBE: "subscribe",
    linear_tracker.Q_ATTACHED: "read",
    linear_tracker.M_TITLE: "title",
    linear_tracker.M_ATTACH: "attach",
    linear_tracker.Q_STATE_NAME: "state_name",
}

EVENT_FIELDS = {
    "comment": "commentCreate",
    "update": "commentUpdate",
    "state": "issueUpdate",
    "subscribe": "issueSubscribe",
    "title": "issueUpdate",
    "attach": "attachmentLinkURL",
}


class Linear:
    """Linear for one issue: its comments by client id, its state; each call as (service, op,
    variables).
    """

    def __init__(self, state, fail=lambda _op, _v: None):
        self.calls, self.comments, self.state, self.fail = [], {}, state, fail

    def __call__(self, query, *, identity, timeout=None, **v):
        service = identity.service
        op = EVENT_OPS[query]
        self.calls.append((service, op, v))
        if (error := self.fail(op, v)) is not None:
            raise error
        found = self.comments.get(v.get("c"))
        if op == "update":
            if found is None:
                raise NOT_FOUND
            found["body"] = v["b"]
        elif op == "comment":
            self.comments[v["c"]] = {"body": v["b"], "service": service}
        elif op == "find":
            if found is None:
                raise NOT_FOUND
            mine = {"isMe": found["service"] == service}
            return {"comment": {"body": found["body"], "issue": {"id": UUID}, "user": mine}}
        elif op == "reread":
            return {"issue": {"state": {"id": self.state}}}
        elif op == "state":
            self.state = v["s"]
        elif op == "read":
            return {"issue": {"attachments": {"nodes": []}}}
        elif op == "state_name":
            return {"workflowState": {"name": "Todo"}}
        return {EVENT_FIELDS[op]: {"success": True}}

    def writes(self, op):
        return [(service, v) for service, o, v in self.calls if o == op]


def delegate_event(kind, attempt=1, sid=RUN_SID, **fields):
    return DelegateEvent.parse(
        json.dumps({"v": 1, "kind": kind, "sid": sid, "attempt": attempt, **fields})
    )


class Events(RunCase):
    def setUp(self):
        super().setUp()
        log.use_logs(self.cfg.logs_dir)
        self.workdir = self.cfg.issue_workdir(RUN_ISSUE)
        LinearRun(
            RUN_ISSUE,
            "Echo input.",
            self.workdir,
            cfg=self.cfg,
            role="engineer",
            sid=RUN_SID,
            client="fake",
            delegate_kind="fake",
        ).detach()
        self.claim()
        self.linear = Linear(self.cfg.linear.states["in_progress"])
        info = mock.patch.object(
            ReadyDelegate, "session_status", return_value=SessionStatus("yes", None, RESUME_COMMAND)
        )
        info.start()
        self.addCleanup(info.stop)
        self.now = iter((STARTED, RESTARTED, LATER, LATEST))

    def claim(self, sid=RUN_SID, role="engineer", **fields):
        runs.note(
            self.cfg.runs_log,
            "start",
            now=lambda: BEFORE,
            issue=RUN_ISSUE,
            sid=sid,
            role=role,
            client="fake",
            delegate_kind="fake",
            uuid=UUID,
            project="p1",
            target="acme/ensemble",
            **fields,
        )

    def writers(self):
        return functools.partial(cli.TRACKERS[TrackerKind.LINEAR].claimed, self.linear, self.cfg)

    def deliver(self, e, *, workdir=None, issue=RUN_ISSUE):
        return writeback.deliver(
            self.cfg,
            issue,
            workdir or self.workdir,
            e,
            writers=self.writers(),
            now=lambda: next(self.now),
        )

    def ledger(self):
        with open(os.path.join(self.workdir, writeback.LEDGER)) as f:
            return json.load(f)

    def ledger_reset(self, sid):
        ledger = self.ledger()
        del ledger[sid]
        with open(os.path.join(self.workdir, writeback.LEDGER), "w") as f:
            json.dump(ledger, f)

    def said(self):
        return [f"{e['src']} {show(e)}" for e in logged()]

    def racing(self, module, name, act):
        """On module.name's first call, runs act on another thread and gives it half a second
        before the call goes on; act's results and its thread.
        """
        real, results, threads = getattr(module, name), [], []

        def first(*args, **kwargs):
            if not threads:
                threads.append(threading.Thread(target=lambda: results.append(act())))
                threads[0].start()
                threads[0].join(0.5)
            return real(*args, **kwargs)

        patched = mock.patch.object(module, name, first)
        patched.start()
        self.addCleanup(patched.stop)
        return results, threads

    @staticmethod
    def finished(rc=0, *, outcome=None, error="", limit=None, attempt=1):
        return delegate_event(
            "finished", attempt, returncode=rc, outcome=outcome, error=error, limit=limit
        )


class ForClaim(RunCase):
    def test_writes_as_the_writers_it_is_given(self):
        ctx = writeback.for_claim(self.cfg, CLAIM, WRITERS, attempt=1)
        self.assertEqual((ctx.tracker, ctx.harness), (WRITERS.tracker, WRITERS.harness))
        self.assertTrue(ctx.redelivered)
        self.assertEqual(
            (ctx.ident, ctx.issue_id, ctx.sid, ctx.role, ctx.project, ctx.target),
            (RUN_ISSUE, UUID, RUN_SID, "engineer", "p1", ("acme", "ensemble")),
        )
        self.assertEqual(ctx.workdir, self.cfg.issue_workdir(RUN_ISSUE))

    def test_a_later_attempt_is_a_resume(self):
        for attempt, resume in ((1, False), (2, True), (3, True)):
            with self.subTest(attempt=attempt):
                ctx = writeback.for_claim(self.cfg, CLAIM, WRITERS, attempt=attempt)
                self.assertIs(ctx.resume, resume)


class RunningEvents(Events):
    def test_each_roles_new_or_resumed_attempt_rereads_and_returns_to_in_progress(self):
        self.now = iter([STARTED] * 100)
        attempt = 0
        for role in config.TASKS:
            with open(self.cfg.runs_log, "w"):
                pass
            self.claim(role=role)
            for state in (*self.cfg.linear.states.values(), "other-state"):
                with self.subTest(role=role, state=state):
                    attempt += 1
                    self.linear.state = state
                    n = len(self.linear.calls)
                    self.assertTrue(self.deliver(delegate_event("begun", attempt)))
                    calls = self.linear.calls[n:]
                    self.assertEqual(self.linear.state, self.cfg.linear.states["in_progress"])
                    self.assertIn(
                        (self.cfg.linear.roles[role].token_service, "reread", {"i": UUID}), calls
                    )
                    moved = [c for c in calls if c[1] == "state"]
                    expected = (
                        []
                        if state == self.cfg.linear.states["in_progress"]
                        else [
                            (
                                self.cfg.linear.roles[role].token_service,
                                "state",
                                {"i": UUID, "s": self.cfg.linear.states["in_progress"]},
                            )
                        ]
                    )
                    self.assertEqual(moved, expected)
                    if moved:
                        self.assertLess(
                            calls.index(
                                (self.cfg.linear.roles[role].token_service, "reread", {"i": UUID})
                            ),
                            calls.index(moved[0]),
                        )

    def test_limit_resume_returns_to_work_and_the_next_limit_ends_it_again(self):
        self.assertTrue(self.deliver(delegate_event("begun")))
        self.assertTrue(self.deliver(self.finished(-9, limit={"resets_at": None})))
        self.assertEqual(self.linear.state, self.cfg.linear.states["in_review"])
        self.assertTrue(self.deliver(delegate_event("begun", 2)))
        self.assertEqual(self.linear.state, self.cfg.linear.states["in_progress"])
        self.assertTrue(self.deliver(self.finished(-9, attempt=2, limit={"resets_at": None})))
        self.assertEqual(self.linear.state, self.cfg.linear.states["in_review"])

    def test_failed_state_read_or_write_is_retryable_from_progress(self):
        for op in ("reread", "state"):
            with self.subTest(op=op):
                attempt = 1 if op == "reread" else 2
                self.linear.state = self.cfg.linear.states["in_review"]
                self.linear.fail = lambda name, _v, op=op: (
                    LinearError("down") if name == op else None
                )
                self.assertFalse(self.deliver(delegate_event("begun", attempt)))
                self.assertEqual(self.linear.state, self.cfg.linear.states["in_review"])
                self.linear.fail = lambda _name, _v: None
                self.assertTrue(
                    self.deliver(delegate_event("progress", attempt, name="step", text="fix"))
                )
                self.assertEqual(self.linear.state, self.cfg.linear.states["in_progress"])

    def test_duplicate_or_delayed_events_do_not_reopen_an_ended_attempt(self):
        self.assertTrue(self.deliver(delegate_event("begun")))
        self.assertTrue(self.deliver(self.finished(outcome=outcome_data())))
        self.assertEqual(self.linear.state, self.cfg.linear.states["in_review"])
        n = len(self.linear.writes("state"))
        self.assertTrue(self.deliver(delegate_event("begun")))
        self.assertTrue(self.deliver(delegate_event("progress", name="step", text="late")))
        self.assertEqual(len(self.linear.writes("state")), n)
        self.assertEqual(self.linear.state, self.cfg.linear.states["in_review"])

    def test_a_delayed_older_attempt_cannot_move_the_current_attempt(self):
        self.assertTrue(self.deliver(delegate_event("begun", 2)))
        self.linear.state = self.cfg.linear.states["in_review"]
        n = len(self.linear.writes("state"))
        self.assertTrue(self.deliver(delegate_event("begun")))
        self.assertTrue(self.deliver(delegate_event("progress", name="step", text="old")))
        self.assertEqual(len(self.linear.writes("state")), n)
        self.assertEqual(self.linear.state, self.cfg.linear.states["in_review"])


class BegunEvents(Events):
    def test_posts_the_running_session_comment_as_the_harness_and_records_its_start(self):
        self.assertTrue(self.deliver(delegate_event("begun")))
        body = f"Run {RUN_SID} · running · #1 · {STARTED}\n\n```\n{RESUME_COMMAND}\n```"
        harness = self.cfg.linear.harness.service
        self.assertEqual(
            self.linear.calls,
            [
                (harness, "update", {"c": RUN_SESSION, "b": body}),
                (harness, "comment", {"c": RUN_SESSION, "i": RUN_ISSUE, "b": body}),
                (ROLE_SERVICE, "reread", {"i": UUID}),
            ],
        )
        self.assertEqual(
            self.ledger(),
            {
                RUN_SID: {
                    "steps": ["running:1"],
                    "attempts": {"1": {"started_at": STARTED, "seq": 1}},
                }
            },
        )

    def test_the_same_begun_twice_writes_once(self):
        self.assertTrue(self.deliver(delegate_event("begun")))
        n = len(self.linear.calls)
        self.assertTrue(self.deliver(delegate_event("begun")))
        self.assertEqual(self.linear.calls[n:], [])
        self.assertEqual(
            self.ledger()[RUN_SID]["attempts"], {"1": {"started_at": STARTED, "seq": 1}}
        )

    def test_a_later_attempt_has_its_own_start(self):
        self.assertTrue(self.deliver(delegate_event("begun")))
        self.assertTrue(self.deliver(delegate_event("begun", attempt=2)))
        body = f"Run {RUN_SID} · running · #1 · {RESTARTED}\n\n```\n{RESUME_COMMAND}\n```"
        self.assertEqual(
            self.linear.writes("update")[-1],
            (self.cfg.linear.harness.service, {"c": RUN_SESSION, "b": body}),
        )
        self.assertEqual(
            self.ledger()[RUN_SID]["attempts"],
            {"1": {"started_at": STARTED, "seq": 1}, "2": {"started_at": RESTARTED, "seq": 1}},
        )

    def test_a_failed_write_keeps_the_start_and_a_retry_posts(self):
        self.linear.fail = lambda _op, _v: LinearError("linear api error: down")
        self.assertFalse(self.deliver(delegate_event("begun")))
        self.assertEqual(
            self.ledger()[RUN_SID]["attempts"], {"1": {"started_at": STARTED, "seq": 0}}
        )
        self.assertIn(
            f"writeback registry-error {RUN_ISSUE} sid={RUN_SID} error=LinearError: linear api error: down",
            self.said(),
        )
        self.linear.fail = lambda _op, _v: None
        self.assertTrue(self.deliver(delegate_event("begun")))
        self.assertEqual(
            self.linear.comments[RUN_SESSION]["body"],
            f"Run {RUN_SID} · running · #1 · {STARTED}\n\n```\n{RESUME_COMMAND}\n```",
        )
        self.assertEqual(
            self.ledger()[RUN_SID]["attempts"], {"1": {"started_at": STARTED, "seq": 1}}
        )


class ProgressEvents(Events):
    def test_start_and_marks_post_once_as_the_claims_app(self):
        self.assertTrue(
            self.deliver(delegate_event("progress", name="start", text="light-build: a tweak"))
        )
        self.assertTrue(
            self.deliver(delegate_event("progress", name="start", text="light-build: a tweak"))
        )
        self.assertTrue(self.deliver(delegate_event("progress", name="tests", text="12 passing")))
        self.assertTrue(self.deliver(delegate_event("progress", name="tests", text="12 passing")))
        digest = "progress:tests:" + hashlib.sha256(b"12 passing").hexdigest()[:12]
        self.assertEqual(
            self.linear.calls,
            [
                (ROLE_SERVICE, "reread", {"i": UUID}),
                (ROLE_SERVICE, "find", {"c": board_ids.comment_id("engineer", RUN_SID, "start")}),
                (
                    ROLE_SERVICE,
                    "comment",
                    {
                        "c": board_ids.comment_id("engineer", RUN_SID, "start"),
                        "i": UUID,
                        "b": "Build started: light-build: a tweak",
                    },
                ),
                (ROLE_SERVICE, "find", {"c": board_ids.comment_id("engineer", RUN_SID, digest)}),
                (
                    ROLE_SERVICE,
                    "comment",
                    {
                        "c": board_ids.comment_id("engineer", RUN_SID, digest),
                        "i": UUID,
                        "b": "Progress (tests): 12 passing",
                    },
                ),
            ],
        )
        self.assertEqual(self.ledger(), {RUN_SID: {"steps": ["running:1", "start", digest]}})

    def test_a_redelivered_start_whose_ledger_write_was_lost_is_found_not_reposted(self):
        self.assertTrue(self.deliver(delegate_event("begun")))
        start = delegate_event("progress", name="start", text="x")
        with mock.patch.object(os, "replace", side_effect=OSError("disk full")):
            self.assertFalse(self.deliver(start))
        self.assertTrue(self.deliver(start))
        posts = [v["c"] for _, op, v in self.linear.calls if op == "comment"]
        self.assertEqual(posts, [RUN_SESSION, board_ids.comment_id("engineer", RUN_SID, "start")])
        self.assertEqual(self.ledger()[RUN_SID]["steps"], ["running:1", "start"])

    def test_a_resumed_attempt_finds_a_start_whose_ledger_write_was_lost(self):
        self.linear.comments[board_ids.comment_id("engineer", RUN_SID, "start")] = {
            "body": "x",
            "service": ROLE_SERVICE,
        }
        self.assertTrue(self.deliver(delegate_event("progress", attempt=2, name="start", text="x")))
        self.assertEqual(
            self.linear.calls,
            [
                (ROLE_SERVICE, "reread", {"i": UUID}),
                (ROLE_SERVICE, "find", {"c": board_ids.comment_id("engineer", RUN_SID, "start")}),
            ],
        )

    def test_a_failed_post_is_false(self):
        self.linear.fail = lambda op, _v: (
            LinearError("linear api error: down") if op == "comment" else None
        )
        self.assertFalse(self.deliver(delegate_event("progress", name="start", text="x")))
        self.assertEqual(self.ledger(), {RUN_SID: {"steps": ["running:1"]}})


class RefusedEvents(Events):
    def test_current_namespace_keeps_the_author_check(self):
        cid = board_ids.comment_id("engineer", RUN_SID, "start")
        held = {"body": "someone else's comment", "service": "other-app"}
        self.linear.comments[cid] = held.copy()
        self.assertFalse(self.deliver(delegate_event("progress", 2, name="start", text="x")))
        self.assertEqual(self.linear.comments[cid], held)
        self.assertEqual(self.linear.writes("comment"), [])

    def test_resume_preserves_old_author_comment_after_lost_ledger_write(self):
        old = ops.client_id(RUN_SID, "start")
        held = {"body": "pre-app progress", "service": "pre-app-key"}
        self.linear.comments[old] = held.copy()
        self.assertTrue(self.deliver(delegate_event("progress", 2, name="start", text="new app")))
        self.assertEqual(self.linear.comments[old], held)
        self.assertEqual(len(self.linear.writes("comment")), 1)
        self.assertNotEqual(self.linear.writes("comment")[0][1]["c"], old)
        self.assertTrue(self.deliver(delegate_event("progress", 2, name="start", text="new app")))
        self.assertEqual(len(self.linear.writes("comment")), 1)

    def test_recover_preserves_old_author_completion_after_lost_ledger_write(self):
        old = ops.client_id(RUN_SID, "comment")
        held = {"body": "pre-app completion", "service": "pre-app-key"}
        self.linear.comments[old] = held.copy()
        outcome = Outcome(status="failed", title="x", summary="run failed")
        self.assertTrue(
            writeback.replay(self.cfg, RUN_ISSUE, RUN_SID, outcome, writers=self.writers())
        )
        self.assertEqual(self.linear.comments[old], held)
        posts = self.linear.writes("comment")
        self.assertEqual(posts[0][0], ROLE_SERVICE)
        self.assertNotEqual(posts[0][1]["c"], old)
        self.assertIsNone(
            writeback.replay(self.cfg, RUN_ISSUE, RUN_SID, outcome, writers=self.writers())
        )
        self.assertEqual(self.linear.writes("comment"), posts)

    def test_what_cannot_be_written_raises_before_any_write(self):
        other = "9c1d2e3f-4a5b-4c6d-8e7f-a0b1c2d3e4f5"
        for name, kwargs, e, error in (
            (
                "another workdir",
                {"workdir": self.cfg.issue_workdir("TASK-2")},
                delegate_event("begun"),
                ConfigError,
            ),
            ("a bad issue id", {"issue": "../x"}, delegate_event("begun"), ConfigError),
            ("no start row", {}, delegate_event("begun", sid=other), ValueError),
        ):
            with self.subTest(name), self.assertRaises(error):
                self.deliver(e, **kwargs)
        self.assertEqual(self.linear.calls, [])
        self.assertFalse(os.path.exists(os.path.join(self.workdir, writeback.LEDGER)))

    def test_removed_role_refuses_before_writing(self):
        self.cfg = replace(self.cfg, linear=replace(self.cfg.linear, roles={}))
        with self.assertRaisesRegex(ConfigError, "role 'engineer' has no configured Linear app"):
            self.deliver(delegate_event("begun"))
        self.assertEqual(self.linear.calls, [])

    def test_current_app_replaces_the_old_start_row_credential(self):
        with open(self.cfg.runs_log) as f:
            rows = [json.loads(line) for line in f]
        with open(self.cfg.runs_log, "w") as f:
            for row in rows:
                old = {"key": "pre-app-engineer-key"} if row["kind"] == "start" else {}
                f.write(json.dumps(row | old) + "\n")
        app = replace(self.cfg.linear.roles["engineer"], token_service="new-engineer-token")
        self.cfg = replace(self.cfg, linear=replace(self.cfg.linear, roles={"engineer": app}))
        self.assertTrue(self.deliver(delegate_event("progress", name="start", text="x")))
        self.assertEqual({service for service, _, _ in self.linear.calls}, {"new-engineer-token"})

    def test_a_claimed_role_without_write_back_raises(self):
        other = "9c1d2e3f-4a5b-4c6d-8e7f-a0b1c2d3e4f5"
        runs.note(
            self.cfg.runs_log,
            "start",
            **{**vars(replace(CLAIM, sid=other, role="dummy")), "target": None, "uuid": UUID},
        )
        with self.assertRaisesRegex(ConfigError, "dummy"):
            self.deliver(delegate_event("begun", sid=other))
        self.assertEqual(self.linear.calls, [])


def outcome_data(**fields):
    return {
        "status": "done",
        "title": "TASK-1: x",
        "summary": "Ready.",
        "questions": [],
        "url": PR,
        "files": [],
        "deliverable": "",
    } | fields


def session_body(line):
    return f"Run {RUN_SID} · {line}\n\n```\n{RESUME_COMMAND}\n```"


class FinishedEvents(Events):
    def test_an_outcome_is_written_back_then_the_session_ends(self):
        self.assertTrue(self.deliver(delegate_event("begun")))
        n = len(self.linear.calls)
        self.assertTrue(self.deliver(self.finished(outcome=outcome_data())))
        harness, states = self.cfg.linear.harness.service, self.cfg.linear.states
        ended = session_body(f"done · #2 · {STARTED} → {RESTARTED} · exit 0")
        self.assertEqual(
            self.linear.calls[n:],
            [
                (ROLE_SERVICE, "read", {"i": UUID}),
                (ROLE_SERVICE, "find", {"c": board_ids.comment_id("engineer", RUN_SID, "comment")}),
                (
                    ROLE_SERVICE,
                    "comment",
                    {
                        "c": board_ids.comment_id("engineer", RUN_SID, "comment"),
                        "i": UUID,
                        "b": "Build ready:\nAgent report before delivery:\n\n> Ready.",
                    },
                ),
                (ROLE_SERVICE, "attach", {"i": UUID, "u": PR, "t": "TASK-1: x"}),
                (ROLE_SERVICE, "reread", {"i": UUID}),
                (ROLE_SERVICE, "state", {"i": UUID, "s": states["in_review"]}),
                (harness, "update", {"c": RUN_SESSION, "b": ended}),
                (harness, "find", {"c": RUN_SESSION}),
            ],
        )
        self.assertEqual(
            self.ledger(),
            {
                RUN_SID: {
                    "steps": ["running:1", "comment", f"attach:{PR}", "move:in_review", "outcome"],
                    "attempts": {"1": {"started_at": STARTED, "seq": 2, "ended_at": RESTARTED}},
                }
            },
        )

    def test_the_same_finished_twice_writes_once(self):
        self.assertTrue(self.deliver(delegate_event("begun")))
        self.assertTrue(self.deliver(self.finished(outcome=outcome_data())))
        n = len(self.linear.calls)
        self.assertTrue(self.deliver(self.finished(outcome=outcome_data())))
        self.assertEqual(self.linear.calls[n:], [])
        comments = [v for _, op, v in self.linear.calls if op == "comment" and v.get("i") == UUID]
        self.assertEqual(len(comments), 1)
        self.assertEqual(len(self.linear.writes("state")), 1)

    def test_a_redelivered_outcome_whose_ledger_write_was_lost_completes_without_a_repost(self):
        with mock.patch.object(os, "replace", side_effect=OSError("disk full")):
            self.assertFalse(self.deliver(self.finished(outcome=outcome_data())))
        self.assertEqual(self.linear.writes("state"), [])
        self.assertTrue(self.deliver(self.finished(outcome=outcome_data())))
        posts = [
            v["c"]
            for _, op, v in self.linear.calls
            if op == "comment" and "i" in v and v["i"] == UUID
        ]
        self.assertEqual(posts, [board_ids.comment_id("engineer", RUN_SID, "comment")])
        self.assertEqual(len(self.linear.writes("state")), 1)
        self.assertEqual(self.ledger()[RUN_SID]["steps"][-2:], ["move:in_review", "outcome"])

    def test_a_valid_outcome_is_honored_on_a_nonzero_exit(self):
        self.assertTrue(self.deliver(self.finished(1, outcome=outcome_data())))
        self.assertEqual(self.linear.state, self.cfg.linear.states["in_review"])
        self.assertEqual(self.ledger()[RUN_SID]["steps"][-1], "outcome")
        self.assertEqual(
            self.linear.comments[RUN_SESSION]["body"],
            session_body(f"done · #1 · {STARTED} → {RESTARTED} · exit 1"),
        )

    def test_interruption_ends_the_session_and_leaves_the_issue_for_recover(self):
        self.assertTrue(self.deliver(delegate_event("begun")))
        n = len(self.linear.calls)
        self.assertTrue(self.deliver(self.finished(1, error="stopped: Stopped: 143")))
        ended = session_body(f"interrupted · #2 · {STARTED} → {RESTARTED} · exit 1")
        harness = self.cfg.linear.harness.service
        self.assertEqual(
            self.linear.calls[n:],
            [
                (harness, "update", {"c": RUN_SESSION, "b": ended}),
                (harness, "find", {"c": RUN_SESSION}),
            ],
        )
        self.assertEqual(self.linear.state, self.cfg.linear.states["in_progress"])
        self.assertIn(
            f"writeback no-outcome {RUN_ISSUE} sid={RUN_SID} error=stopped: Stopped: 143",
            self.said(),
        )
        self.assertEqual(self.ledger()[RUN_SID]["steps"], ["running:1"])

    def test_a_clean_exit_without_an_outcome_ends_the_session_as_no_outcome(self):
        self.assertTrue(self.deliver(delegate_event("begun")))
        self.assertTrue(self.deliver(self.finished(0, error="the agent run returned no outcome")))
        self.assertEqual(
            self.linear.comments[RUN_SESSION]["body"],
            session_body(f"no outcome · #2 · {STARTED} → {RESTARTED} · exit 0"),
        )
        self.assertEqual(self.linear.state, self.cfg.linear.states["in_review"])
        self.assertEqual(
            self.linear.comments[board_ids.comment_id("engineer", RUN_SID, "no-outcome:1:comment")][
                "body"
            ],
            "Build failed: the agent run returned no outcome",
        )

    def test_errors_without_outcomes_report_the_reason_and_move_once_per_attempt(self):
        self.now = iter([STARTED, RESTARTED] * 6)
        for attempt, error in enumerate(
            (
                "invalid outcome: status converged",
                "the client exited 1",
                "stopped: ValueError: broken",
            ),
            1,
        ):
            with self.subTest(attempt=attempt):
                self.linear.state = self.cfg.linear.states["in_progress"]
                event = self.finished(1, error=error, attempt=attempt)
                self.assertTrue(self.deliver(event))
                self.assertEqual(self.linear.state, self.cfg.linear.states["in_review"])
                self.assertEqual(
                    self.linear.comments[
                        board_ids.comment_id("engineer", RUN_SID, f"no-outcome:{attempt}:comment")
                    ]["body"],
                    f"Build failed: {error}",
                )
                calls = len(self.linear.calls)
                self.assertTrue(self.deliver(event))
                self.assertEqual(len(self.linear.calls), calls)

    def test_a_failed_turn_without_an_outcome_is_written_back_as_failed(self):
        self.assertTrue(self.deliver(delegate_event("begun")))
        n = len(self.linear.calls)
        self.assertTrue(self.deliver(self.finished(0, error=TURN_FAILED)))
        harness, states = self.cfg.linear.harness.service, self.cfg.linear.states
        ended = session_body(f"no outcome · #2 · {STARTED} → {RESTARTED} · exit 0")
        comment = board_ids.comment_id("engineer", RUN_SID, "no-outcome:1:comment")
        self.assertEqual(
            self.linear.calls[n:],
            [
                (ROLE_SERVICE, "read", {"i": UUID}),
                (ROLE_SERVICE, "find", {"c": comment}),
                (
                    ROLE_SERVICE,
                    "comment",
                    {"c": comment, "i": UUID, "b": f"Build failed: {TURN_FAILED}"},
                ),
                (ROLE_SERVICE, "reread", {"i": UUID}),
                (ROLE_SERVICE, "state", {"i": UUID, "s": states["in_review"]}),
                (harness, "update", {"c": RUN_SESSION, "b": ended}),
                (harness, "find", {"c": RUN_SESSION}),
            ],
        )
        self.assertEqual(
            self.ledger()[RUN_SID]["steps"],
            [
                "running:1",
                "no-outcome:1:comment",
                "no-outcome:1:move:in_review",
                "no-outcome:1:outcome",
            ],
        )
        self.assertIn(
            f"writeback no-outcome {RUN_ISSUE} sid={RUN_SID} error={TURN_FAILED}", self.said()
        )
        n = len(self.linear.calls)
        self.assertTrue(self.deliver(self.finished(0, error=TURN_FAILED)))
        self.assertEqual(self.linear.calls[n:], [])

    def test_a_failed_researcher_run_without_an_outcome_returns_to_todo_unless_resumed(self):
        self.claim(OTHER_SID, role="researcher")
        for attempt, state in ((1, "todo"), (2, "in_review")):
            with self.subTest(attempt=attempt):
                self.linear.state = self.cfg.linear.states["in_progress"]
                event = delegate_event(
                    "finished",
                    attempt,
                    sid=OTHER_SID,
                    returncode=0,
                    outcome=None,
                    error=TURN_FAILED,
                    limit=None,
                )
                self.assertTrue(self.deliver(event))
                self.assertEqual(self.linear.state, self.cfg.linear.states[state])
                self.assertEqual(
                    self.linear.comments[
                        board_ids.comment_id(
                            "researcher", OTHER_SID, f"no-outcome:{attempt}:comment"
                        )
                    ]["body"],
                    TURN_FAILED,
                )

    def test_a_partial_turn_failure_write_back_leaves_a_later_outcome_its_own_steps(self):
        self.linear.fail = lambda op, _v: (
            LinearError("linear api error: down") if op == "state" else None
        )
        self.assertFalse(self.deliver(self.finished(0, error=TURN_FAILED)))
        self.assertEqual(self.linear.state, self.cfg.linear.states["in_progress"])
        self.linear.fail = lambda _op, _v: None
        self.assertTrue(self.deliver(self.finished(outcome=outcome_data(), attempt=2)))
        self.assertEqual(
            self.linear.comments[board_ids.comment_id("engineer", RUN_SID, "attempt:2:comment")][
                "body"
            ],
            "Build ready:\nAgent report before delivery:\n\n> Ready.",
        )
        self.assertEqual(self.linear.state, self.cfg.linear.states["in_review"])
        self.assertEqual(
            self.ledger()[RUN_SID]["steps"],
            [
                "no-outcome:1:comment",
                "attempt:2:comment",
                f"attach:{PR}",
                "attempt:2:move:in_review",
                "attempt:2:outcome",
            ],
        )

    def test_a_failed_turn_with_a_limit_or_an_outcome_is_not_rewritten(self):
        self.assertTrue(self.deliver(self.finished(1, error=TURN_FAILED, outcome=outcome_data())))
        self.assertEqual(
            self.linear.comments[board_ids.comment_id("engineer", RUN_SID, "comment")]["body"],
            "Build ready:\nAgent report before delivery:\n\n> Ready.",
        )
        self.assertNotIn(
            board_ids.comment_id("engineer", RUN_SID, "no-outcome:1:comment"), self.linear.comments
        )

    def test_a_finished_without_begun_starts_its_session_then(self):
        self.assertTrue(self.deliver(self.finished(1, error="launch failed")))
        body = session_body(f"interrupted · #1 · {STARTED} → {RESTARTED} · exit 1")
        self.assertEqual(self.linear.comments[RUN_SESSION]["body"], body)
        self.assertEqual(
            self.ledger()[RUN_SID]["attempts"],
            {"1": {"started_at": STARTED, "seq": 1, "ended_at": RESTARTED}},
        )

    def test_a_failed_move_leaves_no_outcome_and_a_replay_moves(self):
        self.linear.fail = lambda op, _v: (
            LinearError("linear api error: down") if op == "state" else None
        )
        self.assertFalse(self.deliver(self.finished(outcome=outcome_data())))
        self.assertEqual(self.ledger()[RUN_SID]["steps"], ["comment", f"attach:{PR}"])
        self.linear.fail = lambda _op, _v: None
        n = len(self.linear.calls)
        self.assertTrue(self.deliver(self.finished(outcome=outcome_data())))
        self.assertEqual([op for _, op, _ in self.linear.calls[n:]], ["read", "reread", "state"])
        self.assertEqual(
            self.ledger()[RUN_SID]["steps"],
            ["comment", f"attach:{PR}", "move:in_review", "outcome"],
        )

    def test_an_invalid_outcome_raises_before_any_write(self):
        for name, data in (
            ("a missing field", {"status": "done", "title": "x", "summary": "y"}),
            ("an unknown status", outcome_data(status="maybe")),
            ("an unknown field", outcome_data(extra="x")),
            ("questions not a list of str", outcome_data(questions="why?")),
            ("a non-str title", outcome_data(title=1)),
        ):
            with self.subTest(name), self.assertRaises(ValueError):
                self.deliver(self.finished(outcome=data))
        self.assertEqual(self.linear.calls, [])
        self.assertFalse(os.path.exists(os.path.join(self.workdir, writeback.LEDGER)))


class FollowUpAttempts(Events):
    """FR-30 to FR-32: each attempt of one sid gets its own result, steps and moves, once."""

    NEEDS = outcome_data(status="needs_input", questions=["Which?"], url="")
    DONE_BODY = "Build ready:\nAgent report before delivery:\n\n> Ready."

    def setUp(self):
        super().setUp()
        self.now = iter([STARTED] * 50)

    @staticmethod
    def cid(step):
        return board_ids.comment_id("engineer", RUN_SID, step)

    def asked(self):
        self.assertTrue(self.deliver(delegate_event("begun")))
        self.assertTrue(self.deliver(self.finished(outcome=self.NEEDS)))
        self.assertTrue(self.deliver(delegate_event("begun", 2)))

    def posts(self, step):
        return [
            v for _, op, v in self.linear.calls if op == "comment" and v.get("c") == self.cid(step)
        ]

    def test_needs_input_then_answer_then_done_writes_each_attempt(self):
        states = self.cfg.linear.states
        self.assertTrue(self.deliver(delegate_event("begun")))
        self.assertTrue(self.deliver(self.finished(outcome=self.NEEDS)))
        question = self.linear.comments[self.cid("comment")]["body"]
        self.assertIn("1. Which?", question)
        self.assertEqual(self.linear.state, states["in_review"])
        self.assertTrue(self.deliver(delegate_event("begun", 2)))
        self.assertEqual(self.linear.state, states["in_progress"])
        self.assertTrue(self.deliver(self.finished(outcome=outcome_data(), attempt=2)))
        self.assertEqual(
            self.linear.comments[self.cid("attempt:2:comment")]["body"], self.DONE_BODY
        )
        self.assertEqual(self.linear.comments[self.cid("comment")]["body"], question)
        self.assertEqual(self.linear.state, states["in_review"])
        steps = self.ledger()[RUN_SID]["steps"]
        self.assertEqual(
            steps[steps.index("outcome") :][-3:],
            [f"attach:{PR}", "attempt:2:move:in_review", "attempt:2:outcome"],
        )
        self.assertEqual(steps.count("outcome"), 1)

    def test_replayed_events_of_both_attempts_write_nothing(self):
        events = (
            delegate_event("begun"),
            delegate_event("progress", name="step", text="one"),
            self.finished(outcome=self.NEEDS),
            delegate_event("begun", 2),
            delegate_event("progress", 2, name="step", text="two"),
            self.finished(outcome=outcome_data(), attempt=2),
        )
        for event in events:
            self.assertTrue(self.deliver(event))
        n = len(self.linear.calls)
        for event in events:
            with self.subTest(event=event):
                self.assertTrue(self.deliver(event))
        writes = [op for _, op, _ in self.linear.calls[n:] if op in ("comment", "state")]
        self.assertEqual(writes, [])

    def test_a_redelivered_second_outcome_whose_ledger_write_was_lost_is_found_not_reposted(self):
        self.asked()
        with mock.patch.object(os, "replace", side_effect=OSError("disk full")):
            self.assertFalse(self.deliver(self.finished(outcome=outcome_data(), attempt=2)))
        self.assertTrue(self.deliver(self.finished(outcome=outcome_data(), attempt=2)))
        self.assertEqual(len(self.posts("attempt:2:comment")), 1)
        self.assertEqual(self.linear.state, self.cfg.linear.states["in_review"])
        self.assertEqual(self.ledger()[RUN_SID]["steps"][-1], "attempt:2:outcome")

    def test_a_failed_second_attempt_does_not_rewrite_the_first_done(self):
        self.assertTrue(self.deliver(delegate_event("begun")))
        self.assertTrue(self.deliver(self.finished(outcome=outcome_data())))
        self.assertTrue(self.deliver(delegate_event("begun", 2)))
        self.assertTrue(self.deliver(self.finished(1, error="the client exited 1", attempt=2)))
        self.assertEqual(
            self.linear.comments[self.cid("no-outcome:2:comment")]["body"],
            "Build failed: the client exited 1",
        )
        self.assertEqual(self.linear.state, self.cfg.linear.states["in_review"])
        done = [v for v in self.linear.comments.values() if v["body"] == self.DONE_BODY]
        self.assertEqual(len(done), 1)
        self.assertEqual(len(self.posts("comment")), 1)

    def test_a_failed_second_outcome_step_is_completed_on_redelivery(self):
        self.asked()
        self.linear.fail = lambda op, _v: (
            LinearError("linear api error: down") if op == "state" else None
        )
        self.assertFalse(self.deliver(self.finished(outcome=outcome_data(), attempt=2)))
        self.assertNotIn("attempt:2:outcome", self.ledger()[RUN_SID]["steps"])
        self.linear.fail = lambda _op, _v: None
        self.assertTrue(self.deliver(self.finished(outcome=outcome_data(), attempt=2)))
        self.assertEqual(len(self.posts("attempt:2:comment")), 1)
        self.assertEqual(self.linear.state, self.cfg.linear.states["in_review"])
        self.assertEqual(self.ledger()[RUN_SID]["steps"][-1], "attempt:2:outcome")

    def test_a_user_moved_issue_keeps_its_state_at_the_next_outcome(self):
        self.asked()
        self.linear.state = self.cfg.linear.states["done"]
        self.assertTrue(self.deliver(self.finished(outcome=outcome_data(), attempt=2)))
        self.assertEqual(
            self.linear.comments[self.cid("attempt:2:comment")]["body"], self.DONE_BODY
        )
        self.assertEqual(self.linear.state, self.cfg.linear.states["done"])

    def test_a_stale_finished_writes_its_comment_but_moves_nothing(self):
        self.assertTrue(self.deliver(delegate_event("begun")))
        self.linear.fail = lambda op, _v: (
            LinearError("linear api error: down") if op == "state" else None
        )
        self.assertFalse(self.deliver(self.finished(outcome=self.NEEDS)))
        self.linear.fail = lambda _op, _v: None
        self.assertTrue(self.deliver(delegate_event("begun", 2)))
        n, calls = len(self.linear.writes("state")), len(self.linear.calls)
        self.assertTrue(self.deliver(self.finished(outcome=self.NEEDS)))
        self.assertEqual(len(self.linear.writes("state")), n)
        self.assertNotIn("subscribe", [op for _, op, _ in self.linear.calls[calls:]])
        self.assertEqual(self.linear.state, self.cfg.linear.states["in_progress"])
        steps = self.ledger()[RUN_SID]["steps"]
        self.assertIn("outcome", steps)
        self.assertNotIn("move:in_review", steps)
        self.assertEqual(len(self.posts("comment")), 1)

    def test_a_stale_limit_moves_nothing(self):
        self.assertTrue(self.deliver(delegate_event("begun")))
        self.assertTrue(self.deliver(delegate_event("begun", 2)))
        n = len(self.linear.writes("state"))
        self.assertTrue(self.deliver(self.finished(-9, limit={"resets_at": None})))
        self.assertEqual(len(self.linear.writes("state")), n)
        self.assertEqual(self.linear.state, self.cfg.linear.states["in_progress"])
        self.assertIn(self.cid("limit"), self.linear.comments)
        self.assertNotIn("limit-move:1", self.ledger()[RUN_SID]["steps"])


class LimitEvents(Events):
    def setUp(self):
        super().setUp()
        # POSIX TZ: CST-8 is UTC+8
        tz = mock.patch.dict(os.environ, {"TZ": "CST-8"})
        tz.start()
        self.addCleanup(time.tzset)
        self.addCleanup(tz.stop)
        time.tzset()

    def limited(self, attempt=1, resets_at=T):
        return self.finished(-9, limit={"resets_at": resets_at}, attempt=attempt)

    def rows(self):
        return [
            {k: v for k, v in row.items() if k != "ts"}
            for row in map(json.loads, open(self.cfg.runs_log, encoding="utf-8"))
            if row["kind"] == "limit"
        ]

    def test_a_limit_comments_moves_and_ends_the_session_without_a_quota_row(self):
        self.assertTrue(self.deliver(delegate_event("begun")))
        n = len(self.linear.calls)
        self.assertTrue(self.deliver(self.limited()))
        harness, states = self.cfg.linear.harness.service, self.cfg.linear.states
        ended = session_body(f"limit · #2 · {STARTED} → {RESTARTED} · exit -9")
        self.assertEqual(
            self.linear.calls[n:],
            [
                (ROLE_SERVICE, "find", {"c": board_ids.comment_id("engineer", RUN_SID, "limit")}),
                (
                    ROLE_SERVICE,
                    "comment",
                    {
                        "c": board_ids.comment_id("engineer", RUN_SID, "limit"),
                        "i": UUID,
                        "b": "Usage limit, resets at 2026-10-11T18:00:00+08:00.",
                    },
                ),
                (ROLE_SERVICE, "reread", {"i": UUID}),
                (ROLE_SERVICE, "state", {"i": UUID, "s": states["in_review"]}),
                (harness, "update", {"c": RUN_SESSION, "b": ended}),
                (harness, "find", {"c": RUN_SESSION}),
            ],
        )
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.ledger()[RUN_SID]["steps"], ["running:1", "limit", "limit-move:1"])

    def test_an_outcome_with_a_limit_writes_both_and_moves_once(self):
        both = self.finished(-9, outcome=outcome_data(), limit={"resets_at": None})
        self.assertTrue(self.deliver(both))
        self.assertEqual(
            self.ledger()[RUN_SID]["steps"],
            [
                "comment",
                f"attach:{PR}",
                "move:in_review",
                "outcome",
                "limit",
                "limit-move:1",
            ],
        )
        self.assertEqual(len(self.linear.writes("state")), 1)
        self.assertEqual(self.rows(), [])
        self.assertEqual(
            self.linear.comments[RUN_SESSION]["body"],
            session_body(f"done · #1 · {STARTED} → {RESTARTED} · exit -9"),
        )

    def test_an_unknown_reset_time_says_so(self):
        for index, resets_at in enumerate((None, 1e20)):
            with self.subTest(resets_at=resets_at):
                sid = f"0b6f2c1e-6d0a-4c1b-9a51-3f1f6b0e2a{index:02d}"
                self.claim(sid)
                event = delegate_event("finished", sid=sid, returncode=-9, outcome=None)
                self.assertTrue(self.deliver(replace(event, limit={"resets_at": resets_at})))
                body = self.linear.comments[board_ids.comment_id("engineer", sid, "limit")]["body"]
                self.assertEqual(body, "Usage limit, reset time unknown.")
                self.linear.state = self.cfg.linear.states["in_progress"]

    def test_local_time(self):
        self.assertEqual(writeback.local_time(T), "2026-10-11T18:00:00+08:00")
        self.assertIsNone(writeback.local_time(None))

    def test_the_humans_are_subscribed_and_a_failed_subscription_noted(self):
        self.cfg = replace(self.cfg, linear=replace(self.cfg.linear, human_members=HUMANS))

        def fail(op, v):
            if op == "subscribe" and v["e"] == HUMANS[0]:
                return LinearError("issueSubscribe: success: false")
            return None

        self.linear.fail = fail
        self.assertTrue(self.deliver(self.limited()))
        mine = [(op, v) for service, op, v in self.linear.calls if service == ROLE_SERVICE]
        self.assertEqual(
            [op for op, _ in mine], ["subscribe", "subscribe", "find", "comment", "reread", "state"]
        )
        self.assertEqual([v["e"] for op, v in mine if op == "subscribe"], list(HUMANS))
        self.assertEqual(
            self.linear.comments[board_ids.comment_id("engineer", RUN_SID, "limit")]["body"],
            "Usage limit, resets at 2026-10-11T18:00:00+08:00.\n\n"
            "Could not subscribe ann@example.com: LinearError: issueSubscribe: success: false",
        )

    def test_it_moves_to_in_review_only_from_in_progress(self):
        for attempt, state, skip in (
            (1, "in_review", None),
            (2, "handoff", f"skip {RUN_ISSUE} reason=move to in_review: issue is "),
        ):
            with self.subTest(state=state):
                self.linear.state = self.cfg.linear.states[state]
                before = len(self.said())
                self.assertTrue(self.deliver(self.limited(attempt)))
                self.assertEqual(self.linear.state, self.cfg.linear.states[state])
                skips = [line for line in self.said()[before:] if line.startswith("writeback skip")]
                self.assertEqual(
                    [line.startswith(f"writeback {skip}") for line in skips],
                    [True] if skip else [],
                )

    def test_a_failed_limit_comment_stops_before_the_move(self):
        self.linear.fail = lambda op, _v: (
            LinearError("commentCreate: success: false") if op == "comment" else None
        )
        self.assertFalse(self.deliver(self.limited()))
        mine = [op for service, op, _ in self.linear.calls if service == ROLE_SERVICE]
        self.assertEqual(mine, ["find", "comment"])
        self.assertIn(
            f"writeback step-error {RUN_ISSUE} sid={RUN_SID} step=limit "
            "error=LinearError: commentCreate: success: false",
            self.said(),
        )

    def test_a_linear_failure_in_the_limit_is_a_step_error_never_raised(self):
        self.cfg = replace(self.cfg, linear=replace(self.cfg.linear, human_members=HUMANS))
        for index, (op, step) in enumerate(
            (("reread", "limit-move:1"), ("state", "limit-move:1"), ("subscribe", "limit"))
        ):
            with self.subTest(op=op):
                sid = f"0b6f2c1e-6d0a-4c1b-9a51-3f1f6b0e2b{index:02d}"
                self.claim(sid)
                self.now = iter((STARTED, RESTARTED))
                self.linear.state = self.cfg.linear.states["in_progress"]
                self.linear.fail = lambda o, _v, op=op: (
                    LinearError("linear api error: down") if o == op else None
                )
                event = delegate_event("finished", sid=sid, returncode=-9, outcome=None)
                ok = self.deliver(replace(event, limit={"resets_at": T}))
                session = board_ids.comment_id("harness", sid, "session")
                self.assertIn("· limit · ", self.linear.comments[session]["body"])
                if op == "subscribe":
                    self.assertTrue(ok)
                    self.assertIn(
                        "Could not subscribe",
                        self.linear.comments[board_ids.comment_id("engineer", sid, "limit")][
                            "body"
                        ],
                    )
                else:
                    self.assertFalse(ok)
                    self.assertTrue(
                        any(
                            line.startswith(
                                f"writeback step-error {RUN_ISSUE} sid={sid} step={step} "
                            )
                            for line in self.said()
                        ),
                        self.said(),
                    )

    def test_the_same_limit_twice_moves_once_without_a_quota_row(self):
        self.assertTrue(self.deliver(self.limited()))
        n = len(self.linear.calls)
        self.assertTrue(self.deliver(self.limited()))
        self.assertEqual(self.linear.calls[n:], [])
        self.assertEqual(len(self.linear.writes("state")), 1)
        self.assertEqual(self.rows(), [])

    def test_a_later_attempts_limit_moves_again_without_a_second_comment(self):
        self.assertTrue(self.deliver(self.limited()))
        self.linear.state = self.cfg.linear.states["in_progress"]
        n = len(self.linear.calls)
        self.assertTrue(self.deliver(self.limited(attempt=2)))
        self.assertEqual(
            [op for service, op, _ in self.linear.calls[n:] if service == ROLE_SERVICE],
            ["reread", "state"],
        )
        self.assertEqual(len([c for c in self.linear.comments if c != RUN_SESSION]), 1)
        self.assertEqual(self.rows(), [])
        self.assertEqual(
            self.ledger()[RUN_SID]["steps"],
            ["limit", "limit-move:1", "limit-move:2"],
        )

    def test_two_writers_lose_no_step(self):
        one, two = (delegate_event("progress", name="build", text=t) for t in ("one", "two"))
        results, threads = self.racing(writeback, "save", lambda: self.deliver(two))
        self.assertTrue(self.deliver(one))
        threads[0].join()
        self.assertEqual(results, [True])
        digest = {t: hashlib.sha256(t.encode()).hexdigest()[:12] for t in ("one", "two")}
        self.assertEqual(
            self.ledger()[RUN_SID]["steps"],
            ["running:1", f"progress:build:{digest['one']}", f"progress:build:{digest['two']}"],
        )


class RecoverReplay(Events):
    """Recover's in-process write-back of an ended run's outcome (FR-41)."""

    def outcome(self):
        data = outcome_data()
        return Outcome(**data | {"status": OutcomeStatus(data["status"])})

    def replay(self):
        return writeback.replay(
            self.cfg,
            RUN_ISSUE,
            RUN_SID,
            self.outcome(),
            writers=self.writers(),
            now=lambda: next(self.now),
        )

    def test_a_replay_ends_the_attempt_its_begun_recorded_past_a_failed_resume_row(self):
        for _ in range(2):  # a resume refused before its launch, then the one that ran
            self.resumed(BEFORE)
        self.assertTrue(self.deliver(delegate_event("begun", 2)))
        self.assertTrue(self.replay())
        self.assertEqual(
            self.linear.comments[RUN_SESSION]["body"],
            session_body(f"done · #2 · {STARTED} → {RESTARTED}"),
        )

    def resumed(self, at):
        runs.note(
            self.cfg.runs_log,
            "resume",
            now=lambda: at,
            issue=RUN_ISSUE,
            sid=RUN_SID,
            role="engineer",
            client="fake",
            delegate_kind="fake",
        )

    def test_a_replay_ends_a_resumed_attempt_whose_begun_was_lost_too(self):
        self.assertTrue(self.deliver(delegate_event("begun")))
        self.resumed(datetime.fromisoformat(STARTED) + timedelta(minutes=30))
        self.assertTrue(self.replay())
        self.assertEqual(
            self.linear.comments[RUN_SESSION]["body"],
            session_body(f"done · #1 · {RESTARTED} → {LATER}"),
        )
        self.assertEqual(sorted(self.ledger()[RUN_SID]["attempts"]), ["1", "2"])

    def test_a_replay_writes_a_second_attempts_outcome_after_the_first(self):
        self.now = iter([STARTED] * 20)
        needs = outcome_data(status="needs_input", questions=["Which?"], url="")
        self.assertTrue(self.deliver(delegate_event("begun")))
        self.assertTrue(self.deliver(self.finished(outcome=needs)))
        self.assertTrue(self.deliver(delegate_event("begun", 2)))
        self.assertTrue(self.replay())
        second = board_ids.comment_id("engineer", RUN_SID, "attempt:2:comment")
        self.assertEqual(
            self.linear.comments[second]["body"],
            "Build ready:\nAgent report before delivery:\n\n> Ready.",
        )
        self.assertEqual(self.linear.state, self.cfg.linear.states["in_review"])
        self.assertIsNone(self.replay())

    def test_a_replay_of_a_lost_finished_ends_its_session_comment_once(self):
        self.assertTrue(self.deliver(delegate_event("begun")))
        self.assertTrue(self.replay())
        ended = session_body(f"done · #2 · {STARTED} → {RESTARTED}")
        self.assertEqual(self.linear.comments[RUN_SESSION]["body"], ended)
        self.assertTrue(self.deliver(self.finished(outcome=outcome_data())))
        self.assertEqual(self.linear.comments[RUN_SESSION]["body"], ended)

    def test_a_callback_and_recover_at_once_write_each_step_once(self):
        results, threads = self.racing(writeback, "save", self.replay)
        self.assertTrue(self.deliver(self.finished(outcome=outcome_data())))
        threads[0].join()
        self.assertEqual(results, [True])
        self.assertEqual(
            self.ledger()[RUN_SID]["steps"],
            ["comment", f"attach:{PR}", "move:in_review", "outcome"],
        )
        self.assertEqual(len(self.linear.writes("state")), 1)
        self.assertEqual(len([c for c in self.linear.comments if c != RUN_SESSION]), 1)

    def test_a_first_event_makes_its_workdir(self):
        shutil.rmtree(self.workdir)
        self.assertTrue(self.deliver(delegate_event("progress", name="build", text="one")))
        self.assertEqual(len(self.ledger()[RUN_SID]["steps"]), 2)

    def test_ledger_and_lock_live_in_the_workdir_root(self):
        before = sorted(os.listdir(self.workdir))
        self.assertTrue(self.deliver(delegate_event("progress", name="start", text="x")))
        self.assertEqual(self.ledger(), {RUN_SID: {"steps": ["running:1", "start"]}})
        self.assertEqual(
            sorted(os.listdir(self.workdir)),
            sorted([*before, writeback.LEDGER, writeback.LOCK]),
        )


DELIVERABLE_FLOW = board_ids.PACKAGED_FLOW.replace(
    "files = true\n", "files = true\ndeliverable = true\n"
)

REPORT = (
    "# Verification\n\nPassed.\n\n```bash\nssh mini echo ok\n```\n\n"
    + "".join(f"- step {n}: `check {n}` ok\n" for n in range(300))
    + "\n```text\nend of log\n```\n"
)


class DeliverableReport(Base):
    """A deliverable stage's done outcome: its summary unquoted, then the saved report (FR-76)."""

    DONE = Outcome(status="done", title="ENG-7: x", summary="Passed: all checks.", url=PR)
    LEAD = "Build ready: Passed: all checks."

    def setUp(self):
        super().setUp()
        config.TASKS.load(board_ids.workflow_file(DELIVERABLE_FLOW))

    def comments(self, gql):
        return [c for c in gql.calls if c[0] == "comment"]

    def test_the_report_follows_the_done_comment_byte_for_byte(self):
        self.assertGreater(len(REPORT.encode()), 4096)
        gql = Gql()
        ctx = self.ctx(gql=gql)
        self.write(ctx, "run/out.md", REPORT)
        self.assertTrue(writeback.finish(ctx, self.DONE))
        calls = expect(body=self.LEAD, state="in_review", attach=PR, attach_title="ENG-7: x")
        calls.insert(calls.index(post("comment", self.LEAD)) + 1, post("deliverable", REPORT))
        self.assertEqual(gql.calls, calls)
        self.assertEqual(
            self.steps(ctx),
            {SID: ["comment", "deliverable", f"attach:{PR}", "move:in_review", "outcome"]},
        )

    def test_a_role_with_a_docs_dir_posts_its_document(self):
        gql = Gql()
        ctx = replace(self.ctx(gql=gql), document=os.path.join(self.tmp, "docs", "document.md"))
        os.makedirs(os.path.dirname(ctx.document))
        with open(ctx.document, "w") as f:
            f.write(REPORT)
        self.write(ctx, "run/out.md", "not this")
        self.assertTrue(writeback.finish(ctx, self.DONE))
        self.assertEqual(
            self.comments(gql), [post("comment", self.LEAD), post("deliverable", REPORT)]
        )

    def test_no_report_notes_why_in_the_done_comment(self):
        for name, make, reason in (
            ("missing", lambda _ctx: None, "FileNotFoundError"),
            ("empty", lambda ctx: self.write(ctx, "run/out.md", " \n"), "empty"),
            (
                "too big",
                lambda ctx: self.write(ctx, "run/out.md", "x" * 1_000_001),
                "ValueError: over 1000000 bytes",
            ),
        ):
            with self.subTest(name):
                gql = Gql()
                ctx = self.ctx(gql=gql)
                make(ctx)
                self.assertTrue(writeback.finish(ctx, self.DONE))
                [(_, sent)] = self.comments(gql)
                self.assertTrue(
                    sent["b"].startswith(f"{self.LEAD}\n\nCould not post the report `out.md`: "),
                    sent["b"],
                )
                self.assertIn(reason, sent["b"])
                self.assertEqual(gql.calls[-2:], [reread(), move("in_review")])

    def test_failed_and_needs_input_write_back_as_without_the_key(self):
        for o in (
            Outcome(status="failed", title="ENG-7: x", summary="mini unreachable", url=PR),
            Outcome(status="needs_input", title="ENG-7: x", summary="?", questions=["Which ref?"]),
        ):
            with self.subTest(o.status):
                calls = []
                for flow in (DELIVERABLE_FLOW, board_ids.PACKAGED_FLOW):
                    config.TASKS.load(board_ids.workflow_file(flow))
                    gql = Gql()
                    ctx = self.ctx(gql=gql)
                    self.write(ctx, "run/out.md", REPORT)
                    self.assertTrue(writeback.finish(ctx, o))
                    calls.append(gql.calls)
                self.assertEqual(calls[0], calls[1])

    def test_a_failed_report_post_stops_before_the_move_and_a_retry_posts_it_once(self):
        broken = Gql(
            fail=lambda name, v: (
                LinearError("linear api error: down")
                if name == "comment" and v.get("b") == REPORT
                else None
            )
        )
        ctx = self.ctx(gql=broken)
        self.write(ctx, "run/out.md", REPORT)
        self.assertFalse(writeback.finish(ctx, self.DONE))
        self.assertNotIn(move("in_review"), broken.calls)
        self.assertEqual(self.steps(ctx), {SID: ["comment"]})
        again = Gql()
        self.assertTrue(writeback.finish(replace(ctx, tracker=tracker(again)), self.DONE))
        self.assertEqual(self.comments(again), [post("deliverable", REPORT)])


class DeliverableReplay(Events):
    """Recover posts a deliverable stage's report from the run dir: the run record's outcome
    carries no deliverable.
    """

    def setUp(self):
        super().setUp()
        config.TASKS.load(board_ids.workflow_file(DELIVERABLE_FLOW))
        os.makedirs(os.path.join(self.workdir, "run"), exist_ok=True)
        with open(os.path.join(self.workdir, "run", "out.md"), "w") as f:
            f.write(REPORT)

    def replay(self):
        data = outcome_data()
        return writeback.replay(
            self.cfg,
            RUN_ISSUE,
            RUN_SID,
            Outcome(**data | {"status": OutcomeStatus(data["status"])}),
            writers=self.writers(),
            now=lambda: next(self.now),
        )

    def posted(self):
        return [
            v["b"]
            for service, op, v in self.linear.calls
            if service == ROLE_SERVICE and op == "comment"
        ]

    def test_recover_posts_the_report_from_the_run_dir(self):
        self.assertTrue(self.deliver(delegate_event("begun")))
        self.assertTrue(self.replay())
        self.assertEqual(self.posted(), ["Build ready: Ready.", REPORT])

    def test_a_callback_then_a_replay_post_the_report_once(self):
        self.assertTrue(self.deliver(self.finished(outcome=outcome_data(deliverable=REPORT))))
        self.assertIsNone(self.replay())
        self.assertEqual(self.posted(), ["Build ready: Ready.", REPORT])


BUSY_ERROR = "busy: lock dummy-lock is held by ensemble-x-1 (owner x)"


class BusyFinished(Events):
    def _failure_comment(self, error):
        bodies = [v["body"] for v in self.linear.comments.values() if error in v["body"]]
        self.assertEqual(len(bodies), 1)
        return bodies[0]

    def test_client_capacity_failure_comments_and_counts_one_attempt(self):
        error = "client fake reached max_runs limit 1; held by: worker-one"
        for _ in range(2):
            self.assertTrue(self.deliver(self.finished(1, error=error)))
        self._failure_comment(error)
        self.assertEqual(router.attempt_count(runs.parse_log(self.cfg.runs_log), RUN_ISSUE), 1)
        self.assertEqual(self.linear.state, self.cfg.linear.states["in_review"])

    def test_busy_start_is_a_normal_failure_comment_and_attempt_once(self):
        for _ in range(2):
            self.assertTrue(self.deliver(self.finished(75, error=BUSY_ERROR)))
        body = self._failure_comment(BUSY_ERROR)
        self.assertIn(BUSY_ERROR, body)
        self.assertEqual(self.linear.state, self.cfg.linear.states["in_review"])
        self.assertEqual(router.attempt_count(runs.parse_log(self.cfg.runs_log), RUN_ISSUE), 1)
        with open(self.cfg.runs_log) as stream:
            self.assertNotIn("busy", [json.loads(line)["kind"] for line in stream])

    def test_busy_resume_is_a_normal_failure_and_keeps_its_attempt(self):
        runs.note(
            self.cfg.runs_log,
            "resume",
            now=lambda: BEFORE + timedelta(minutes=10),
            issue=RUN_ISSUE,
            sid=RUN_SID,
            role="engineer",
            client="fake",
            delegate_kind="fake",
        )
        for _ in range(2):
            self.assertTrue(self.deliver(self.finished(75, error=BUSY_ERROR, attempt=2)))
        body = self._failure_comment(BUSY_ERROR)
        self.assertIn(BUSY_ERROR, body)
        self.assertEqual(self.linear.state, self.cfg.linear.states["in_review"])
        self.assertEqual(router.attempt_count(runs.parse_log(self.cfg.runs_log), RUN_ISSUE), 2)

    def test_four_busy_attempts_write_four_failures(self):
        self.now = iter([STARTED] * 20)
        for attempt in range(1, 5):
            with self.subTest(attempt=attempt):
                self.linear.state = self.cfg.linear.states["in_progress"]
                self.assertTrue(self.deliver(self.finished(75, error=BUSY_ERROR, attempt=attempt)))
                failure = board_ids.comment_id("engineer", RUN_SID, f"no-outcome:{attempt}:comment")
                self.assertIn(BUSY_ERROR, self.linear.comments[failure]["body"])
                self.assertEqual(self.linear.state, self.cfg.linear.states["in_review"])
        failures = [v for v in self.linear.comments.values() if BUSY_ERROR in v["body"]]
        self.assertEqual(len(failures), 4)

    def test_an_environment_start_failure_comments_naming_the_image_or_gateway(self):
        for attempt, error, named in (
            (1, "new: base image dummy-env:test is not on this Mac", "dummy-env:test"),
            (2, "resume: gateway container windroc-gateway is not running", "windroc-gateway"),
        ):
            with self.subTest(named=named):
                self.assertTrue(self.deliver(self.finished(1, error=error, attempt=attempt)))
                self.assertIn(named, self._failure_comment(error))
