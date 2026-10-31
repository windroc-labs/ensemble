"""A claimed run launched by router.outer (entry.py launch), headless: its harness on a private tmux
server, fake_claude as client `claude`, each event written back by `writeback` over FakeLinear's
HTTP. Done runs and prune; a run with no outcome; a usage limit and its resume; a resume after a
lost ledger write.
"""

import os
import re
import shlex
import unittest
from datetime import UTC, datetime, timedelta

from tests.delegate.replies import block, mark, option
from tests.delegate.workdir import checkout_path, session_cwd
from tests.orchestrator.integration.flow_fixtures import (
    ENTRY,
    HUMAN,
    LAUNCH,
    PROJECT,
    UPDRAFT,
    TmuxFlow,
    steps,
)
from tests.orchestrator.unit.board_ids import HARNESS_USER, STATES, comment_id, role_id
from tests.orchestrator.unit.run_fixtures import ID, SESSION, SID, UUID
from tests.orchestrator.unit.writeback_fixtures import APPROVE_MAPPED, DOC, PR, PRD

T = datetime(2026, 10, 11, 10, 0, tzinfo=UTC).timestamp()
RESETS = "2026-10-11T10:00:00+00:00"  # T in the runs' TZ
LIMIT = {"kind": "limit", "resets_at": T}
PICK = "picked the task."
RUNNING = r"running · #1 · \S+"
DONE = r"done · #2 · \S+ → \S+ · exit 0"
UNDONE = r"no outcome · #2 · \S+ → \S+ · exit 0"
LIMITED = r"limit · #2 · \S+ → \S+ · exit -1"  # the Session's guard hangs up a limit-hit session
KINDS = ["session", "input", "client_session", "progress", "result", "end"]
RESEARCH = {
    "status": "done",
    "title": "Queue options",
    "summary": "Three queues compared.\nRedis streams fit best.",
    "url": DOC,
}
READ = ("writeback.Q_ATTACHED", {"i": UUID})
SUBSCRIBE = ("linear.M_SUBSCRIBE", {"i": UUID, "e": HUMAN.lower()})
REREAD = ("linear.Q_ISSUE_STATE", {"i": UUID})
TO_REVIEW = ("linear.M_STATE", {"i": UUID, "s": STATES["in_review"]})


def cid(step, role="researcher"):
    return comment_id(role, SID, step)


def post(step, body, role="researcher"):
    return ("linear.M_COMMENT_ID", {"c": cid(step, role), "i": UUID, "b": body})


def find(step):
    return ("linear.Q_COMMENT", {"c": cid(step)})


def looked_up(op, caller, v):
    """A role's call as made: a comment post is first looked up by its client id, not found."""
    made = (op, caller, v, "ok")
    if op != "linear.M_COMMENT_ID":
        return [made]
    return [("linear.Q_COMMENT", caller, {"c": v["c"]}, "error"), made]


def attach(url, title):
    return ("writeback.M_ATTACH", {"i": UUID, "u": url, "t": title})


class Run(TmuxFlow):
    def setUp(self):
        super().setUp()
        self.rd = os.path.join(self.workdirs, ID)
        self.ends = 0

    def seed(self, role):
        self.role = role
        self.fake.issue(
            "Session registry",
            identifier=ID,
            id=UUID,
            state="in_progress",
            delegate=role_id(role),
            project=PROJECT,
        )

    def launch(self, steps, mode="new", **scenario):
        """The run launched and its harness ended, each event written back."""
        self.scene(steps, **scenario)
        res = self.script(
            *LAUNCH,
            *("--uuid", UUID, "--issue", ID, "--project", PROJECT["id"], "--sid", SID),
            *("--role", self.role, "--client", "claude", "--mode", mode),
        )
        self.assertEqual((res.returncode, res.stdout), (0, ""), (res.stderr, self.said()))
        self.ends += 1
        self.wait_end(SID, ID, self.ends, self.role)

    def wrote(self, since=0):
        """The write-back's requests since: all but the launch's issue read and related search."""
        return [
            (r.op, r.caller, r.variables, r.result)
            for r in self.requests()[since:]
            if r.op != "issues.Q_ISSUE" and not str(r.op).startswith("related.")
        ]

    def session(self, *lines, since=0):
        """The session comment's bodies written since, each `Run <SID> · <line>` and the resume
        command.
        """
        bodies = []
        for _, _, v, _ in self.wrote(since):
            if v.get("c") == SESSION and "b" in v and v["b"] not in bodies[-1:]:
                bodies.append(v["b"])
        cwd = re.escape(shlex.quote(session_cwd(self.rd)))
        command = (
            rf"\n\n```\ncd {cwd} && ensemble-session client-resume claude \S+ {cwd} {SID}\n```"
        )
        self.assertEqual(len(bodies), len(lines), bodies)
        for body, line in zip(bodies, lines, strict=True):
            self.assertRegex(body, rf"\ARun {SID} · {line}{command}\Z")
        return bodies

    def framed(self, *calls, end=DONE, created=True, since=0, reactivate=False):
        """A run's Linear calls: the session comment's first write (created: not found, then created
        by its id), `calls` as the role's app, the session comment's end and repair.
        """
        running, ended = self.session(RUNNING, end, since=since)
        update = ("sessions.M_UPDATE", HARNESS_USER, {"c": SESSION, "b": running})
        create = ("linear.M_COMMENT_ID", HARNESS_USER, {"c": SESSION, "i": ID, "b": running})
        return [
            *([(*update, "error"), (*create, "ok")] if created else [(*update, "ok")]),
            (REREAD[0], role_id(self.role), REREAD[1], "ok"),
            *(
                [
                    (
                        "linear.M_STATE",
                        role_id(self.role),
                        {"i": UUID, "s": STATES["in_progress"]},
                        "ok",
                    )
                ]
                if reactivate
                else []
            ),
            *(made for op, v in calls for made in looked_up(op, role_id(self.role), v)),
            ("sessions.M_UPDATE", HARNESS_USER, {"c": SESSION, "b": ended}, "ok"),
            ("linear.Q_COMMENT", HARNESS_USER, {"c": SESSION}, "ok"),
        ]

    def stored(self):
        return [(self.who(c["user"]), c["id"]) for c in self.issue(ID)["comments"]]


