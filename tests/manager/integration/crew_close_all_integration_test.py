"""close-all on the real commands (tests/manager/live_crew.py): main's
test_close_all_closes_what_mgr_opened_and_only_that with crew run. What mgr opened, what a manager
it opened opened, and a matching orphan close; another manager's work stays. A worker whose
display was killed is still closed.
"""

from tests.delegate.replies import mark
from tests.manager.live_crew import OTHER, LiveCrew, Owner
from tests.session import live_tmux

START = mark("start", "echoing")


class CloseAllTest(LiveCrew):
    def test_close_all_closes_what_mgr_opened_and_only_that(self):
        for name in ("w1", "w2"):
            self.record(self.worker(name))
        role = self.record(self.role("r", steps=[START]))
        # from a shell in w1's worker pane, w1 its manager
        w1 = live_tmux.wait(lambda: self.server.pane_of("w1"), what="w1's worker pane")
        self.record(self.worker("w3", TMUX_PANE=w1, manager="w1"))
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
        self.until(
            lambda: self.gone(*sessions) and not self.drive_running(role),
            f"{sessions} and {role.name}'s drive gone",
        )
        self.assertTrue(self.live("mgr", OTHER, "o1"))
        self.assertEqual(self.roster(), {})
        self.assertEqual(self.ok(self.crew("close-all")).stdout, "nothing to close\n")

    def test_close_all_closes_a_worker_whose_display_was_killed(self):
        self.record(self.worker("w1"))
        owner = Owner(self.ensemble, "w1")
        pane = live_tmux.wait(lambda: self.server.pane_of("w1"), what="w1's worker pane")
        self.ok(self.server.tmux("kill-pane", "-t", pane))
        self.assertTrue(owner.alive())
        self.assertEqual(self.ok(self.crew("close-all")).stdout.splitlines(), ["w1"])
        self.until(lambda: not owner.alive(), "w1's harness gone")
        self.assertEqual(self.roster(), {})
