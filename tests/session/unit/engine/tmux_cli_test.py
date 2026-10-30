"""TmuxCli's tmux verbs against a recorded tmux: status, process identity, kill, keys, press, read,
options and pane targets; listing row validation, shown-client precedence and diagnostic
tails.
"""

from ensemble.session.engine import tmux_cli
from ensemble.session.engine.types import PaneStatus
from tests.session.case import Case
from tests.session.tmux_fixtures import (
    ABSENT,
    FAILED,
    FORMAT,
    TMUX_KW,
    VIEWLESS,
    FakeTmux,
    panes,
    tabbed,
)


def out_of_mode(target):
    """The copy-mode -q a keys or press call chains first."""
    return ["tmux", "copy-mode", "-q", "-t", target, ";"]


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


class StatusLineTest(Case):
    def test_enabling_reserves_the_full_name_brackets_and_space_on_only_that_session(self):
        for name in ("s", "engineer-WDRC-281-502d679e"):
            with self.subTest(name=name):
                target = f"={name}:"
                self.assertEqual(
                    tmux_cli.status(name, on=True),
                    [
                        "set-option",
                        "-t",
                        target,
                        "status",
                        "on",
                        ";",
                        "set-option",
                        "-t",
                        target,
                        "status-left-length",
                        str(len(name) + 3),
                    ],
                )

    def test_disabling_only_sets_the_session_status(self):
        self.assertEqual(
            tmux_cli.status("worker", on=False),
            ["set-option", "-t", "=worker:", "status", "off"],
        )


class ProcessIdentityTest(Case):
    FORMAT = "#{pid} #{session_id} #{pane_id} #{pane_pid}"

    def test_the_server_session_pane_and_process_ids_of_its_pane_else_none(self):
        for target, printed, want in (
            ("s", (0, "4242 $3 %7 4343\n"), "4242 $3 %7 4343"),
            ("%7", (0, "4242 $3 %7 4343\n"), "4242 $3 %7 4343"),
            ("s", (0, " \n"), None),
            ("%7", (0, "4242   \n"), None),
            ("%7", (0, "4242  %7 4343\n"), None),
            ("%7", (0, "4242 $3 %7 \n"), None),
            ("%7", (0, "4242  %7 \n"), None),
            ("s", (1, "", "can't find session: =s\n"), None),
            ("%7", (1, "", "can't find pane: %7\n"), None),
            ("s", (1, "", "no server running on /tmp/tmux-501/default\n"), None),
        ):
            with self.subTest(target=target, printed=printed):
                fake = FakeTmux(results={"display-message": printed})
                self.assertEqual(tmux_cli.TmuxCli(fake).process(target), want)
                at = "=s:" if target == "s" else target
                self.assertEqual(
                    fake.calls, [["tmux", "display-message", "-p", "-t", at, self.FORMAT]]
                )

    def test_a_failure_bad_output_or_bad_target_raises(self):
        for printed, error in (
            ((1, "", "boom\n"), "^tmux: boom$"),
            ((0, "4242 $3 %7\n"), "^bad tmux process "),
            ((0, "4242  %7 bad\n"), "^bad tmux process "),
            ((0, "bad  %7 4343\n"), "^bad tmux process "),
            ((0, "boom\n"), "^bad tmux process "),
            ((0, "4242 $3  4343\n"), "^bad tmux process "),
            ((0, "4242 $3 %7 4343 9\n"), "^bad tmux process "),
        ):
            with self.subTest(printed=printed):
                fake = FakeTmux(results={"display-message": printed})
                with self.assertRaisesRegex(tmux_cli.TuiError, error):
                    tmux_cli.TmuxCli(fake).process("s")
        fake = FakeTmux()
        with self.assertRaisesRegex(tmux_cli.TuiError, "invalid target"):
            tmux_cli.TmuxCli(fake).process("a b")
        self.assertEqual(fake.calls, [])


