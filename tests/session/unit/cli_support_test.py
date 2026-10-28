from types import SimpleNamespace

from tests.session.case import Case
from tests.session.cli_support import CliCase


class EarlyExitTest(Case):
    def result(self, tail="", **extra):
        attach = "session w1: tmux attach -t '=w1'\n"
        head = "ensemble-session: w1: claude exited 0 at once"
        return SimpleNamespace(
            **{"returncode": 1, "stdout": "", "stderr": attach + head + tail + "\n", **extra}
        )

    def test_an_empty_screen_needs_no_diagnostic_suffix(self):
        self.assertEqual(CliCase.assert_early_exit(self, self.result(), "w1", 0), [])

    def test_a_screen_including_an_opening_command_is_preserved(self):
        result = self.result("; its last lines:\n/color\nPane is dead")
        self.assertEqual(
            CliCase.assert_early_exit(self, result, "w1", 0), ["/color", "Pane is dead"]
        )

    def test_wrong_status_record_or_exit_code_is_rejected(self):
        for extra, code in (({"returncode": 0}, 0), ({"stdout": "Record\n"}, 0), ({}, 2)):
            with self.subTest(extra=extra, code=code), self.assertRaises(AssertionError):
                CliCase.assert_early_exit(self, self.result(**extra), "w1", code)
