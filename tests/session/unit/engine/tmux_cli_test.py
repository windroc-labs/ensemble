"""TmuxCli against a recorded tmux: run and checked, has_session, and display_error."""

from ensemble.session.engine import tmux_cli
from tests.session.case import Case
from tests.session.tmux_fixtures import ABSENT, TMUX_KW, FakeTmux, done


class DisplayErrorTest(Case):
    def test_none_or_printable_text_not_ending_in_a_semicolon(self):
        for value in (None, "", "mgr", "mgr/right/m1", "elsewhere:1.2", "a b#{x}", "\u00e9"):
            with self.subTest(value=value):
                self.assertIsNone(tmux_cli.display_error(value))
        for value in ("a\tb", "a\nb", "a\0b", "a\u2028b", "mgr;", 5, b"x"):
            with self.subTest(value=value):
                self.assertEqual(
                    tmux_cli.display_error(value),
                    f"display {value!r:.80}: want printable text not ending in ;",
                )


class RunTest(Case):
    def test_run_prefixes_tmux_and_feeds_stdin_or_devnull(self):
        fake = FakeTmux()
        tmux_cli.TmuxCli(fake).run(["list-sessions"])
        tmux_cli.TmuxCli(fake).run(["load-buffer", "-"], "text")
        self.assertEqual(fake.calls, [["tmux", "list-sessions"], ["tmux", "load-buffer", "-"]])
        self.assertEqual(
            fake.kwargs, [TMUX_KW, {"capture_output": True, "text": True, "input": "text"}]
        )

    def test_checked_raises_tmuxs_error_and_run_an_os_error(self):
        fake = FakeTmux(results={"kill-pane": (1, "", "  boom \n")})
        self.assertEqual(tmux_cli.TmuxCli(fake).run(["kill-pane"]).returncode, 1)
        with self.assertRaisesRegex(tmux_cli.TuiError, "^tmux: boom$"):
            tmux_cli.TmuxCli(fake).checked(["kill-pane"])
        missing = FakeTmux(results={"list-sessions": FileNotFoundError(2, "No such file", "tmux")})
        for call in (tmux_cli.TmuxCli(missing).run, tmux_cli.TmuxCli(missing).checked):
            with self.subTest(call=call), self.assertRaisesRegex(tmux_cli.TuiError, "^tmux: "):
                call(["list-sessions"])

    def test_absent_is_a_missing_session_window_pane_or_server(self):
        for err in ABSENT:
            with self.subTest(err=err):
                self.assertTrue(tmux_cli.absent(done([], 1, err=err)))
        for err in ("boom", "", "error connecting to /tmp/x (Permission denied)"):
            with self.subTest(err=err):
                self.assertFalse(tmux_cli.absent(done([], 1, err=err)))


class HasSessionTest(Case):
    def test_true_false_without_it_or_a_server_and_a_failure_raises(self):
        for result, want in (
            ((0,), True),
            ((1, "", "can't find session: w1\n"), False),
            ((1, "", "no server running on /tmp/tmux-501/default\n"), False),
        ):
            with self.subTest(result=result):
                fake = FakeTmux(results={"has-session": result})
                self.assertIs(tmux_cli.TmuxCli(fake).has_session("w1"), want)
                self.assertEqual(fake.calls, [["tmux", "has-session", "-t", "=w1"]])
        fake = FakeTmux(results={"has-session": (1, "", "boom\n")})
        with self.assertRaisesRegex(tmux_cli.TuiError, "^tmux: boom$"):
            tmux_cli.TmuxCli(fake).has_session("w1")
        with self.assertRaises(tmux_cli.TuiError):
            tmux_cli.TmuxCli(fake).has_session("a b")