class StatusTest(Case):
    def test_states(self):
        # a signal death: -n (tmux prints the signal's name where libc has one, else its number)
        for pane, want in (
            ("0  ", PaneStatus.RUNNING),
            ("1 3 ", 3),
            ("1 0 ", 0),
            ("1  kill", -9),
            ("1  9", -9),
            ("1  term", -15),
            ("  ", None),
            (None, None),
        ):
            with self.subTest(pane=pane):
                fake = FakeTmux(pane=pane)
                self.assertEqual(tmux_cli.TmuxCli(fake).status("s"), want)
                self.assertEqual(
                    fake.calls, [["tmux", "display-message", "-p", "-t", "=s:", FORMAT]]
                )
                self.assertEqual(fake.kwargs, [TMUX_KW])

    def test_a_dead_pane_tmux_has_not_reaped_is_running_and_gets_one_no_op_job(self):
        for fake in (
            FakeTmux(pane="1  "),
            FakeTmux(pane="1  ", fail=("run-shell",)),
            FakeTmux(pane="1  ", results={"run-shell": OSError("gone")}),
        ):
            with self.subTest(results=fake.results, fail=fake.fail):
                self.assertEqual(tmux_cli.TmuxCli(fake).status("s"), PaneStatus.RUNNING)
                self.assertEqual(
                    fake.calls,
                    [
                        ["tmux", "display-message", "-p", "-t", "=s:", FORMAT],
                        ["tmux", "run-shell", "-b", "true"],
                    ],
                )

    def test_a_pane_status_is_a_str_enum_member(self):
        self.assertEqual(list(PaneStatus), ["running", "unknown"])
        for name in ("RUNNING", "UNKNOWN"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(tmux_cli, name))
        for result, want in (
            ((0, "0  \n"), PaneStatus.RUNNING),
            ((0, "x\n"), PaneStatus.UNKNOWN),
        ):
            with self.subTest(result=result):
                fake = FakeTmux(results={"display-message": result})
                self.assertIs(tmux_cli.TmuxCli(fake).status("s"), want)

    def test_absent_for_a_missing_session_or_server_else_unknown(self):
        missing = FileNotFoundError(2, "No such file or directory", "tmux")
        for result, want in (
            *(((1, "", err + "\n"), None) for err in ABSENT),
            *(((1, "", err + "\n"), PaneStatus.UNKNOWN) for err in FAILED),
            ((0, "x\n"), PaneStatus.UNKNOWN),
            ((0, "2 0 \n"), PaneStatus.UNKNOWN),
            ((0, "1 0\n"), PaneStatus.UNKNOWN),
            ((0, "1 x \n"), PaneStatus.UNKNOWN),
            ((0, "1 \u00b2 \n"), PaneStatus.UNKNOWN),
            ((0, "1  \u00b2\n"), PaneStatus.UNKNOWN),
            ((0, "1  nals\n"), PaneStatus.UNKNOWN),
            ((0, ""), PaneStatus.UNKNOWN),
            (missing, PaneStatus.UNKNOWN),
            (PermissionError(13, "Permission denied"), PaneStatus.UNKNOWN),
        ):
            with self.subTest(result=result):
                fake = FakeTmux(results={"display-message": result})
                self.assertEqual(tmux_cli.TmuxCli(fake).status("s"), want)
                self.assertEqual(len(fake.calls), 1)


