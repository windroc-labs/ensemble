"""A claimed run launched by router.outer (entry.py launch), headless: its harness on a private tmux
server, fake_claude as client `claude`, each event written back by ensemble-orchestrator writeback over
FakeLinear's HTTP. Done runs write back as writeback_test pins them, plus the session comment by
its session id; a run with no outcome; a usage limit (FR-59) and its resume; a resume after a lost
ledger write (FR-54).
"""

import os
import re
import shlex
import unittest
from datetime import UTC, datetime, timedelta

from ensemble.orchestrator.trackers.linear import ops
from orchestrator_tests.integration.flow_fixtures import (
    ACCOUNTS,
    ENTRY,
    HARNESS_EMAIL,
    HUMAN,
    KEYS,
    LAUNCH,
    PROJECT,
    UPDRAFT,
    TmuxFlow,
    steps,
)
from orchestrator_tests.unit.board_ids import STATES
from orchestrator_tests.unit.run_fixtures import ID, SESSION, SID, UUID
from orchestrator_tests.unit.writeback_fixtures import APPROVE_MAPPED, DOC, PR, PRD

from delegate.tests.replies import block, mark, option
from delegate.tests.workdir import checkout_path, session_cwd

T = datetime(2026, 10, 11, 10, 0, tzinfo=UTC).timestamp()
RESETS = "2026-10-11T10:00:00+00:00"  # T in the runs' TZ
LIMIT = {"kind": "limit", "resets_at": T}
PICK = "picked the task."
RUNNING = r"running · #1 · \S+"
DONE = r"done · #2 · \S+ → \S+ · exit 0"
UNDONE = r"no outcome · #2 · \S+ → \S+ · exit 0"
LIMITED = r"limit · #2 · \S+ → \S+ · exit -9"  # the Session kills a limit-hit session
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


def cid(step):
    return ops.client_id(SID, step)


def post(step, body):
    return ("linear.M_COMMENT_ID", {"c": cid(step), "i": UUID, "b": body})


def find(step):
    return ("linear.Q_COMMENT", {"c": cid(step)})


def looked_up(op, account, v):
    """A role's call as made: a comment post is first looked up by its client id, not found."""
    made = (op, account, v, "ok")
    if op != "linear.M_COMMENT_ID":
        return [made]
    return [("linear.Q_COMMENT", account, {"c": v["c"]}, "error"), made]


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
            assignee=self.ids[ACCOUNTS[role]],
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
            (r.op, r.account, r.variables, r.result)
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
        by its id), `calls` as the role's account, the session comment's end and repair.
        """
        running, ended = self.session(RUNNING, end, since=since)
        update = ("sessions.M_UPDATE", HARNESS_EMAIL, {"c": SESSION, "b": running})
        create = ("linear.M_COMMENT_ID", HARNESS_EMAIL, {"c": SESSION, "i": ID, "b": running})
        return [
            *([(*update, "error"), (*create, "ok")] if created else [(*update, "ok")]),
            (REREAD[0], ACCOUNTS[self.role], REREAD[1], "ok"),
            *(
                [
                    (
                        "linear.M_STATE",
                        ACCOUNTS[self.role],
                        {"i": UUID, "s": STATES["in_progress"]},
                        "ok",
                    )
                ]
                if reactivate
                else []
            ),
            *(made for op, v in calls for made in looked_up(op, ACCOUNTS[self.role], v)),
            ("sessions.M_UPDATE", HARNESS_EMAIL, {"c": SESSION, "b": ended}, "ok"),
            ("linear.Q_COMMENT", HARNESS_EMAIL, {"c": SESSION}, "ok"),
        ]

    def stored(self):
        return [(self.email(c["user"]), c["id"]) for c in self.issue(ID)["comments"]]


class Done(Run):
    def done(self, role, start, outcome, calls, ledger, *, kinds=KINDS):
        ledger = ["running:1", *ledger]
        self.seed(role)
        self.launch([mark("start", PICK), block(outcome)])
        self.assertEqual(self.wrote(), self.framed(post("start", f"{start} {PICK}"), *calls))
        self.assertEqual(self.ledger(ID), {SID: ledger})
        self.assertEqual(
            [line for line in self.said() if line.startswith("writeback ")], steps(*ledger)
        )
        self.assertEqual([e["kind"] for e in self.records(ID)], kinds)
        self.assertEqual(self.records(ID)[-2]["outcome"]["status"], "done")
        self.assertEqual([option(c, "--session-id") for c in self.calls()], [SID])
        account = ACCOUNTS[role]
        self.assertEqual(
            self.stored(),
            [(HARNESS_EMAIL, SESSION), (account, cid("start")), (account, cid("comment"))],
        )
        self.assertEqual(self.state(ID), "in_review")
        self.assertEqual(self.issue(ID)["subscribers"], [HUMAN])
        self.assertEqual(
            self.issue(ID)["attachments"], [{"url": outcome["url"], "title": outcome["title"]}]
        )

    def test_prune_deletes_a_finished_runs_agent_files_and_keeps_its_records(self):
        self.seed("researcher")
        self.launch([mark("start", PICK), block(RESEARCH)])
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

    def test_a_researcher_run(self):
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
                post("comment", f"PRD for the registry.\n\n{PRD}\n\n{APPROVE_MAPPED}"),
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
                post("start", f"Build started: {PICK}"),
                READ,
                SUBSCRIBE,
                post("no-outcome:1:comment", "Build failed: the agent run returned no outcome"),
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
        self.assertEqual(
            self.said(), steps("running:1", "start", "limit", "limit-move:1", "limit-row:1", end=-9)
        )
        self.assertEqual(self.records(ID)[-3]["kind"], "limit")
        note = {
            "kind": "limit",
            "issue": ID,
            "sid": SID,
            "client": "claude",
            "resets_at": RESETS,
            "delegate_kind": "harness",
            "attempt": 1,
        }
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
            "key": KEYS["researcher"],
            "task": None,
        }
        self.assertEqual(self.runs(), [start, note])
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
        again = {**note, "attempt": 2}
        self.assertEqual(self.runs(), [start, note, {"kind": "resume", **resumed}, again])
        self.assertEqual([s for _, s in self.stored()].count(cid("limit")), 1)
        researcher = ACCOUNTS["researcher"]
        self.assertEqual(
            self.history(ID),
            [
                ("in_progress", "in_review", researcher),
                ("in_review", "in_progress", researcher),
                ("in_progress", "in_review", researcher),
            ],
        )

    def test_a_resume_after_a_lost_ledger_write_posts_no_second_comment(self):
        self.seed("researcher")
        script = [mark("start", PICK), block(RESEARCH)]
        self.launch(script)
        ledger = self.ledger(ID)
        os.remove(os.path.join(self.rd, "writeback.json"))
        n, said = len(self.requests()), len(self.said())
        self.launch(script, mode="resume", resume=script)
        ledger[SID][0] = "running:2"
        self.assertEqual(
            self.wrote(n),
            self.framed(
                find("start"),
                READ,
                SUBSCRIBE,
                find("comment"),
                REREAD,
                TO_REVIEW,
                created=False,
                since=n,
                reactivate=True,
            ),
        )
        self.assertEqual(self.said()[said:], steps(*ledger[SID], found=("start", "comment")))
        self.assertEqual(self.ledger(ID), ledger)
        self.assertEqual([option(c, "--resume") for c in self.calls()], [None, SID])
        account = ACCOUNTS["researcher"]
        self.assertEqual(
            self.stored(),
            [(HARNESS_EMAIL, SESSION), (account, cid("start")), (account, cid("comment"))],
        )


if __name__ == "__main__":
    unittest.main()
