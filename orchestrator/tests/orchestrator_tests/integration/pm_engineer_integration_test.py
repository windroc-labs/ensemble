"""The pm to engineer flow over router.py --issue, the run's detached harness and its write-back, and
promote.py: a PRD issue becomes an ENG issue; a rework starts a new session; a failed run is not
promoted; a Linear outage relating the child is retried; a run without an outcome resumes its
session; an attended run (router.py --tui) that gives up is resumed headless by a router.py run
without --tui, its TUI closed first (main's test_b6); a lost transcript (resumable no) sends the
issue back to Todo for a new session.
"""

import os
import re
import unittest
from urllib.parse import unquote

from ensemble.orchestrator.engine import config
from ensemble.orchestrator.engine.flow import handoff
from ensemble.orchestrator.trackers.linear import ops
from orchestrator_tests.integration import fake_linear
from orchestrator_tests.integration.flow_fixtures import HARNESS_EMAIL
from orchestrator_tests.integration.segments import BUILD, REPORT, Segment

from delegate.tests.workdir import session_cwd

REWORK = "Add a section on stale sessions."

PRD = "# PRD: Session registry\n\nThe registry maps each tmux session to its run."


class PmToEngineer(Segment):
    def transcript_path(self, ident, sid):
        """The Claude client's transcript of sid run in ident's agent dir."""
        cwd = session_cwd(os.path.join(self.workdirs, ident))
        project = re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(cwd))
        return os.path.join(self.root, "claude-config", "projects", project, f"{sid}.jsonl")

    def test_b1_prd_to_an_engineer_issue(self):
        child = self.pm_to_engineer()
        [run] = self.runs()
        ident = run["issue"]
        [prd] = self.attachments(ident)
        titles = [self.issue(i)["title"] for i in (ident, child)]
        self.assertEqual(titles, [f"PRD: {prd['title']}", f"ENG: {prd['title']}"])
        self.assertIn(REPORT, self.section(self.prompt(ident, run["sid"]), "Reports"))

    def test_b2_rework_starts_a_new_session(self):
        ident = self.pm_ready()
        first = self.agent_script("pm", "done", ident)
        self.claim(ident, "pm", first)
        self.agent_run()
        self.human_say(ident, REWORK, "todo", "human: rework")
        url = first.report["url"]
        path = unquote(url.removeprefix(self.doc_url("")))
        self.publish(path, PRD)
        child = self.pm_done(ident, url)
        one, two = self.runs()
        self.assertEqual((one["kind"], two["kind"]), ("start", "start"))
        self.assertNotEqual(one["sid"], two["sid"])
        prompt = self.prompt(ident, two["sid"])
        self.assertIn(REWORK, self.section(prompt, "The user's later words"))
        self.assertIn(PRD, self.section(prompt, f"Earlier version: `{path}`"))
        description = self.issue(child)["description"]
        self.assertEqual(handoff.parse_handoff(description).instructions.partition(":\n")[2], BUILD)

    def test_b3_a_failed_run_is_not_promoted(self):
        ident = self.pm_ready()
        self.claim(ident, "pm", self.agent_script("pm", "failed", ident))
        self.agent_run()
        self.promote_nothing(ident)

    def test_b4_a_linear_outage_relating_the_child_is_retried(self):
        ident = self.pm_ready()
        self.claim(ident, "pm", self.agent_script("pm", "done", ident))
        self.agent_run()
        self.handoff(ident, BUILD)
        with self.step("promote: linear 503"):
            self.fake.fail("promote.M_RELATE", "503", account=HARNESS_EMAIL)
            events = len(self.logged())
            res = self.promote_now()
            self.assertEqual((res.returncode, res.stdout, res.stderr), (1, "", ""))
            self.assertEqual(
                [{k: v for k, v in e.items() if k != "ts"} for e in self.logged()[events:]],
                [
                    {
                        "src": "promote",
                        "kind": "linear-error",
                        "op": "issueRelationCreate",
                        "status": 503,
                    }
                ],
            )
            last = self.requests()[-1]
            self.assertEqual((last.op, last.result), ("promote.M_RELATE", "503"))
            with self.fake.lock:
                src = self.fake.find(ident)
                moves = [
                    (fake_linear.NAMES[h["toStateId"]], h["createdAt"]) for h in src["history"]
                ]
                cutoff = max(at for to, at in moves if to == "in_review")
                first = min(at for to, at in moves if to == "handoff" and at > cutoff)
                key = ops.client_id(src["id"], "engineer", first)
                made, relations = self.fake.find(key), list(self.fake.relations)
            self.assertIsNotNone(made, f"no issue {key}")
            self.assertEqual(relations, [])
            self.assertEqual([v["in"]["id"] for v in self.sent("promote.M_CREATE")], [key])
            child = made["identifier"]
            self.track(child, state="todo")
            self.expect_problem(("promote.M_RELATE", HARNESS_EMAIL, "503"))
        self.assertEqual(self.promote(ident, "pm", "engineer"), child)
        self.assertEqual([v["in"]["id"] for v in self.sent("promote.M_CREATE")], [key])

    def test_b5_a_run_without_an_outcome_is_resumed(self):
        ident = self.pm_ready()
        sid = self.claim(ident, "pm", self.agent_script("pm", "none", ident))
        self.agent_run()
        self.human_move(ident, "in_progress", "human: retry failed run")
        self.resume(ident, "pm", sid, self.agent_script("pm", "done", ident))
        self.agent_run()
        self.handoff(ident, BUILD)
        self.promote(ident, "pm", "engineer")
        self.assertEqual(
            [
                e["issue"]
                for e in self.logged()
                if (e["src"], e["kind"]) == ("writeback", "no-outcome")
            ],
            [ident],
        )

    def test_b6_an_attended_run_that_gives_up_is_resumed_headless(self):
        ident = self.pm_ready()
        sid = self.claim(ident, "pm", self.agent_script("pm", "none", ident), tui=True)
        self.agent_run()
        self.human_move(ident, "in_progress", "human: retry failed run")
        self.resume(ident, "pm", sid, self.agent_script("pm", "done", ident))
        self.assertFalse(self.live(config.tui_name("pm", ident, sid)))
        self.agent_run()
        self.handoff(ident, BUILD)
        self.promote(ident, "pm", "engineer")

    def test_b7_a_lost_transcript_starts_a_new_session(self):
        ident = self.pm_ready()
        sid = self.claim(ident, "pm", self.agent_script("pm", "none", ident))
        self.agent_run()
        self.human_move(ident, "in_progress", "human: retry failed run")
        with self.step("drop transcript"):
            os.remove(self.transcript_path(ident, sid))
        with self.step("router: interrupted"):
            self.assertEqual(
                [
                    (
                        e["src"],
                        e["kind"],
                        e.get("issue"),
                        e.get("to"),
                        e.get("reason"),
                        e.get("sid"),
                    )
                    for e in self.refused(ident)
                ],
                [("router", "recover", ident, "todo", "not resumable", sid)],
            )
            self.expect(
                ident,
                state="todo",
                history=[("in_progress", "todo", HARNESS_EMAIL)],
                comments=[(HARNESS_EMAIL, "interrupted")],
            )
        self.pm_done(ident)


if __name__ == "__main__":
    unittest.main()
