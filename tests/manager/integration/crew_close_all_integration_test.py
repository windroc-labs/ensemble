"""close-all on the real commands (FR-35; tests/manager/live_crew.py): main's
test_close_all_closes_what_mgr_opened_and_only_that with crew run. What mgr opened, what a manager
it opened opened, and a matching orphan close; another manager's work stays.
"""

from tests.delegate.replies import mark
from tests.manager.live_crew import OTHER, LiveCrew
from tests.session import live_tmux

START = mark("start", "echoing")


class CloseAllTest(LiveCrew):
    def test_close_all_closes_what_mgr_opened_and_only_that(self):
        for name in ("w1", "w2"):
            self.record(self.worker(name))
        role = self.record(self.role("r", steps=[START]))
        self.record(self.worker("w3", inside="w1", manager="w1"))
        self.orphan("o2", "mgr")
        self.ok(self.server.tmux("new-session", "-d", "-s", OTHER, *live_tmux.IDLE))
        self.server.attach(OTHER)
        self.orphan("o1", OTHER)
        sessions = ("w1", "w2", "w3", "o2", *role.sessions)
        entries = self.roster()
        res = self.ok(self.crew("close-all", "--dry-run"))
        self.assertEqual(res.stdout.splitlines(), sorted(("o2", "r", role.name, "w1", "w2", "w3")))
        self.assertTrue(self.live(*sessions, "o1"))
        self.assertEqual(self.roster(), entries)
        res = self.ok(self.crew("close-all"))
        self.assertEqual(res.stdout.splitlines(), sorted(("o2", "r", role.name, "w1", "w2", "w3")))
        self.until(lambda: self.gone(*sessions), f"{sessions} gone")
        self.assertTrue(self.live("mgr", OTHER, "o1"))
        self.assertEqual(self.roster(), {})
        self.assertEqual(self.ok(self.crew("close-all")).stdout, "nothing to close\n")
