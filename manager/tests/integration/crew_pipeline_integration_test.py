"""A Linear pipeline through ensemble-crew on the real commands (manager/tests/live_pipeline.py):
`crew run p -- ensemble-orchestrator router --issue ID --tui`, no --events, records its pipeline Record
and writes its turn ends and one outcome to the manager's events file (FR-18, FR-28); a crew killed
before the commit leaves an entry attach adopts and crew resume keeps (FR-28); a pipeline whose
sessions were killed comes back by crew resume on its sid at its display and writes back. Ports
main's tests/integration/orchestrator/pm_engineer_integration_test.py
test_b8_a_managed_attended_run_is_recovered_from_the_roster.
"""

from ensemble.manager import State
from orchestrator_tests.integration.flow_fixtures import UPDRAFT

from delegate.tests.replies import block, mark
from manager.tests.live_crew import MANAGER, has_pair, logged
from manager.tests.live_pipeline import CTL, ID, PR, RESUME, ROUTER, LivePipeline
from session.tests import live_tmux

START = mark("start", "Building the session registry.")
DONE = block(
    {
        "status": "done",
        "title": "Session registry",
        "summary": "Added it.",
        "url": PR,
        "updraft": UPDRAFT,
    }
)


class PipelineTest(LivePipeline):
    def test_a_pipeline_is_recorded_and_its_events_reach_the_managers_file(self):
        record = self.pipeline("p", [f"{START}\n{DONE}"])
        harness, tui = record.sessions
        self.wait_event(harness, "exited")
        self.assertEqual(self.words(tui), [["done"]])
        self.assertEqual(self.words(harness), [["outcome", "done"], ["exited", "0"]])
        self.until(lambda: self.state(ID) == "in_review", f"{ID} written back")
        self.until(lambda: self.gone(harness), f"{harness} gone")
        self.ok(self.crew("attach"))
        self.assertEqual(self.roster()["p"].state, State.DONE)

    def test_a_pipeline_whose_crew_died_before_the_commit_is_adopted_and_resumed(self):
        record = self.crashed("p", ROUTER, **self.scene("p", [START]))
        self.assertEqual((record.kind, record.ctl, record.resume), ("pipeline", CTL, RESUME))
        self.killed("p", record)
        resumed = self.record(self.crew("resume", "p", **self.scene("p", [START])))
        self.assertEqual((resumed.sid, resumed.ctl, resumed.resume), (record.sid, CTL, RESUME))
        entry = self.roster()["p"]
        self.assertEqual((entry.state, entry.record), (State.LIVE, resumed))

    def test_b8_a_killed_pipeline_comes_back_attended_at_its_display(self):
        record = self.pipeline("p", [START])
        harness, tui = record.sessions
        self.assertTrue(record.display.startswith(MANAGER), record.display)
        self.killed("p", record)
        self.assertEqual(self.state(ID), "in_progress")
        resumed = self.record(self.crew("resume", "p", **self.scene("p", [f"{START}\n{DONE}"])))
        self.assertEqual(resumed, record)
        self.until(
            lambda: live_tmux.assert_grid(self, self.server, MANAGER, [[tui]]),
            f"the grid [[{tui}]]",
        )
        self.assertTrue(has_pair(logged(self.log("p"))[-1]["argv"], "--resume", record.sid))
        self.until(lambda: ["outcome", "done"] in self.words(harness), "the resumed outcome")
        self.until(lambda: self.state(ID) == "in_review", f"{ID} written back")
        self.until(lambda: self.gone(harness), f"{harness} gone")
        self.ok(self.crew("attach"))
        entry = self.roster()["p"]
        self.assertEqual((entry.state, entry.record), (State.DONE, record))