class KillTest(Case):
    def test_kill(self):
        fake = FakeTmux()
        tmux_cli.TmuxCli(fake).kill("s")
        self.assertEqual(fake.calls, [["tmux", "kill-session", "-t", "=s"]])
        self.assertEqual(fake.kwargs, [TMUX_KW])

    def test_failure(self):
        with self.assertRaisesRegex(tmux_cli.TuiError, r"^tmux: boom$"):
            tmux_cli.TmuxCli(FakeTmux(fail={"kill-session"})).kill("s")

    def test_unview_sizes_the_window_hidden_then_lets_the_next_client_size_it(self):
        for given, x, y in (
            ((), "80", "24"),
            (((100, 30),), "100", "30"),
            (((100, 30), True), "100", "31"),  # its pane under a border row
        ):
            with self.subTest(given=given):
                fake = FakeTmux()
                tmux_cli.TmuxCli(fake).unview("s", *given[:1], border=given[1:] == (True,))
                self.assertEqual(
                    fake.calls,
                    [
                        [
                            *("tmux", "resize-window", "-t", "=s:", "-x", x, "-y", y, ";"),
                            *("set-option", "-w", "-u", "-t", "=s:", "window-size"),
                        ]
                    ],
                )
        with self.assertRaisesRegex(tmux_cli.TuiError, r"^tmux: boom$"):
            tmux_cli.TmuxCli(FakeTmux(fail={"resize-window"})).unview("s")

    def test_a_pane_id_kills_that_pane_only(self):
        fake = FakeTmux()
        tmux_cli.TmuxCli(fake).kill("%12")
        self.assertEqual(fake.calls, [["tmux", "kill-pane", "-t", "%12"]])
        self.assertEqual(fake.kwargs, [TMUX_KW])
        with self.assertRaisesRegex(tmux_cli.TuiError, r"^tmux: can't find pane: %12$"):
            tmux_cli.TmuxCli(
                FakeTmux(results={"kill-pane": (1, "", "can't find pane: %12\n")})
            ).kill("%12")


class KeysTest(Case):
    def test_send_keys_names_the_keys(self):
        fake = FakeTmux()
        self.assertIsNone(tmux_cli.TmuxCli(fake).keys("s", "Escape", "C-c", "Enter"))
        self.assertEqual(
            fake.calls, [[*out_of_mode("=s:"), "send-keys", "-t", "=s:", "Escape", "C-c", "Enter"]]
        )
        self.assertEqual(fake.kwargs, [TMUX_KW])

    def test_a_name_tmux_would_misread_is_refused_before_tmux(self):
        for name in ("-l", "-", "Enter;", ";"):
            with self.subTest(name=name):
                fake = FakeTmux()
                with self.assertRaisesRegex(tmux_cli.TuiError, "^tmux would misread"):
                    tmux_cli.TmuxCli(fake).keys("s", "Escape", name)
                self.assertEqual(fake.calls, [])

    def test_failure(self):
        with self.assertRaisesRegex(tmux_cli.TuiError, r"^tmux: boom$"):
            tmux_cli.TmuxCli(FakeTmux(fail={"copy-mode"})).keys("s", "Enter")

    def test_press_names_the_keys_after_a_double_dash(self):
        fake = FakeTmux()
        self.assertIsNone(tmux_cli.TmuxCli(fake).press("s", "-", "\\;", "C-c"))
        self.assertEqual(
            fake.calls, [[*out_of_mode("=s:"), "send-keys", "-t", "=s:", "--", "-", "\\;", "C-c"]]
        )

    def test_press_refuses_an_empty_name_or_an_unescaped_semicolon(self):
        for name in ("", ";", "C-;"):
            with self.subTest(name=name):
                fake = FakeTmux()
                with self.assertRaisesRegex(tmux_cli.TuiError, "^tmux would misread"):
                    tmux_cli.TmuxCli(fake).press("s", "Escape", name)
                self.assertEqual(fake.calls, [])


