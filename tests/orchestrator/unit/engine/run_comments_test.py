"""Run comments: one comment written by its session id,
numbered `#<seq>`, repaired at the end after a late write; the resume command is the run view's
session_status."""

import threading
import time
from datetime import datetime
from unittest import mock

from ensemble.delegate import ConfigError, Outcome
from ensemble.orchestrator.engine import run_comments
from ensemble.orchestrator.engine.types import LinearError, Stopped
from ensemble.orchestrator.trackers.linear import linear_tracker, ops
from ensemble.orchestrator.trackers.linear.linear_tracker import LinearTracker, Selves
from ensemble.session import SessionStatus
from tests.orchestrator import case
from tests.orchestrator.process_fixtures import CalledProcessError
from tests.orchestrator.unit.board_ids import HARNESS_USER, MANAGER_USER, role_id

SID = "0b6f2c1e-6d0a-4c1b-9a51-3f1f6b0e2a7d"

OTHER = "7c1d9e2f-0a3b-4c5d-8e6f-1a2b3c4d5e6f"

ISSUE, ISSUE_ID = "TASK-12", "00000000-0000-4000-8000-000000000012"

OTHER_ISSUE = "00000000-0000-4000-8000-000000000013"

CID = ops.client_id(SID, "session")

T1, T2, T3 = "2026-09-30T10:00:00+08:00", "2026-09-30T11:30:00+08:00", "2026-09-30T12:00:00+08:00"

WORKDIR, CLIENT = "/w/TASK-12", "claude"

CMD = f"cd /w/TASK-12 && claude --resume {SID}"

NOT_FOUND = LinearError("linear api error: [{'message': 'Entity not found: Comment'}]")


def record(info, started_at=T1):
    return run_comments.base(
        sid=SID, workdir=WORKDIR, client=CLIENT, started_at=started_at, info=info
    )


def text(line):
    return f"Run {SID} · {line}\n\n```\n{CMD}\n```"


def comment(id, body, issue=ISSUE_ID, *, mine=True):
    return {"id": id, "body": body, "mine": mine, "issue": issue}


def start(gql, rec, timeout=run_comments.LIMIT):
    return run_comments.post(ISSUE, rec, tracker=LinearTracker(gql), timeout=timeout)


def end(gql, rec, rc, ended_at=T2):
    return run_comments.post(ISSUE, rec, tracker=LinearTracker(gql), rc=rc, ended_at=ended_at)


class FakeLinear:
    def __init__(self, *comments, fail=None):
        self.comments = {c["id"]: dict(c) for c in comments}
        self.fail, self.calls = dict(fail or {}), []

    def __call__(self, query, **v):
        op = next(o for o in ("commentUpdate", "commentCreate", "comment") if o + "(" in query)
        self.calls.append(op)
        if op in self.fail:
            if self.fail[op] is False:
                return {op: {"success": False}}
            raise self.fail[op]
        if op == "comment":
            assert query == ops.Q_COMMENT, query
            c = self.comments.get(v["c"])
            if c is None:
                raise NOT_FOUND
            user = {"isMe": c["mine"]}
            return {
                "comment": {
                    "id": c["id"],
                    "body": c["body"],
                    "issue": {"id": c["issue"]},
                    "user": user,
                }
            }
        if op == "commentCreate":
            assert query == ops.M_COMMENT_ID and v["i"] == ISSUE, (query, v)
            assert v["c"] not in self.comments, "fake: comment id in use"
            self.comments[v["c"]] = comment(v["c"], v["b"])
        else:
            assert query == linear_tracker.M_UPDATE, query
            if v["c"] not in self.comments:
                raise NOT_FOUND
            self.comments[v["c"]]["body"] = v["b"]
        return {op: {"success": True}}

    def only(self):
        assert len(self.comments) == 1, self.comments
        assert CID in self.comments, self.comments
        return self.comments[CID]


class Late:
    """Hold the first write until released, so it can arrive after timing out."""

    def __init__(self, gql):
        self.gql, self.first = gql, True
        self.held, self.release, self.landed = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )

    def __call__(self, query, **v):
        if not self.first:
            return self.gql(query, **v)
        self.first = False
        self.held.set()
        self.release.wait(30)
        try:
            return self.gql(query, **v)
        finally:
            self.landed.set()


