"""TmuxCli's tmux verbs against a recorded tmux: status, process identity, kill, send, paste, keys,
read, options and pane targets; listing row validation, shown-client precedence and diagnostic
tails.
"""

import errno
import os
import re
import stat
import sys
import tempfile
import unittest
import unittest.mock

from ensemble.session import ConfigError, Launch
from ensemble.session.engine import tmux_cli
from ensemble.session.engine.tmux_cli import (
    EXEC,
    HANDOVER_TIMEOUT,
    POLL,
    TERMINAL_KEYS,
    TmuxCli,
)
from ensemble.session.engine.types import PaneStatus
from tests.session.case import Case
from tests.session.tmux_fixtures import (
    ABSENT,
    ALL_CLIENTS,
    COLUMNS,
    FAILED,
    FORMAT,
    HOST_PANES,
    INSIDE,
    ITERM,
    LISTING,
    NEW,
    NEW_PROCESS,
    PREFIXES,
    TMUX_KW,
    VIEWLESS,
    FakeTmux,
    StartFixture,
    captured,
    chained,
    decorations,
    environ,
    expected_row,
    listed,
    panes,
    recorded,
    tabbed,
    which,
)


def out_of_mode(target):
    """The copy-mode -q a paste or keys call chains first."""
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

    def test_set_option_sets_a_user_option_of_the_targets_session(self):
        fake = FakeTmux()
        self.assertIsNone(tmux_cli.TmuxCli(fake).set_option("%7", "@record", '{"v":1}'))
        self.assertEqual(fake.calls, [["tmux", "set-option", "-t", "%7", "@record", '{"v":1}']])
        for name, value in (("record", "x"), ("@record", "x;"), ("@record", "a\nb")):
            with self.subTest(name=name, value=value), self.assertRaises(tmux_cli.TuiError):
                tmux_cli.TmuxCli(fake).set_option("%7", name, value)
        self.assertEqual(len(fake.calls), 1)

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
    """status, kill, paste, keys, option and read take a pane id (%N) as their target."""

    def calls(self, target, fake):
        return [
            lambda: tmux_cli.TmuxCli(fake).status(target),
            lambda: tmux_cli.TmuxCli(fake).kill(target),
            lambda: tmux_cli.TmuxCli(fake).paste(target, "hi"),
            lambda: tmux_cli.TmuxCli(fake).keys(target, "Enter"),
            lambda: tmux_cli.TmuxCli(fake).option(target, "@sid"),
            lambda: tmux_cli.TmuxCli(fake).read(target),
        ]

    def test_a_pane_id_is_the_target_as_is(self):
        fake = FakeTmux(pane="0  ")
        for call in self.calls("%12", fake):
            call()
        buffer = fake.calls[2][3]
        self.assertEqual(
            fake.calls,
            [
                ["tmux", "display-message", "-p", "-t", "%12", FORMAT],
                ["tmux", "kill-pane", "-t", "%12"],
                ["tmux", "load-buffer", "-b", buffer, "-"],
                [*out_of_mode("%12"), "paste-buffer", "-p", "-r", "-d", "-b", buffer, "-t", "%12"],
                [*out_of_mode("%12"), "send-keys", "-t", "%12", "Enter"],
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
    def listing(self, sessions, clients=(0, ""), panes=(0, ""), capture=(0, ""), match=None):
        fake = FakeTmux(
            results={
                "list-sessions": sessions,
                "list-clients": clients,
                "list-panes": panes,
                "capture-pane": capture,
            }
        )
        self.fake = fake
        return tmux_cli.TmuxCli(fake).listing(match), fake

    def test_every_field_sorted_by_name_one_call_each(self):
        got, fake = self.listing(
            listed(
                ("w2", "0", "sid-2", "fake", "done", "%7", "mgr"),
                ("w1", "1", "sid-1", "other", "dead", "ABC-1", "w0t0p0:ABC-1"),
                ("mgr", "0"),
            ),
            tabbed(
                ("200", "/dev/ttys4", "w2"), ("100", "/dev/ttys9", "w2"), ("5", "/dev/ttys1", "mgr")
            ),
            panes(("0", "/dev/ttys4", "mgr"), ("0", "/dev/ttys2", "w2")),
            (0, "a\nlast\n\n"),
        )
        self.assertEqual(
            got,
            [
                expected_row("mgr", shown=""),
                expected_row(
                    "w1",
                    sid="sid-1",
                    client="other",
                    state="dead",
                    pane="ABC-1",
                    opener="w0t0p0:ABC-1",
                    dead=True,
                    last_lines="a\nlast",
                ),
                expected_row(
                    "w2",
                    sid="sid-2",
                    client="fake",
                    state="done",
                    pane="%7",
                    opener="mgr",
                    shown="mgr",
                ),
            ],
        )
        self.assertEqual(fake.calls, [LISTING, ALL_CLIENTS, HOST_PANES, captured("w1")])
        self.assertEqual(fake.kwargs, [TMUX_KW] * 4)
        self.assertEqual(tmux_cli.LAST_LINES, 2000)

    def test_a_name_failing_NAME_or_a_row_tmux_split_wrongly_is_dropped(self):
        got, fake = self.listing(
            (0, "".join(f"{n}\t0\t\t\t\t\t\t\t\n" for n in ("a b", "", "%1", "x;", "#{x}", "ok")))
        )
        self.assertEqual(got, [expected_row("ok")])
        got, _ = self.listing((0, "w1\t0\ts\tid\t\t\t\t\t\t\nw2\t0\nw3\t0\t\t\t\t\t\t\t\n"))
        self.assertEqual(got, [expected_row("w3")])
        forged = listed(("w4", "0", "", "", "", "", "", "mgr\tx"), ("w5", "0"))
        got, _ = self.listing(forged)
        self.assertEqual(got, [expected_row("w5")])
        got, fake = self.listing(listed(("a b", "0")))
        self.assertEqual((got, fake.calls), ([], [LISTING]))

    def test_a_bad_or_empty_field_is_none(self):
        for key, value, want in (
            ("sid", "sid-1", "sid-1"),
            ("sid", "", None),
            ("sid", "s\x1b]0;x\x07", None),
            ("sid", "s\x7f", None),
            ("client", "fake", "fake"),
            ("client", "", None),
            ("client", "\x1b[2J", None),
            ("state", "done", "done"),
            ("state", "", None),
            ("state", "do\x08ne", None),
            ("pane", "%3", "%3"),
            ("pane", "ABC-12", "ABC-12"),
            ("pane", "", None),
            ("pane", "%", None),
            ("pane", "%3;", None),
            ("pane", "w0t0p0:ABC", None),
            ("opener", "mgr", "mgr"),
            ("opener", "w0t0p0:ABC-1", "w0t0p0:ABC-1"),
            ("opener", "cmd", "cmd"),
            ("opener", "a_b-1", "a_b-1"),
            ("opener", "w0t0p0:ABC", "w0t0p0:ABC"),
            ("opener", "w0t1p0:6D7E-8F", "w0t1p0:6D7E-8F"),
            ("opener", "", None),
            ("opener", "a b", None),
            ("opener", "x:", None),
            ("opener", "%3", None),
            ("display", "mgr/right/m1", "mgr/right/m1"),
            ("display", "elsewhere:1.2", "elsewhere:1.2"),
            ("display", "", None),
            ("display", "mgr;", None),
            ("display", "a\x1bb", None),
            ("dead", "0", False),
            ("dead", "1", True),
            ("dead", "", None),
            ("dead", "2", None),
        ):
            with self.subTest(key=key, value=value):
                fields = {**dict.fromkeys(COLUMNS, ""), "name": "s", "dead": "0", key: value}
                got, _ = self.listing(listed(tuple(fields[c] for c in COLUMNS)))
                self.assertEqual(got[0][key], want)

    def test_a_dead_rows_last_lines_live_rows_none(self):
        printed = {
            "=d1:": (0, "x\n  y\n\n"),
            "=d2:": (1, "", "boom\n"),
            "=d3:": (0, "\n \n"),
            "=d4:": FileNotFoundError(2, "No such file", "tmux"),
            "=d5:": (0, "".join(f"{i}\n" for i in range(2001)) + "\n\n"),
        }
        got, fake = self.listing(
            listed(*((n, "1") for n in ("d1", "d2", "d3", "d4", "d5")), ("live", "0"), ("odd", "")),
            capture=lambda argv: printed[argv[argv.index("-t") + 1]],
        )
        self.assertEqual(
            [(r["name"], r["last_lines"]) for r in got],
            [
                ("d1", "x\n  y"),
                ("d2", None),
                ("d3", None),
                ("d4", None),
                ("d5", "\n".join(map(str, range(1, 2001)))),
                ("live", None),
                ("odd", None),
            ],
        )
        self.assertEqual(fake.calls[2:], [captured(n) for n in ("d1", "d2", "d3", "d4", "d5")])

    def test_a_launched_session_reads_its_own_pane_whichever_pane_is_active(self):
        got, fake = self.listing(
            listed(
                ("w1", "0", *[""] * 6, "4242 $1 %7 4343"),
                ("w2", "1", *[""] * 6, "4242 $2 %8 4344"),
                ("w3", "0", *[""] * 6, "4242 $3 %9 4345"),
                ("w4", "1", *[""] * 6, "4242 $4 %x 4346"),
                ("mgr", "1"),
            ),
            panes=panes(
                ("1", "", "w1", "%7"),
                ("0", "/dev/ttys1", "w1", "%10"),
                ("0", "/dev/ttys2", "w2", "%8"),
                ("1", "", "w2", "%11"),
                ("0", "/dev/ttys3", "w3", "%12"),
            ),
            capture=lambda argv: {"%7": (0, "agent\n"), "=mgr:": (0, "manager\n")}[
                argv[argv.index("-t") + 1]
            ],
        )
        self.assertEqual(
            [(r["name"], r["dead"], r["last_lines"]) for r in got],
            [
                ("mgr", True, "manager"),
                ("w1", True, "agent"),
                ("w2", False, None),
                ("w3", True, None),
                ("w4", None, None),
            ],
        )
        self.assertEqual(
            fake.calls, [LISTING, ALL_CLIENTS, HOST_PANES, captured("mgr"), captured("%7")]
        )

    def test_shown_by_the_most_recent_client_in_its_host_session(self):
        names = ("a", "b", "c", "d", "e", "f", "g", "h", "i")
        got, fake = self.listing(
            listed(*((n, "0") for n in names)),
            tabbed(
                ("7", "/dev/host", "b"),
                ("7", "/dev/term", "c"),
                ("7", "/dev/bad", "d"),
                ("7", "/dev/gone", "e"),
                ("10", "/dev/host", "f"),
                ("20", "/dev/term", "f"),
                ("9", "/dev/term", "g"),
                ("10", "/dev/host", "g"),
                ("x", "/dev/host", "h"),
                ("7", "", "h"),
                ("7", "/dev/host", "h", "extra"),
                ("7", "/dev/host"),
                ("7", "/dev/host", "zz"),
            ),
            panes(
                ("0", "/dev/host", "host"),
                ("0", "/dev/bad", "bad name"),
                ("1", "/dev/gone", "host2"),
            ),
        )
        self.assertEqual(
            {r["name"]: r["shown"] for r in got},
            {
                "a": None,
                "b": "host",
                "c": "",
                "d": "",
                "e": "",
                "f": "",
                "g": "host",
                "h": None,
                "i": None,
            },
        )
        self.assertEqual(fake.calls, [LISTING, ALL_CLIENTS, HOST_PANES])

    def test_host_panes_only_when_a_listed_session_has_a_client_or_a_launch(self):
        for clients, match, want in (
            ((0, ""), None, [expected_row("mgr"), expected_row("w1")]),
            (tabbed(("7", "/dev/host", "w1")), re.compile("mgr"), [expected_row("mgr")]),
        ):
            with self.subTest(clients=clients):
                got, fake = self.listing(listed(("mgr", "0"), ("w1", "0")), clients, match=match)
                self.assertEqual(got, want)
                self.assertEqual(fake.calls, [LISTING, ALL_CLIENTS])
        got, fake = self.listing(listed(("w1", "0", *[""] * 6, "4242 $1 %7 4343")))
        self.assertEqual(got, [expected_row("w1", dead=True)])
        self.assertEqual(fake.calls, [LISTING, ALL_CLIENTS, HOST_PANES])

    def test_match_fullmatches_before_any_capture(self):
        sessions = listed(("w1", "1"), ("w10", "1"), ("mgr", "1"), ("xw1", "0"))
        for match, want in (
            (re.compile("w1"), ["w1"]),
            (re.compile("w[0-9]+"), ["w1", "w10"]),
            (None, ["mgr", "w1", "w10", "xw1"]),
        ):
            with self.subTest(match=match):
                got, fake = self.listing(sessions, match=match)
                self.assertEqual([r["name"] for r in got], want)
                self.assertEqual(
                    fake.calls, [LISTING, ALL_CLIENTS, *(captured(n) for n in want if n != "xw1")]
                )
        got, fake = self.listing(sessions, match=re.compile("w"))
        self.assertEqual((got, fake.calls), ([], [LISTING]))

    def test_no_server_is_empty(self):
        for result in (*((1, "", err + "\n") for err in ABSENT), (0, "")):
            with self.subTest(result=result):
                got, fake = self.listing(result)
                self.assertEqual((got, fake.calls), ([], [LISTING]))

    def test_tmux_failing_raises_why(self):
        sessions, client = listed(("w1", "0")), tabbed(("7", "/dev/host", "w1"))
        for failing, why in (
            *(((1, "", err + "\n"), "exited 1" + (f": {err}" if err else "")) for err in FAILED),
            (FileNotFoundError(2, "No such file", "tmux"), "[Errno 2] No such file: 'tmux'"),
            (
                BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable"),
                f"[Errno {errno.EAGAIN}] Resource temporarily unavailable",
            ),
        ):
            for at, calls in (
                ({"sessions": failing}, [LISTING]),
                ({"clients": failing}, [LISTING, ALL_CLIENTS]),
                ({"clients": client, "panes": failing}, [LISTING, ALL_CLIENTS, HOST_PANES]),
            ):
                with self.subTest(failing=failing, at=at):
                    with self.assertRaises(tmux_cli.TuiError) as raised:
                        self.listing(**{"sessions": sessions, **at})
                    verb = calls[-1][2]
                    want = f"tmux {verb} {why}" if why.startswith("exited") else f"tmux: {why}"
                    self.assertEqual((str(raised.exception), self.fake.calls), (want, calls))


class StartTest(StartFixture):
    def test_the_creating_call_records_and_returns_its_pane_identity(self):
        fake = FakeTmux()
        self.assertEqual(self.start(fake), NEW_PROCESS)
        self.assertEqual(fake.calls[1][: 3 + len(NEW)], ["tmux", "new-session", "-d", *NEW])
        self.assertEqual(fake.calls[1][-len(recorded("s")) :], recorded("s"))

    def test_a_new_session_tmux_does_not_name_is_killed(self):
        for out in ("", "4242 $3 %7\n", "4242 $3 %7 x\n"):
            with self.subTest(out=out):
                fake = FakeTmux(results={"new-session": (0, out, "")})
                with self.assertRaisesRegex(tmux_cli.TuiError, "^tmux: bad new session "):
                    self.start(fake)
                self.assertEqual(
                    fake.commands(), ["display-message", "new-session", "kill-session"]
                )
                self.assertEqual(os.listdir(self.temp), [])

    def test_start_runs_wrapper_decorates_dead_pane_without_opening_a_pane(
        self,
    ):
        # in tmux or iTerm2 all the same: start never shows the session
        for env in ({}, INSIDE, ITERM):
            with self.subTest(env=env), environ(**env), which():
                fake = FakeTmux()
                self.start(fake)
                self.assertEqual(fake.commands(), ["display-message", "new-session"])
                self.assertEqual(
                    fake.calls[1],
                    [
                        "tmux",
                        "new-session",
                        "-d",
                        *NEW,
                        "-s",
                        "s",
                        "-x",
                        "120",
                        "-y",
                        "40",
                        sys.executable,
                        "-I",
                        "-c",
                        EXEC,
                        fake.path,
                        fake.fifo,
                        ";",
                        "set-option",
                        "-p",
                        "-t",
                        "=s:",
                        "remain-on-exit",
                        "on",
                        *chained(decorations("s")),
                        *recorded("s"),
                    ],
                )
                self.assertNotIn("-c", fake.calls[1][: fake.calls[1].index(sys.executable)])
                self.assertEqual({a for a in fake.calls[1] if a.endswith(";")}, {";"})
                self.assertEqual(fake.kwargs, [TMUX_KW] * len(fake.calls))
                self.sleep.assert_not_called()
                self.assertEqual(self.stderr.getvalue(), "")

    def test_handover_file(self):
        # the env as given but its terminal keys; PWD the cwd, given another (as here) or none
        # (test_events_and_...)
        env = {
            "PATH": self.bin,
            "HOME": "/h",
            "CHILD_SESSION": "1",
            "PWD": "/elsewhere",
            **{k: "x" for k in TERMINAL_KEYS},
        }
        for argv in (("tool", "-x", "a b"), ("tool", "--settings", "s.json")):
            with self.subTest(argv=argv):
                fake = FakeTmux()
                self.start(fake, argv=argv, env=env)
                self.assertEqual(
                    fake._handover,
                    {
                        "argv": [self.tool, *argv[1:]],
                        "env": {
                            "PATH": self.bin,
                            "HOME": "/h",
                            "CHILD_SESSION": "1",
                            "PWD": self.cwd,
                        },
                        "cwd": self.cwd,
                    },
                )
                self.assertEqual(fake.mode, 0o600)
                self.assertEqual(fake.dir_mode, 0o700)
                self.assertEqual(os.path.dirname(os.path.dirname(fake.path)), self.temp)
                self.assertEqual(os.path.dirname(fake.fifo), os.path.dirname(fake.path))
                self.assertTrue(stat.S_ISFIFO(fake.fifo_mode))
                self.assertEqual(stat.S_IMODE(fake.fifo_mode), 0o600)
                self.assertEqual(os.listdir(self.temp), [])

    def test_terminal_keys(self):
        terminal = dict.fromkeys(
            (
                "TMUX",
                "TMUX_PANE",
                "TERM",
                "COLORTERM",
                "TERM_PROGRAM",
                "TERM_PROGRAM_VERSION",
                "TERM_SESSION_ID",
                "ITERM_SESSION_ID",
                "ITERM_PROFILE",
                "LC_TERMINAL",
                "LC_TERMINAL_VERSION",
                "COLUMNS",
                "LINES",
            ),
            "x",
        )
        fake = FakeTmux()
        self.start(fake, env={"PATH": self.bin, "HOME": "/h", "SHELL": "/bin/zsh", **terminal})
        self.assertEqual(
            fake._handover["env"],
            {"PATH": self.bin, "HOME": "/h", "SHELL": "/bin/zsh", "PWD": self.cwd},
        )

    def test_cwd_never_reaches_tmux(self):
        self.cwd = os.path.join(self.root, "#(x)#{y}")
        os.makedirs(self.cwd)
        fake = FakeTmux()
        self.start(fake)
        self.assertEqual(fake._handover["cwd"], self.cwd)
        self.assertEqual([a for c in fake.calls for a in c if "#(x)" in a or "#{y}" in a], [])

    def test_launch_rejects_nul_before_tmux(self):
        fake = FakeTmux()
        with self.assertRaisesRegex(ConfigError, "launch: argv must be strings without NUL"):
            self.start(fake, argv=("tool", "a\0b"))
        self.assertEqual(fake.calls, [])
        self.assertEqual(os.listdir(self.temp), [])

    def test_refused_before_tmux(self):
        # the command resolved with the env's PATH (no sh there); an events file that is no regular
        # file
        for kw, error in (
            ({"argv": ("sh", "-c", "true")}, "sh"),
            ({"argv": ("no-such-tool",)}, "no-such-tool"),
            ({"argv": ()}, "no command"),
            ({"events": self.cwd}, ""),
        ):
            with self.subTest(**kw):
                fake = FakeTmux()
                with self.assertRaisesRegex(tmux_cli.TuiError, error):
                    self.start(fake, **kw)
                self.assertEqual(fake.calls, [])
                self.assertEqual(os.listdir(self.temp), [])

    def test_handover_file_unwritable(self):
        fake = FakeTmux()
        with (
            unittest.mock.patch.object(tempfile, "tempdir", os.path.join(self.root, "missing")),
            self.assertRaisesRegex(tmux_cli.TuiError, "^handover: "),
        ):
            self.start(fake)
        real = os.open

        def refuse(path, *args, **kw):
            if str(path).endswith("handover.json"):
                raise PermissionError(13, "Permission denied", path)
            return real(path, *args, **kw)

        with (
            unittest.mock.patch("os.open", side_effect=refuse),
            self.assertRaisesRegex(tmux_cli.TuiError, "^handover: "),
        ):
            self.start(fake)
        self.assertNotIn("new-session", fake.commands())
        self.assertEqual(os.listdir(self.temp), [])

    def test_relative_command_made_absolute(self):
        here = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, here)
        for argv0, path in (("bin/tool", "/nonexistent"), ("tool", "bin")):
            fake = FakeTmux()
            self.start(fake, argv=(argv0,), env={"PATH": path})
            self.assertEqual(fake._handover["argv"], [self.tool])

    def test_live_session_is_refused(self):
        for env, prefix in PREFIXES:
            with self.subTest(env=env), unittest.mock.patch.dict(os.environ, env):
                fake = FakeTmux(pane="0  ")
                with self.assertRaisesRegex(
                    tmux_cli.TuiError,
                    "^" + re.escape(f"session s is running; {prefix}tmux attach -t '=s'") + "$",
                ):
                    self.start(fake)
                self.assertEqual(
                    fake.calls, [["tmux", "display-message", "-p", "-t", "=s:", FORMAT]]
                )
                self.assertEqual(os.listdir(self.temp), [])

    def test_dead_session_is_refused_untouched(self):
        for pane in ("1 0 ", "1  9"):
            with self.subTest(pane=pane):
                fake = FakeTmux(pane=pane)
                with self.assertRaisesRegex(tmux_cli.TuiError, "^session s exists$"):
                    self.start(fake)
                self.assertEqual(
                    fake.calls, [["tmux", "display-message", "-p", "-t", "=s:", FORMAT]]
                )
                self.assertEqual(os.listdir(self.temp), [])

    def test_an_unknown_session_is_never_killed(self):
        # tmux failing otherwise: new-session finds out; another start's session is not ours to kill
        for err in FAILED:
            with self.subTest(err=err):
                fake = FakeTmux(results={"display-message": (1, "", err)})
                self.start(fake)
                self.assertEqual(fake.commands(), ["display-message", "new-session"])
        fake = FakeTmux(
            results={
                "display-message": (1, "", "boom\n"),
                "new-session": (1, "", "duplicate session: s\n"),
            }
        )
        with self.assertRaisesRegex(tmux_cli.TuiError, r"^tmux: duplicate session: s$"):
            self.start(fake)
        self.assertEqual(fake.commands(), ["display-message", "new-session"])

    def test_tags_are_session_options_set_in_the_new_session_call(self):
        fake = FakeTmux()
        self.start(fake, tags={"@sid": "0123abcd-x", "@k": "a b#{x}"})
        self.assertEqual(
            fake.calls[1][fake.calls[1].index(fake.fifo) + 1 :],
            [
                ";",
                "set-option",
                "-p",
                "-t",
                "=s:",
                "remain-on-exit",
                "on",
                ";",
                "set-option",
                "-t",
                "=s:",
                "@sid",
                "0123abcd-x",
                ";",
                "set-option",
                "-t",
                "=s:",
                "@k",
                "a b#{x}",
                *chained(decorations("s")),
                *recorded("s"),
            ],
        )
        self.assertEqual(fake.commands(), ["display-message", "new-session"])

    def test_bad_tags_refused_before_tmux(self):
        for tags in (
            {"sid": "x"},
            {"@": "x"},
            {"@a b": "x"},
            {"@a;": "x"},
            {"@k": "x;"},
            {"@k": 1},
            {"@k": None},
        ):
            with self.subTest(tags=tags):
                fake = FakeTmux()
                with self.assertRaisesRegex(tmux_cli.TuiError, "^tag "):
                    self.start(fake, tags=tags)
                self.assertEqual(fake.calls, [])
                self.assertEqual(os.listdir(self.temp), [])

    def test_a_display_tag_tmux_could_not_list_or_a_nul_is_refused_before_tmux(self):
        for tags, error in (
            ({"@display": "a\tb"}, "^display "),
            ({"@display": "x;"}, "^display "),
            ({"@k": "a\0b"}, "^tag "),
        ):
            with self.subTest(tags=tags):
                fake = FakeTmux()
                with self.assertRaisesRegex(tmux_cli.TuiError, error):
                    self.start(fake, tags=tags)
                self.assertEqual(fake.calls, [])

    def test_a_failed_tmux_call_kills_the_session_it_may_have_made(self):
        fake = FakeTmux(
            fail={"new-session"}
        )  # e.g. the chained set-option failed after new-session made the session
        with self.assertRaisesRegex(tmux_cli.TuiError, r"^tmux: boom$"):
            self.start(fake)
        self.assertEqual(fake.commands(), ["display-message", "new-session", "kill-session"])
        self.assertEqual(fake.calls[2], ["tmux", "kill-session", "-t", "=s"])
        self.assertEqual(os.listdir(self.temp), [])
        self.assertEqual(self.stderr.getvalue(), "")

    def test_a_duplicate_session_is_not_killed(self):
        fake = FakeTmux(
            results={"new-session": (1, "", "duplicate session: s\n")}
        )  # another start took the name
        with self.assertRaisesRegex(tmux_cli.TuiError, r"^tmux: duplicate session: s$"):
            self.start(fake)
        self.assertEqual(fake.commands(), ["display-message", "new-session"])
        self.assertEqual(os.listdir(self.temp), [])

    def test_an_interrupt_after_new_session_kills_the_session(self):
        def interrupt(*_args, **_kw):
            raise KeyboardInterrupt

        fake = FakeTmux(hand_over=False)
        self.sleep = interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.start(fake)
        self.assertEqual(fake.commands(), ["display-message", "new-session", "kill-session"])
        self.assertIs(fake.dir_at_kill, expr2=False)
        self.assertEqual(os.listdir(self.temp), [])

    def test_tmux_missing(self):
        # status: unknown; new-session: the error
        def missing(_argv, **_kw):
            raise FileNotFoundError(2, "No such file or directory", "tmux")

        with self.assertRaisesRegex(tmux_cli.TuiError, r"^tmux: "):
            self.start(missing)
        self.assertEqual(os.listdir(self.temp), [])

    def test_wrapper_never_reads(self):
        for fail in ((), {"kill-session"}):
            with self.subTest(fail=fail):
                self.sleep.reset_mock()
                fake = FakeTmux(fail=fail, hand_over=False)
                with self.assertRaisesRegex(tmux_cli.TuiError, r"^the session did not start$"):
                    self.start(fake)
                self.assertEqual(
                    fake.commands(), ["display-message", "new-session", "kill-session"]
                )
                self.assertEqual(fake.calls[2], ["tmux", "kill-session", "-t", "=s"])
                self.assertIs(fake.dir_at_kill, expr2=False)
                self.assertAlmostEqual(
                    sum(c.args[0] for c in self.sleep.call_args_list), HANDOVER_TIMEOUT
                )
                self.assertEqual(os.listdir(self.temp), [])
                self.assertEqual(self.stderr.getvalue(), "")

    def test_waits_for_a_late_wrapper(self):
        fake = FakeTmux(hand_over=False)
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            if len(slept) == 3:
                os.unlink(fake.path)

        self.sleep = sleep
        self.start(fake)
        self.assertEqual(len(slept), 3)
        self.assertEqual(fake.commands(), ["display-message", "new-session"])
        self.assertEqual(os.listdir(self.temp), [])

    def test_waits_for_a_late_exec(self):
        fake, slept = FakeTmux(wrapper="hang"), []

        def sleep(seconds):
            slept.append(seconds)
            if len(slept) == 3:
                os.close(fake.writer)

        self.sleep = sleep
        self.start(fake)
        self.assertEqual(len(slept), 3)
        self.assertEqual(fake.commands(), ["display-message", "new-session"])

    def test_an_exec_failure_raises_its_error_and_kills_the_session(self):
        fake = FakeTmux(wrapper=errno.ENOEXEC)
        with self.assertRaisesRegex(
            tmux_cli.TuiError,
            "^" + re.escape(f"cannot run {self.tool}: {os.strerror(errno.ENOEXEC)}") + "$",
        ):
            self.start(fake)
        self.assertEqual(fake.commands(), ["display-message", "new-session", "kill-session"])
        self.sleep.assert_not_called()
        self.assertEqual(os.listdir(self.temp), [])

    def test_a_handover_at_its_last_poll_still_gets_the_whole_exec_wait(self):
        fake, slept = FakeTmux(hand_over=False), []
        last = round(HANDOVER_TIMEOUT / POLL)

        def sleep(seconds):
            slept.append(seconds)
            if len(slept) == last:
                fake.writer = os.open(fake.fifo, os.O_WRONLY | os.O_NONBLOCK)
                os.unlink(fake.path)
            elif len(slept) == last + 3:
                os.close(fake.writer)

        self.sleep = sleep
        self.start(fake)
        self.assertEqual(len(slept), last + 3)
        self.assertEqual(fake.commands(), ["display-message", "new-session"])

    def test_bad_errpipe_data_is_an_error_and_kills_the_session(self):
        for data in (b"x", b"9" * 20):
            with self.subTest(data=data):
                fake = FakeTmux(wrapper=data)
                with self.assertRaisesRegex(
                    tmux_cli.TuiError,
                    "^" + re.escape(f"cannot run {self.tool}: bad errpipe data") + "$",
                ):
                    self.start(fake)
                self.assertEqual(
                    fake.commands(), ["display-message", "new-session", "kill-session"]
                )
                self.assertEqual(os.listdir(self.temp), [])

    def test_never_execd_is_not_started(self):
        fake = FakeTmux(wrapper="hang")
        with self.assertRaisesRegex(tmux_cli.TuiError, r"^the session did not start$"):
            self.start(fake)
        os.close(fake.writer)
        self.assertEqual(fake.commands(), ["display-message", "new-session", "kill-session"])
        self.assertAlmostEqual(sum(c.args[0] for c in self.sleep.call_args_list), HANDOVER_TIMEOUT)
        self.assertEqual(os.listdir(self.temp), [])

    def test_unsafe_tmux_argument(self):
        for exe in ("/opt/py#3/bin/python3", "/opt/bin/python3;"):
            with self.subTest(exe=exe), unittest.mock.patch.object(sys, "executable", exe):
                fake = FakeTmux()
                with self.assertRaises(tmux_cli.TuiError):
                    self.start(fake)
                self.assertNotIn("new-session", fake.commands())
        hashed = os.path.join(self.root, "t#mp")
        os.makedirs(hashed)
        fake = FakeTmux()
        with (
            unittest.mock.patch.object(tempfile, "tempdir", hashed),
            self.assertRaises(tmux_cli.TuiError),
        ):
            self.start(fake)
        self.assertNotIn("new-session", fake.commands())
        self.assertEqual(os.listdir(hashed), [])
        self.assertEqual(os.listdir(self.temp), [])

    def test_invalid_session_name(self):
        for name in ("", "a b", "a.b", "a:b", "=a", "#{x}", "a;", "a\n", "é"):
            with self.subTest(name=name):
                fake = FakeTmux()
                for call in (
                    lambda *, fake=fake, name=name: self.start(fake, session=name),
                    lambda *, fake=fake, name=name: tmux_cli.TmuxCli(fake).status(name),
                    lambda *, fake=fake, name=name: TmuxCli(fake, sleep=self.sleep).detach(
                        name, Launch(("tool",), {"PATH": self.bin}, cwd=self.cwd)
                    ),
                    lambda *, fake=fake, name=name: tmux_cli.TmuxCli(fake).kill(name),
                    lambda *, fake=fake, name=name: tmux_cli.TmuxCli(fake).paste(name, "x"),
                    lambda *, fake=fake, name=name: tmux_cli.TmuxCli(fake).keys(name, "Enter"),
                    lambda *, fake=fake, name=name: tmux_cli.TmuxCli(fake).read(name),
                    lambda *, fake=fake, name=name: tmux_cli.TmuxCli(fake).option(name, "@sid"),
                ):
                    with self.assertRaises(tmux_cli.TuiError):
                        call()
                self.assertEqual(fake.calls, [])

    def test_events_and_decorations(self):
        here = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, here)
        events = os.path.join(self.root, "ev")
        # the status line is the caller's choice
        for given, path, status_line, status in (
            (None, None, False, "off"),
            ("ev", events, False, "off"),
            (None, None, True, "on"),
        ):
            with self.subTest(events=given, status_line=status_line):
                fake = FakeTmux()
                self.start(fake, env={"PATH": self.bin}, events=given, status_line=status_line)
                self.assertEqual(fake._handover["argv"], [self.tool, "-x", "a b"])
                self.assertEqual(fake._handover["env"], {"PATH": self.bin, "PWD": self.cwd})
                self.assertEqual(fake.commands(), ["display-message", "new-session"])
                self.assertEqual(
                    fake.calls[1][fake.calls[1].index(fake.fifo) + 8 :],
                    [*chained(decorations("s", path, status)), *recorded("s")],
                )
        self.assertEqual(stat.S_IMODE(os.stat(events).st_mode), 0o600)