class Done(Run):
    def done(self, role, start, outcome, calls, ledger, *, kinds=KINDS):
        ledger = ["running:1", *ledger]
        self.seed(role)
        self.launch([mark("start", PICK), block(outcome)])
        self.assertEqual(self.wrote(), self.framed(post("start", f"{start} {PICK}", role), *calls))
        self.assertEqual(self.ledger(ID), {SID: ledger})
        self.assertEqual(
            [line for line in self.said() if line.startswith("writeback ")], steps(*ledger)
        )
        self.assertEqual([e["kind"] for e in self.records(ID)], kinds)
        self.assertEqual(self.records(ID)[-2]["outcome"]["status"], "done")
        self.assertEqual([option(c, "--session-id") for c in self.calls()], [SID])
        app = role_id(role)
        self.assertEqual(
            self.stored(),
            [
                (HARNESS_USER, SESSION),
                (app, cid("start", role)),
                (app, cid("comment", role)),
            ],
        )
        self.assertEqual(self.state(ID), "in_review")
        self.assertEqual(self.issue(ID)["subscribers"], [HUMAN])
        self.assertEqual(
            self.issue(ID)["attachments"], [{"url": outcome["url"], "title": outcome["title"]}]
        )

    def test_a_researcher_run_then_prune_deletes_its_agent_files_and_keeps_its_records(self):
        self.done(
            "researcher",
            "Research started:",
            RESEARCH,
            [
                READ,
                SUBSCRIBE,
                post("comment", f"Three queues compared.\nRedis streams fit best.\n\n{DOC}"),
                attach(DOC, "Queue options"),
                REREAD,
                TO_REVIEW,
            ],
            ["start", "comment", f"attach:{DOC}", "move:in_review", "outcome"],
        )
        os.makedirs(checkout_path(self.rd, "o", "n", ID))
        agent = session_cwd(self.rd)

        def kept():
            found = {}
            for top, dirs, names in os.walk(self.rd):
                dirs[:] = [d for d in dirs if os.path.join(top, d) != agent]
                for name in names:
                    with open(os.path.join(top, name), "rb") as f:
                        found[os.path.relpath(os.path.join(top, name), self.rd)] = f.read()
            return found

        before, ledger = kept(), self.ledger(ID)
        self.assertIn("writeback.json", before)
        done = datetime.now(UTC) - timedelta(hours=30)
        self.fake.move(ID, "done", actor=self.ids[HUMAN], at=done)
        res = self.script(ENTRY, "prune")
        self.assertEqual(res.returncode, 0, (res.stderr, self.said()))
        self.assertIn(f"prune prune-removed {ID} what=agent dir", self.said())
        self.assertFalse(os.path.lexists(agent))
        self.assertEqual((kept(), self.ledger(ID)), (before, ledger))

    def test_a_pm_run(self):
        self.done(
            "pm",
            "PRD started:",
            {
                "status": "done",
                "title": "Session Registry",
                "summary": "PRD for the registry.",
                "url": PRD,
            },
            [
                ("sessions.Q_STATE_NAME", {"i": STATES["handoff"]}),
                READ,
                ("writeback.M_TITLE", {"i": UUID, "t": "PRD: Session Registry"}),
                SUBSCRIBE,
                post("comment", f"PRD for the registry.\n\n{PRD}\n\n{APPROVE_MAPPED}", "pm"),
                attach(PRD, "Session Registry"),
                REREAD,
                TO_REVIEW,
            ],
            ["start", "retitle", "comment", f"attach:{PRD}", "move:in_review", "outcome"],
        )
        self.assertEqual(self.issue(ID)["title"], "PRD: Session Registry")

    def test_an_engineer_run(self):
        title = f"{ID}: Session registry"
        self.done(
            "engineer",
            "Build started:",
            {
                "status": "done",
                "title": title,
                "summary": "Adds the registry.\nVerify: python3 -m unittest",
                "url": PR,
                "updraft": UPDRAFT,
            },
            [
                READ,
                SUBSCRIBE,
                post(
                    "comment",
                    "Build ready:\nAgent report before delivery:\n\n> Adds the registry.\n> Verify: python3 -m unittest",
                    "engineer",
                ),
                attach(PR, title),
                REREAD,
                TO_REVIEW,
            ],
            ["start", "comment", f"attach:{PR}", "move:in_review", "outcome"],
            kinds=[*KINDS[:-2], "ready_gate", *KINDS[-2:]],
        )