class Case(case.Case):
    def setUp(self):
        self.info = mock.Mock(return_value=SessionStatus("yes", None, CMD))

    def record(self, started_at=T1):
        return record(self.info, started_at)


class Record(Case):
    def test_base(self):
        self.assertEqual(
            self.record(),
            {
                "sid": SID,
                "workdir": WORKDIR,
                "client": CLIENT,
                "started_at": T1,
                "seq": 0,
                "info": self.info,
            },
        )

    def test_post_creates_by_session_id_then_updates_it(self):
        for rc, line in (
            (None, f"running · #1 · {T1}"),
            (0, f"no outcome · #2 · {T1} → {T2} · exit 0"),
            (3, f"interrupted · #2 · {T1} → {T2} · exit 3"),
        ):
            with self.subTest(rc=rc):
                fake, rec = FakeLinear(), self.record()
                self.assertIsNone(start(fake, rec))
                if rc is not None:
                    self.assertIsNone(end(fake, rec, rc))
                ended = ["commentUpdate"] if rc is not None else []
                self.assertEqual(fake.calls, ["commentUpdate", "commentCreate", *ended])
                self.assertEqual(fake.only()["body"], text(line))
                self.assertEqual(rec["last"], 1 if rc is None else 2)

    def test_a_null_update_is_not_found(self):
        calls = []

        def gql(query, **v):
            calls.append((query, v))
            if query == linear_tracker.M_UPDATE:
                return {"commentUpdate": None}
            return {"commentCreate": {"success": True}}

        self.assertIsNone(start(gql, self.record()))
        body = text(f"running · #1 · {T1}")
        self.assertEqual(
            calls,
            [
                (linear_tracker.M_UPDATE, {"c": CID, "b": body}),
                (ops.M_COMMENT_ID, {"c": CID, "i": ISSUE, "b": body}),
            ],
        )

    def test_each_body_takes_the_next_seq(self):
        rec = self.record()
        self.assertEqual(run_comments.body(rec), text(f"running · #1 · {T1}"))
        self.assertEqual(
            run_comments.body(rec, 0, T2), text(f"no outcome · #2 · {T1} → {T2} · exit 0")
        )
        self.assertEqual(rec["seq"], 2)

    def test_terminal_outcome_keeps_status_and_native_exit_diagnostic(self):
        for status in ("done", "needs_input", "failed"):
            with self.subTest(status=status):
                fake = FakeLinear()
                self.assertIsNone(
                    run_comments.post(
                        ISSUE,
                        self.record(),
                        tracker=LinearTracker(fake),
                        rc=3,
                        ended_at=T2,
                        outcome=Outcome(status, "title", "summary"),
                    )
                )
                line = fake.only()["body"].splitlines()[0]
                self.assertEqual(line, f"Run {SID} · {status} · #1 · {T1} → {T2} · exit 3")
                self.assertNotIn("interrupted", line)

    def test_a_limit_ended_run_says_limit_unless_it_reported_an_outcome(self):
        rec, asked = self.record(), Outcome("needs_input", "title", "summary")
        self.assertEqual(run_comments.body(rec, limit=True), text(f"running · #1 · {T1}"))
        self.assertEqual(
            run_comments.body(rec, -9, T2, limit=True), text(f"limit · #2 · {T1} → {T2} · exit -9")
        )
        self.assertEqual(
            run_comments.body(rec, -9, T2, outcome=asked, limit=True),
            text(f"needs_input · #3 · {T1} → {T2} · exit -9"),
        )
        fake = FakeLinear()
        self.assertIsNone(
            run_comments.post(
                ISSUE,
                self.record(),
                tracker=LinearTracker(fake),
                rc=-9,
                ended_at=T2,
                limit=True,
            )
        )
        self.assertEqual(fake.only()["body"], text(f"limit · #1 · {T1} → {T2} · exit -9"))

    def test_recover_resume_updates_the_one_comment(self):
        fake, rec = FakeLinear(), self.record()
        start(fake, rec)
        end(fake, rec, 3)
        start(fake, self.record(started_at=T2))
        body = fake.only()["body"]
        self.assertEqual(body.splitlines()[0], f"Run {SID} · running · #1 · {T2}")
        self.assertEqual(body, run_comments.body(self.record(started_at=T2)))

    def test_end_defaults_to_now(self):
        fake = FakeLinear()
        self.assertIsNone(
            run_comments.post(ISSUE, self.record(), tracker=LinearTracker(fake), rc=0)
        )
        body = fake.only()["body"]
        stamp = body.split(" → ", 1)[1].split(" · ", 1)[0]
        self.assertEqual(body, text(f"no outcome · #1 · {T1} → {stamp} · exit 0"))
        self.assertIsNotNone(datetime.fromisoformat(stamp).utcoffset(), stamp)
        self.assertNotEqual(stamp, T1)

    def test_failed_start_leaves_end_to_create_the_comment(self):
        fake, rec = FakeLinear(fail={"commentCreate": RuntimeError("down")}), self.record()
        self.assertEqual(start(fake, rec), "RuntimeError: down")
        self.assertEqual(fake.comments, {})
        fake.fail = {}
        self.assertIsNone(end(fake, rec, 0))
        self.assertEqual(
            fake.calls, ["commentUpdate", "commentCreate", "commentUpdate", "commentCreate"]
        )
        self.assertEqual(fake.only()["body"], text(f"no outcome · #2 · {T1} → {T2} · exit 0"))

    def test_writes_only_its_session_id(self):
        mine = f"Run {SID} · running · #1 · {T1}"
        others = [
            comment(ops.client_id(OTHER, "session"), f"Run {OTHER} · running · #1 · {T1}"),
            comment("human", mine, mine=False),
            comment("legacy", f"Run {SID} · running · {T1}"),
        ]
        fake = FakeLinear(*others, comment(CID, mine))
        self.assertIsNone(start(fake, self.record(started_at=T2)))
        self.assertEqual(fake.calls, ["commentUpdate"])
        self.assertEqual(fake.comments[CID]["body"], text(f"running · #1 · {T2}"))
        self.assertEqual([fake.comments[c["id"]] for c in others], others)

    def test_is_run_by_the_harness_app_id(self):
        body = run_comments.body(self.record())
        selves = Selves(HARNESS_USER, frozenset({role_id("pm")}), MANAGER_USER)
        harness = {"id": HARNESS_USER}
        self.assertTrue(selves.is_run({"user": harness, "body": body}))
        for name, c in (
            ("user, same body", {"user": {"id": "u-me"}, "body": body}),
            ("role app, same body", {"user": {"id": role_id("pm")}, "body": body}),
            ("manager app, same body", {"user": {"id": MANAGER_USER}, "body": body}),
            ("isMe, same body", {"user": {"id": "u-me", "isMe": True}, "body": body}),
            ("harness, no prefix", {"user": harness, "body": "Repo check failed: no Repo: line"}),
            ("sid not a uuid", {"user": harness, "body": "Run s-1 · x"}),
            ("no user", {"user": None, "body": body}),
            ("prefix not at the start", {"user": harness, "body": "see " + body}),
        ):
            with self.subTest(name):
                self.assertFalse(selves.is_run(c))

    def test_is_record_by_url(self):
        self.assertTrue(
            linear_tracker.is_record({"title": f"Run {SID}", "url": linear_tracker.URL + SID})
        )
        self.assertFalse(
            linear_tracker.is_record({"title": "Run rate analysis", "url": "https://gh/x"})
        )

    def test_limit_and_no_default_identity(self):
        self.assertEqual(run_comments.LIMIT, 10)
        with self.assertRaises(TypeError):
            run_comments.post(ISSUE, self.record())


