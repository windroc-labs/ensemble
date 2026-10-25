"""The researcher to pm flow over router.py --issue, the run's detached harness and its write-back, and
promote.py: a research issue becomes a PM issue; a task label picks the task; a blocked issue waits
for its blocker; an answer starts a new session; a failed run is not promoted; four failed runs cap
the issue.
"""

import unittest

from ensemble.orchestrator.engine import router as routing
from orchestrator_tests.integration.flow_fixtures import (
    HARNESS_EMAIL,
    HUMAN,
    LIGHT_RESEARCH,
    RESEARCH_DIR,
)
from orchestrator_tests.integration.segments import RESEARCH, Segment

BLOCKER = "Pick a session store"

ANSWER = "Only harnesses that run agents in tmux."

EARLIER = "# Session registries (draft)\n\nTmux names each session."


class ResearcherToPm(Segment):
    def test_a1_research_to_a_pm_issue(self):
        self.assert_pm_start(self.researcher_to_pm())

    def test_a2_light_research(self):
        self.researcher_to_pm(labels=[LIGHT_RESEARCH], task="light-research")

    def test_a3_blocked_until_its_blocker_is_done(self):
        with self.step("seed: backlog"):
            ident = self.backlog_issue("researcher", *RESEARCH)
            blocker = self.fake.issue(BLOCKER)["identifier"]
            self.fake.relate("blocks", blocker, ident)
            self.track(blocker)
        self.backlog_not_claimed(ident)
        self.human_move(ident, "todo", "human: to todo")
        with self.step("router: blocked"):
            self.assertEqual(
                [(e["src"], e["kind"], e.get("issue"), e.get("by")) for e in self.refused(ident)],
                [("router", "blocked", ident, [blocker])],
            )
        self.human_move(blocker, "done", "human: blocker done")
        self.researcher_done(ident)

    def test_a4_an_answer_starts_a_new_session(self):
        ident = self.researcher_ready()
        self.claim(ident, "researcher", self.agent_script("researcher", "needs_input", ident))
        self.agent_run()
        self.human_say(ident, ANSWER, "todo", "human: answer")
        path = f"{RESEARCH_DIR}2026-10-01-{ident}-draft.md"
        self.publish(path, EARLIER)
        self.researcher_done(ident)
        first, second = self.runs()
        self.assertEqual((first["kind"], second["kind"]), ("start", "start"))
        self.assertNotEqual(first["sid"], second["sid"])
        prompt = self.prompt(ident, second["sid"])
        self.assertIn(ANSWER, self.section(prompt, "The user's comments"))
        self.assertIn(EARLIER, self.section(prompt, f"Earlier version: `{path}`"))

    def test_a5_a_failed_run_is_not_promoted(self):
        ident = self.researcher_ready()
        self.claim(ident, "researcher", self.agent_script("researcher", "failed", ident))
        self.agent_run()
        self.promote_nothing(ident)

    def test_a6_capped_after_four_failed_runs(self):
        ident = self.researcher_ready()
        self.wait_next_second(ident)
        for _ in range(routing.CAP):
            self.claim(ident, "researcher", self.agent_script("researcher", "failed", ident))
            self.agent_run()
        with self.step("router: cap"):
            attempts = self.attempts(ident)
            self.assertEqual(
                [
                    (e["src"], e["kind"], e.get("issue"), e.get("to"), e.get("reason"))
                    for e in self.refused(ident, attempts)
                ],
                [
                    ("router", "claim-skip", ident, "in_review", f"reached {routing.CAP} attempts"),
                    ("router", "pick-none", None, None, "nothing claimable"),
                ],
                attempts,
            )
            self.expect(
                ident,
                state="in_review",
                history=[("todo", "in_review", HARNESS_EMAIL)],
                comments=[(HARNESS_EMAIL, "cap")],
                subscribers=[HUMAN],
            )
        self.assertEqual(len(self.runs()), routing.CAP)


if __name__ == "__main__":
    unittest.main()
