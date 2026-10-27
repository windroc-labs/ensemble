"""tests/session/fakes/host.py: FakeHost passes HostContract; its state dir, a dead name's reuse,
send's errors and the group killed until found empty.
"""

import os
import sys
import unittest
from unittest import mock

from ensemble.session import SessionError
from ensemble.session.engine.types import Key, Launch, Liveness, Paste
from ensemble.utils import StatusState
from tests.session.case import Case
from tests.session.contracts.host import HostContract
from tests.session.fakes.host import SLEEP, FakeHost


class FakeHostContract(HostContract, Case):
    def make_host(self):
        return FakeHost("fake")


class FakeHostOnly(Case):
    def setUp(self):
        self.host = FakeHost("fake", {"root": "~/hosts"})
        self.addCleanup(lambda: [self.host.process(n).close() for n in self.host.list()])

    def test_state_lives_under_the_root_setting_expanded(self):
        process = self.host.run("w1", Launch(SLEEP, cwd=self.home))
        self.assertEqual(
            sorted(os.listdir(os.path.join(self.home, "hosts", "w1"))),
            ["env.json", "meta.json", "output", "pid"],
        )
        process.close()
        self.assertFalse(os.path.exists(os.path.join(self.home, "hosts", "w1")))

    def test_a_dead_name_is_started_again(self):
        first = self.host.run("w1", Launch((sys.executable, "-I", "-c", "pass"), cwd=self.home))
        self.assertTrue(first.wait_exit(10))
        again = self.host.run_terminal("w1", Launch(SLEEP, cwd=self.home))
        self.assertTrue(again.alive())
        self.assertIsInstance(self.host.process("w1"), type(again))

    def test_send_refuses_an_unknown_key_and_an_ended_terminal(self):
        terminal = self.host.run_terminal("w1", Launch(SLEEP, cwd=self.home))
        with self.assertRaises(SessionError):
            terminal.send(Key("F13"))
        terminal.kill()
        for action in (Paste("x"), Key("Enter")):
            with self.subTest(action=action), self.assertRaises(SessionError):
                terminal.send(action)

    def test_a_retained_terminal_keeps_its_exit_status_and_generation_identity(self):
        first = self.host.run_terminal(
            "w1", Launch((sys.executable, "-I", "-c", "raise SystemExit(7)"), cwd=self.home)
        )
        self.assertTrue(first.wait_exit(10))
        self.assertEqual((first.state(), first.returncode()), (Liveness.LIVE, 7))
        self.assertEqual(self.host.process("w1").identity, first.identity)
        again = self.host.run_terminal("w1", Launch(SLEEP, cwd=self.home))
        self.assertNotEqual(again.identity, first.identity)
        self.assertEqual(first.state(), Liveness.ABSENT)

    def test_activity_marks_are_kept_and_written_to_the_status_log(self):
        terminal = self.host.run_terminal("w1", Launch(SLEEP, cwd=self.home))
        events = os.path.join(self.home, "events")
        self.assertIsNone(terminal.activity())
        terminal.mark(StatusState.BLOCKED, events)
        self.assertEqual(self.host.process("w1").activity(), StatusState.BLOCKED)
        with open(events, encoding="utf-8") as stream:
            self.assertRegex(stream.read(), r"^\d\d:\d\d:\d\d w1 blocked\n$")

    def test_the_group_is_killed_until_found_empty_its_id_free_for_reuse_after(self):
        terminal = self.host.run_terminal("w1", Launch(SLEEP, cwd=self.home))
        with mock.patch.object(os, "killpg", wraps=os.killpg) as killpg:
            terminal.kill()
            terminal.kill()
            terminal.close()
        killpg.assert_not_called()
        self.assertTrue(terminal.wait_exit(0))


if __name__ == "__main__":
    unittest.main()