class Command(Case):
    def test_the_run_view_resolves_its_resume_command(self):
        self.assertEqual(run_comments.command(self.record()), CMD)
        self.info.assert_called_once_with()

    def test_built_fresh_each_time_never_read_from_the_record(self):
        rec = self.record()
        rec["command"] = "rm -rf ~"
        self.assertEqual(run_comments.body(rec), text(f"running · #1 · {T1}"))
        self.info.return_value = SessionStatus("yes", None, "resume it")
        self.assertEqual(
            run_comments.body(rec), f"Run {SID} · running · #2 · {T1}\n\n```\nresume it\n```"
        )
        self.assertEqual(self.info.call_count, 2)

    def test_no_command_block(self):
        for name, effect in (
            ("raises", ConfigError("bad sid")),
            ("unknown", SessionStatus("unknown", None, CMD)),
            ("empty", SessionStatus("yes", None, "")),
        ):
            with self.subTest(name):
                self.info.side_effect = effect if isinstance(effect, Exception) else None
                self.info.return_value = effect
                rec = self.record()
                self.assertIsNone(run_comments.command(rec))
                self.assertEqual(run_comments.body(rec), f"Run {SID} · running · #1 · {T1}")


class Failure(Case):
    def failing(self, exc):
        def gql(_query, **_v):
            raise exc

        return gql

    def test_an_unexpected_worker_error_keeps_its_original_type(self):
        for error in (
            AttributeError("worker defect"),
            ImportError("missing worker module"),
            SystemExit(3),
        ):
            with self.subTest(error=error):
                self.assertEqual(
                    start(self.failing(error), self.record()),
                    f"{type(error).__name__}: {error}",
                )

    def test_worker_control_flow_propagates_to_the_caller(self):
        for error in (KeyboardInterrupt(), Stopped(143)):
            with self.subTest(error=error), self.assertRaises(type(error)) as caught:
                run_comments._bounded(lambda error=error: self.failing(error)("query"), 1)
            self.assertIs(caught.exception, error)

    def test_update_failure_never_creates(self):
        for exc, reason in (
            (RuntimeError("boom"), "RuntimeError: boom"),
            (
                LinearError("linear api error: [{'message': 'nope'}]"),
                "LinearError: linear api error: [{'message': 'nope'}]",
            ),
        ):
            with self.subTest(reason=reason):
                fake = FakeLinear(fail={"commentUpdate": exc})
                self.assertEqual(start(fake, self.record()), reason)
                self.assertEqual(fake.calls, ["commentUpdate"])
        calls = []

        def malformed(query, **_v):
            calls.append(query)
            return {}

        self.assertRegex(end(malformed, self.record(), 0), r"\AKeyError: [^\n]+\Z")
        self.assertEqual(len(calls), 1)

    def test_write_failure_logs_one_line(self):
        existing = comment(CID, f"Run {SID} · running · #1 · {T1}")
        for op, seed, calls in (
            ("commentCreate", (), ["commentUpdate", "commentCreate"]),
            ("commentUpdate", (existing,), ["commentUpdate"]),
        ):
            for exc, reason in (
                (RuntimeError("boom"), "RuntimeError: boom"),
                (
                    LinearError("linear api error: [{'message': 'nope'}]"),
                    "LinearError: linear api error: [{'message': 'nope'}]",
                ),
                (RuntimeError("two\nlines"), "RuntimeError: two lines"),
                (False, "success: false"),
            ):
                with self.subTest(op=op, reason=reason):
                    fake = FakeLinear(*seed, fail={op: exc})
                    self.assertEqual(start(fake, self.record()), reason)
                    self.assertEqual(fake.calls, calls)

    def test_blocking_gql_times_out(self):
        release = threading.Event()
        self.addCleanup(release.set)
        t = time.monotonic()
        out = run_comments.post(
            ISSUE,
            self.record(),
            tracker=LinearTracker(lambda _q, **_v: release.wait(30)),
            timeout=0.1,
            rc=0,
        )
        self.assertLess(time.monotonic() - t, 5)
        self.assertEqual(out, "timed out after 0.1s")

    def test_secret_outside_the_message_does_not_leak(self):
        exc = CalledProcessError(
            1,
            ["security", "find-generic-password", "-s", "svc", "-w"],
            output="lin_api_SECRET",
            stderr="lin_api_SECRET",
        )
        exc.headers = {"Authorization": "lin_api_SECRET"}
        out = start(self.failing(exc), self.record())
        self.assertTrue(out.startswith("CalledProcessError: "), out)
        self.assertNotIn("lin_api_SECRET", out)