class ReadTest(Case):
    def test_visible_pane_or_last_lines(self):
        for lines, printed, want, extra in (
            (None, "a\n  b\n\n \n", "a\n  b", []),
            (3, "1\n2\n\n3\n4\n\n\n", "\n3\n4", ["-S", "-3"]),
        ):
            with self.subTest(lines=lines):
                fake = FakeTmux(results={"capture-pane": (0, printed)})
                self.assertEqual(tmux_cli.TmuxCli(fake).read("s", lines), want)
                self.assertEqual(
                    fake.calls, [["tmux", "capture-pane", "-p", "-J", "-t", "=s:", *extra]]
                )
                self.assertEqual(fake.kwargs, [TMUX_KW])

    def test_lines_must_be_a_positive_int(self):
        fake = FakeTmux()
        for lines in (0, -1, True, 1.5, "3"):
            with self.subTest(lines=lines), self.assertRaises(tmux_cli.TuiError):
                tmux_cli.TmuxCli(fake).read("s", lines)
        self.assertEqual(fake.calls, [])

    def test_failure(self):
        with self.assertRaisesRegex(tmux_cli.TuiError, r"^tmux: boom$"):
            tmux_cli.TmuxCli(FakeTmux(fail={"capture-pane"})).read("s")


class OptionTest(Case):
    def test_value(self):
        for printed, want in (("0123abcd\n", "0123abcd"), ("a b\n", "a b"), ("\n", "")):
            with self.subTest(printed=printed):
                fake = FakeTmux(results={"show-options": (0, printed)})
                self.assertEqual(tmux_cli.TmuxCli(fake).option("s", "@sid"), want)
                self.assertEqual(fake.calls, [["tmux", "show-options", "-v", "-t", "=s:", "@sid"]])
                self.assertEqual(fake.kwargs, [TMUX_KW])

    def test_none_when_unset_or_absent(self):
        for err in ("invalid option: @sid", *ABSENT):
            with self.subTest(err=err):
                fake = FakeTmux(results={"show-options": (1, "", err + "\n")})
                self.assertIsNone(tmux_cli.TmuxCli(fake).option("s", "@sid"))

    def test_another_failure_raises(self):
        for result in (
            (1, "", FAILED[0] + "\n"),
            (1, "", "boom\n"),
            FileNotFoundError(2, "No such file", "tmux"),
        ):
            with (
                self.subTest(result=result),
                self.assertRaisesRegex(tmux_cli.TuiError, "^tmux: "),
            ):
                tmux_cli.TmuxCli(FakeTmux(results={"show-options": result})).option("s", "@sid")

    def test_only_a_user_option(self):
        for name in ("status", "@", "@a b", "@a;", "", "-g"):
            with self.subTest(name=name):
                fake = FakeTmux()
                with self.assertRaisesRegex(tmux_cli.TuiError, "^option "):
                    tmux_cli.TmuxCli(fake).option("s", name)
                self.assertEqual(fake.calls, [])


class PaneTargetTest(Case):
    """status, kill, keys, press, option and read take a pane id (%N) as their target."""

    def calls(self, target, fake):
        return [
            lambda: tmux_cli.TmuxCli(fake).status(target),
            lambda: tmux_cli.TmuxCli(fake).kill(target),
            lambda: tmux_cli.TmuxCli(fake).keys(target, "Enter"),
            lambda: tmux_cli.TmuxCli(fake).press(target, "Enter"),
            lambda: tmux_cli.TmuxCli(fake).option(target, "@sid"),
            lambda: tmux_cli.TmuxCli(fake).read(target),
        ]

    def test_a_pane_id_is_the_target_as_is(self):
        fake = FakeTmux(pane="0  ")
        for call in self.calls("%12", fake):
            call()
        self.assertEqual(
            fake.calls,
            [
                ["tmux", "display-message", "-p", "-t", "%12", FORMAT],
                ["tmux", "kill-pane", "-t", "%12"],
                [*out_of_mode("%12"), "send-keys", "-t", "%12", "Enter"],
                [*out_of_mode("%12"), "send-keys", "-t", "%12", "--", "Enter"],
                ["tmux", "show-options", "-v", "-t", "%12", "@sid"],
                ["tmux", "capture-pane", "-p", "-J", "-t", "%12"],
            ],
        )

    def test_a_gone_pane_is_absent(self):
        gone = (1, "", "can't find pane: %12\n")
        self.assertIsNone(
            tmux_cli.TmuxCli(FakeTmux(results={"display-message": gone})).status("%12")
        )
        self.assertIsNone(
            tmux_cli.TmuxCli(FakeTmux(results={"show-options": gone})).option("%12", "@sid")
        )

    def test_a_bad_target_is_refused_before_tmux(self):
        for target in ("%", "%x", "%1;", "%1 ", "%1:", "%1.0", "=%1", "%-1", "a b"):
            with self.subTest(target=target):
                fake = FakeTmux()
                for call in self.calls(target, fake):
                    with self.assertRaisesRegex(tmux_cli.TuiError, "^invalid target "):
                        call()
                self.assertEqual(fake.calls, [])


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


