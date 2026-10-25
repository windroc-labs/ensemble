import contextlib
import itertools
import os
import tempfile
from unittest import mock

from ensemble.session import SessionError
from ensemble.session.hosts.reclaimed import ReclaimedGroup
from ensemble.utils import ProcessControl

from session.tests.case import Case


class ReclaimTest(Case):
    def test_a_token_naming_no_controller_reclaims_nothing(self):
        with tempfile.TemporaryDirectory() as home:
            for token in (None, 5, "", "junk", home, os.path.join(home, "apm-group-none")):
                with self.subTest(token=token):
                    self.assertIsNone(ReclaimedGroup.reclaim(token))


class GroupTest(Case):
    def setUp(self):
        self.control = mock.create_autospec(ProcessControl, instance=True)
        self.control.path = "/tmp/apm-group-1"
        self.group = ReclaimedGroup(self.control)

    def test_it_is_named_and_tokened_by_its_controller_with_no_record_or_display(self):
        self.assertEqual(
            (self.group.name, self.group.token, self.group.record(), self.group.display()),
            ("apm-group-1", "/tmp/apm-group-1", None, None),
        )

    def test_alive_is_its_controllers_and_a_failure_to_tell_is_a_session_error(self):
        for live in (True, False):
            with self.subTest(live=live):
                self.control.live.return_value = live
                self.assertIs(self.group.alive(), live)
        self.control.live.side_effect = OSError("refused")
        with self.assertRaisesRegex(SessionError, "^process group apm-group-1: refused$"):
            self.group.alive()

    def test_close_terminates_a_group_that_then_exits_without_a_kill(self):
        self.control.live.return_value = False
        self.group.close()
        self.control.terminate.assert_called_once_with()
        self.control.kill.assert_not_called()

    def test_wait_exit_is_false_while_the_group_outlives_the_timeout(self):
        self.control.live.return_value = True
        self.assertFalse(self.group.wait_exit(0))

    def test_close_kills_a_group_that_outlives_its_terminate(self):
        self.control.live.side_effect = lambda: not self.control.kill.called
        with self.clock():
            self.group.close()
        self.control.terminate.assert_called_once_with()
        self.control.kill.assert_called_once_with()

    def test_kill_of_a_group_that_does_not_exit_is_a_session_error(self):
        self.control.live.return_value = True
        with (
            self.clock(),
            self.assertRaisesRegex(SessionError, "^process group apm-group-1: did not exit$"),
        ):
            self.group.kill()

    def test_a_controller_error_on_kill_or_close_is_a_session_error(self):
        for verb, call in (("kill", self.group.kill), ("terminate", self.group.close)):
            with self.subTest(verb=verb):
                getattr(self.control, verb).side_effect = OSError("refused")
                with self.assertRaisesRegex(SessionError, "^process group apm-group-1: refused$"):
                    call()

    def clock(self):
        """Each wait's deadline passes at once."""
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch("time.monotonic", side_effect=itertools.count(0, 10)))
        stack.enter_context(mock.patch("time.sleep"))
        return stack