class Repair(Case):
    DONE = f"{T1} → {T2} · exit 0"

    def setUp(self):
        super().setUp()
        self.fake, self.rec = FakeLinear(), self.record()

    def repair(self, gql=None, **kw):
        return run_comments.repair(
            ISSUE, ISSUE_ID, self.rec, tracker=LinearTracker(gql or self.fake), **kw
        )

    def test_a_late_start_write_is_repaired_with_a_higher_seq(self):
        late = Late(self.fake)
        self.addCleanup(late.release.set)
        self.assertEqual(start(late, self.rec, timeout=0.1), "timed out after 0.1s")
        self.assertTrue(late.held.wait(5))
        self.assertIsNone(end(late, self.rec, 0))
        self.assertEqual(self.fake.only()["body"], text(f"no outcome · #2 · {self.DONE}"))
        late.release.set()
        self.assertTrue(late.landed.wait(5))
        self.assertEqual(self.fake.only()["body"], text(f"running · #1 · {T1}"))
        self.fake.calls.clear()
        self.assertIsNone(self.repair(late))
        self.assertEqual(self.fake.calls, ["comment", "commentUpdate"])
        self.assertEqual(self.fake.only()["body"], text(f"no outcome · #3 · {self.DONE}"))

    def test_no_late_write_reads_and_does_not_rewrite(self):
        start(self.fake, self.rec)
        end(self.fake, self.rec, 0)
        self.fake.calls.clear()
        self.assertIsNone(self.repair())
        self.assertEqual(self.fake.calls, ["comment"])
        self.assertEqual(self.fake.only()["body"], text(f"no outcome · #2 · {self.DONE}"))

    def test_no_parseable_seq_rewrites(self):
        start(self.fake, self.rec)
        end(self.fake, self.rec, 0)
        self.fake.comments[CID]["body"] = f"Run {SID} · done · {self.DONE}"
        self.fake.calls.clear()
        self.assertIsNone(self.repair())
        self.assertEqual(self.fake.calls, ["comment", "commentUpdate"])
        self.assertEqual(self.fake.only()["body"], text(f"no outcome · #3 · {self.DONE}"))

    def test_not_found_creates_it_by_session_id(self):
        down = FakeLinear(fail={"commentCreate": RuntimeError("down")})
        self.assertEqual(end(down, self.rec, 0), "RuntimeError: down")
        self.assertIsNone(self.repair())
        self.assertEqual(self.fake.calls, ["comment", "commentUpdate", "commentCreate"])
        self.assertEqual(self.fake.only()["body"], text(f"no outcome · #2 · {self.DONE}"))

    def test_a_limit_end_is_rewritten_as_limit(self):
        down = FakeLinear(fail={"commentCreate": RuntimeError("down")})
        self.assertEqual(
            run_comments.post(
                ISSUE,
                self.rec,
                tracker=LinearTracker(down),
                rc=-9,
                ended_at=T2,
                limit=True,
            ),
            "RuntimeError: down",
        )
        self.assertIsNone(self.repair())
        self.assertEqual(self.fake.only()["body"], text(f"limit · #2 · {T1} → {T2} · exit -9"))

    def test_a_previous_runs_body_is_rewritten_on_a_resume(self):
        down = FakeLinear(fail={"commentUpdate": RuntimeError("down")})
        self.rec = self.record(started_at=T2)
        self.assertEqual(start(down, self.rec), "RuntimeError: down")
        self.assertEqual(end(down, self.rec, 0, ended_at=T3), "RuntimeError: down")
        self.fake = FakeLinear(comment(CID, text(f"done · #3 · {self.DONE}")))
        self.assertIsNone(self.repair())
        self.assertEqual(self.fake.calls, ["comment", "commentUpdate"])
        self.assertEqual(self.fake.only()["body"], text(f"no outcome · #3 · {T2} → {T3} · exit 0"))

    def test_errors_return_a_string_and_write_nothing(self):
        theirs = f"LinearError: comment {CID} is not ours"
        body = f"Run {SID} · running · #1 · {T1}"
        for name, seed, fail, reason in (
            ("find fails", (), {"comment": RuntimeError("boom")}, "RuntimeError: boom"),
            ("another issue", (comment(CID, body, issue=OTHER_ISSUE),), {}, theirs),
            ("another author", (comment(CID, body, mine=False),), {}, theirs),
        ):
            with self.subTest(name):
                self.rec = self.record()
                end(FakeLinear(), self.rec, 0)
                self.fake = FakeLinear(*seed, fail=fail)
                self.assertEqual(self.repair(), reason)
                self.assertEqual(self.fake.calls, ["comment"])

    def test_a_failed_rewrite_returns_its_error(self):
        end(FakeLinear(), self.rec, 0)
        self.fake.fail = {"commentCreate": RuntimeError("down")}
        self.assertEqual(self.repair(), "RuntimeError: down")

    def test_a_blocking_find_times_out(self):
        end(self.fake, self.rec, 0)
        release = threading.Event()
        self.addCleanup(release.set)
        t = time.monotonic()
        self.assertEqual(
            self.repair(lambda _q, **_v: release.wait(30), limit=0.1), "timed out after 0.1s"
        )
        self.assertLess(time.monotonic() - t, 5)