class Undone(Run):
    def test_a_run_without_an_outcome_reports_failure_and_moves_to_review(self):
        self.seed("engineer")
        self.launch([mark("start", PICK), "Working."])
        self.assertEqual(
            self.wrote(),
            self.framed(
                post("start", f"Build started: {PICK}", "engineer"),
                READ,
                SUBSCRIBE,
                post(
                    "no-outcome:1:comment",
                    "Build failed: the agent run returned no outcome",
                    "engineer",
                ),
                REREAD,
                TO_REVIEW,
                end=UNDONE,
            ),
        )
        self.assertEqual(
            self.said(),
            steps(
                "running:1",
                "start",
                "no-outcome:1:comment",
                "no-outcome:1:move:in_review",
                "no-outcome:1:outcome",
                more=[
                    f"writeback no-outcome {ID} sid={SID} error=the agent run returned no outcome"
                ],
            ),
        )
        self.assertEqual(
            (self.state(ID), self.ledger(ID)),
            (
                "in_review",
                {
                    SID: [
                        "running:1",
                        "start",
                        "no-outcome:1:comment",
                        "no-outcome:1:move:in_review",
                        "no-outcome:1:outcome",
                    ]
                },
            ),
        )

    def test_a_usage_limit_moves_to_in_review_once_per_sid(self):
        self.seed("researcher")
        self.launch([mark("start", PICK), LIMIT], hang=True)
        self.assertEqual(
            self.wrote(),
            self.framed(
                post("start", f"Research started: {PICK}"),
                SUBSCRIBE,
                post("limit", f"Usage limit, resets at {RESETS}."),
                REREAD,
                TO_REVIEW,
                end=LIMITED,
            ),
        )
        self.assertEqual(self.said(), steps("running:1", "start", "limit", "limit-move:1", end=-1))
        self.assertEqual(self.records(ID)[-3]["kind"], "limit")
        start = {
            "kind": "start",
            "issue": ID,
            "sid": SID,
            "role": "researcher",
            "client": "claude",
            "delegate_kind": "harness",
            "uuid": UUID,
            "project": PROJECT["id"],
            "target": None,
            "task": None,
        }
        self.assertEqual(self.runs(), [start])
        self.assertEqual(self.state(ID), "in_review")

        n = len(self.requests())
        self.launch([], mode="resume", resume=[LIMIT], hang=True)
        self.assertEqual(
            self.wrote(n),
            self.framed(
                SUBSCRIBE, REREAD, TO_REVIEW, end=LIMITED, created=False, since=n, reactivate=True
            ),
        )
        resumed = {k: start[k] for k in ("issue", "sid", "role", "client", "delegate_kind")}
        self.assertEqual(self.runs(), [start, {"kind": "resume", **resumed}])
        self.assertEqual([s for _, s in self.stored()].count(cid("limit")), 1)
        researcher = role_id("researcher")
        self.assertEqual(
            self.history(ID),
            [
                ("in_progress", "in_review", researcher),
                ("in_review", "in_progress", researcher),
                ("in_progress", "in_review", researcher),
            ],
        )

    def test_a_resume_after_a_lost_ledger_write_reposts_no_start_and_posts_its_own_result(self):
        self.seed("researcher")
        script = [mark("start", PICK), block(RESEARCH)]
        self.launch(script)
        os.remove(os.path.join(self.rd, "writeback.json"))
        n, said = len(self.requests()), len(self.said())
        self.launch(script, mode="resume", resume=script)
        result = f"Three queues compared.\nRedis streams fit best.\n\n{DOC}"
        self.assertEqual(
            self.wrote(n),
            self.framed(
                find("start"),
                READ,
                SUBSCRIBE,
                post("attempt:2:comment", result),
                REREAD,
                TO_REVIEW,
                created=False,
                since=n,
                reactivate=True,
            ),
        )
        ledger = [
            "running:2",
            "start",
            "attempt:2:comment",
            f"attach:{DOC}",
            "attempt:2:move:in_review",
            "attempt:2:outcome",
        ]
        self.assertEqual(self.said()[said:], steps(*ledger, found=("start",)))
        self.assertEqual(self.ledger(ID), {SID: ledger})
        self.assertEqual([option(c, "--resume") for c in self.calls()], [None, SID])
        app = role_id("researcher")
        self.assertEqual(
            self.stored(),
            [
                (HARNESS_USER, SESSION),
                (app, cid("start")),
                (app, cid("comment")),
                (app, cid("attempt:2:comment")),
            ],
        )


if __name__ == "__main__":
    unittest.main()