class SessionsTest(Case):
    def test_names_none_without_a_server_and_none_on_failure(self):
        for result, want in (
            ((0, "w1\nmgr\n"), ["w1", "mgr"]),
            ((1, "", "no server running on /tmp/tmux-501/default\n"), []),
            ((1, "", "boom\n"), None),
        ):
            with self.subTest(result=result):
                fake = FakeTmux(results={"list-sessions": result})
                self.assertEqual(tmux_cli.TmuxCli(fake).sessions(), want)
                self.assertEqual(
                    fake.calls,
                    [["tmux", "-u", "list-sessions", *VIEWLESS, "-F", "#{session_name}"]],
                )


class ListingTest(Case):
    def listing(self, sessions, clients=(0, ""), panes=(0, "")):
        self.fake = FakeTmux(
            results={"list-sessions": sessions, "list-clients": clients, "list-panes": panes}
        )
        return tmux_cli.TmuxCli(self.fake).listing()

    def test_only_viewer_placement_is_read_and_sorted(self):
        rows = self.listing((0, "w2\tmgr\nw1\t\n"))
        self.assertEqual(
            rows,
            [
                {"name": "w1", "opener": None, "shown": None},
                {"name": "w2", "opener": "mgr", "shown": None},
            ],
        )
        self.assertEqual(
            self.fake.calls[0],
            [
                "tmux",
                "-u",
                "list-sessions",
                "-f",
                "#{!=:#{@view},}",
                "-F",
                "#{session_name}\t#{@opener}",
            ],
        )
        self.assertNotIn("capture-pane", self.fake.commands())

    def test_invalid_names_width_and_openers_are_rejected(self):
        rows = self.listing((0, "bad name\tmgr\nok\tbad opener\nextra\tmgr\tforged\n"))
        self.assertEqual(rows, [{"name": "ok", "opener": None, "shown": None}])

    def test_shown_uses_the_latest_clients_live_host_pane(self):
        rows = self.listing(
            (0, "w1\tmgr\nw2\t\nw3\t\n"),
            tabbed(
                ("10", "/dev/host", "w1"),
                ("9", "/dev/old", "w1"),
                ("7", "/dev/bad", "w2"),
                ("7", "/dev/gone", "w3"),
            ),
            panes(
                ("0", "/dev/host", "manager"),
                ("0", "/dev/bad", "bad name"),
                ("1", "/dev/gone", "old"),
            ),
        )
        self.assertEqual([row["shown"] for row in rows], ["manager", "", ""])

    def test_no_server_or_valid_rows_returns_empty(self):
        for result in ((0, ""), (0, "bad name\tmgr\n"), *((1, "", err) for err in ABSENT)):
            with self.subTest(result=result):
                self.assertEqual(self.listing(result), [])
                self.assertEqual(len(self.fake.calls), 1)

    def test_each_tmux_failure_is_reported(self):
        for at in ("list-sessions", "list-clients", "list-panes"):
            with self.subTest(at=at):
                results = {
                    "list-sessions": (0, "w1\tmgr\n"),
                    "list-clients": tabbed(("1", "/dev/host", "w1")),
                    "list-panes": (0, ""),
                }
                results[at] = (1, "", "lost server")
                with self.assertRaisesRegex(tmux_cli.TuiError, "lost server"):
                    tmux_cli.TmuxCli(FakeTmux(results=results)).listing()
