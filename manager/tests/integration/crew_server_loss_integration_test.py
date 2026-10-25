"""A lost tmux server on the real commands (manager/tests/live_pipeline.py): main's
test_a_lost_tmux_server_comes_back_by_attach_resume_but_a_closed_worker, for a worker, a role run
and a Linear pipeline, each back on its sid by its own crew resume (FR-22, FR-39).
"""

from ensemble.manager import State

from delegate.tests.replies import mark
from manager.tests.live_crew import MANAGER, has_pair, logged
from manager.tests.live_pipeline import LivePipeline

START = mark("start", "echoing")


class ServerLossTest(LivePipeline):
    def test_a_lost_tmux_server_comes_back_by_crew_resume_but_a_closed_worker(self):
        records = {name: self.record(self.worker(name)) for name in ("w1", "w2")}
        records["r"] = self.record(self.role("r", steps=[START]))
        records["p"] = self.pipeline("p", [START])
        for name in ("w1", "w2", records["r"].sessions[1], records["p"].sessions[1]):
            self.wait_event(name, "done")  # its transcript, which --resume needs
        self.ok(self.crew("close", "w2"))
        self.server.kill()
        self.server.start(MANAGER)
        self.server.attach(MANAGER)
        self.ok(self.crew("attach"))
        self.assertEqual(
            {name: e.state for name, e in self.roster().items()},
            {"w1": State.GONE, "r": State.GONE, "p": State.GONE},
        )
        for name, steps in (("w1", None), ("r", [START]), ("p", [START])):
            resumed = self.record(self.crew("resume", name, **self.scene(name, steps)))
            record = records[name]
            self.assertEqual((resumed.sid, resumed.sessions), (record.sid, record.sessions))
            self.until(lambda record=record: self.live(*record.sessions), f"{name} back")
            argv = logged(self.log(name))[-1]["argv"]
            self.assertTrue(has_pair(argv, "--resume", record.sid), argv)
        self.assertTrue(self.gone("w2"))
        self.ok(self.crew("attach"))
        self.assertEqual(
            {name: e.state for name, e in self.roster().items()},
            {"w1": State.LIVE, "r": State.LIVE, "p": State.LIVE},
        )
