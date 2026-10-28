"""Placement context and iTerm fallback through recorded command transport."""

import io
import os
import re
import tempfile
from contextlib import redirect_stderr

from ensemble.session.engine import tmux_cli
from ensemble.session.viewer import types
from tests.session.case import Case
from tests.session.tmux_fixtures import (
    COMMAND,
    HOSTS,
    INSIDE,
    ITERM,
    LIVE,
    OSA_KW,
    OWN,
    PGREP,
    PREFIXES,
    SESSIONS,
    SH_KW,
    SOCK,
    TMUX,
    TMUX_KW,
    WATCH,
    FakeTmux,
    PaneFixture,
    clients_of,
    environ,
    expected_panes,
    grid_read,
    osa,
    placed,
    record,
    seq,
    split_window,
    which,
)
from tests.utils.process_fixtures import TimeoutExpired


class PaneAnchorTest(PaneFixture):
    def test_anchor_by_iterm_session_id(self):
        for split in ("right", "below"):
            with self.subTest(split=split):
                fake = FakeTmux(results={"osascript": (0, "ok NEW\n")})
                self.assertIsNone(self.open(fake, ITERM, split=split))
                self.assertEqual(
                    fake.calls, [PGREP, osa(split, "id", "s", "ABC"), *record("w0t0p0:ABC", "NEW")]
                )
                self.assertEqual(fake.kwargs, [TMUX_KW, OSA_KW, TMUX_KW, TMUX_KW])
                self.assertEqual(self.stderr.getvalue(), "")
        fake, err = FakeTmux(results={"osascript": (0, "ok NEW")}), io.StringIO()
        with environ(**ITERM), which(), redirect_stderr(err):
            self.assertIsNone(placed(fake).open_pane("s", split="below"))
        self.assertEqual(
            (fake.calls[:2], err.getvalue()), ([PGREP, osa("below", "id", "s", "ABC")], "")
        )

    def test_iterm2_opens_beside_the_ttys_showing_own_session_never_a_control_modes(self):
        tty = ("right", "tty", "s")
        for case, clients, then in (
            (
                "newest tty first",
                self.clients(
                    (100, "/dev/ttys001", "%1"),
                    (300, "/dev/ttys003", "%2"),
                    (200, "/dev/ttys002", "%9"),
                ),
                [HOSTS, PGREP, osa(*tty, "/dev/ttys003", "/dev/ttys002", "/dev/ttys001")],
            ),
            (
                "a control-mode tty skipped",
                self.clients(
                    (300, "/dev/ttys003", "%2"),
                    (200, "/dev/ttys009", "%1", "1"),
                    (100, "/dev/ttys001", "%1"),
                ),
                [HOSTS, PGREP, osa(*tty, "/dev/ttys003", "/dev/ttys001")],
            ),
            ("detached: its iTerm2 session id", (0, ""), [PGREP, osa("right", "id", "s", "ABC")]),
        ):
            with self.subTest(case):
                fake = FakeTmux(
                    results={
                        "display-message": (0, "own\n"),
                        "osascript": (0, "ok NEW\n"),
                        "list-clients": clients,
                    }
                )
                self.assertIsNone(self.open(fake, INSIDE))
                self.assertEqual(
                    fake.calls,
                    [OWN, SESSIONS, clients_of("own"), *then, *record("own", "NEW")],
                )

    def test_tmux_split_of_own_pane_when_no_iterm2_pane_shows_it(self):
        clients = self.clients((5, "/dev/ttys004", "%9"))
        for split, flag in (("right", "-h"), ("below", "-v")):
            for not_iterm in (
                {"pgrep": (1,)},
                {"pgrep": FileNotFoundError(2, "No such file", "pgrep")},
                {"osascript": (0, "no iTerm2 pane shows the anchor\n")},
                {"osascript": (1, "", "syntax error\n")},
            ):
                with self.subTest(split=split, not_iterm=not_iterm):
                    fake = FakeTmux(
                        results={
                            "display-message": (0, "own\n"),
                            "list-clients": clients,
                            "split-window": (0, "%10\n"),
                            **not_iterm,
                        }
                    )
                    self.assertIsNone(self.open(fake, INSIDE, split=split))
                    self.assertEqual(
                        fake.calls[-3:], [split_window(flag, "%3"), *record("own", "%10")]
                    )
                    self.assertEqual(self.stderr.getvalue(), "")

    def test_anchor(self):
        own = {"display-message": (0, "own\n"), "list-clients": (0, "")}
        shown = self.clients((5, "/dev/ttys004", "%9"))
        control = self.clients((5, "/dev/ttys004", "%9", "1"))
        mixed = self.clients((5, "/dev/ttys004", "%9"), (3, "/dev/ttys006", "%8", "1"))
        nested = expected_panes(("0", "/dev/ttys001", "%1"), ("0", "/dev/ttys004", "%5"))
        cases = [
            (ITERM, {}, None, types.ItermAnchor(("id", ["ABC"]))),
            (
                INSIDE,
                {**own, "list-clients": shown},
                None,
                types.ItermAnchor(("tty", ["/dev/ttys004"]), types.TmuxAnchor("%3", SOCK, "own")),
            ),
            (
                INSIDE,
                {**own, "list-clients": shown, "list-panes": nested},
                None,
                types.TmuxAnchor("%5", SOCK, "own"),
            ),
            (INSIDE, own, None, types.ItermAnchor(("id", ["ABC"]))),
            (
                {},
                {"list-clients": shown},
                "b",
                types.ItermAnchor(("tty", ["/dev/ttys004"]), types.TmuxAnchor("%9", SOCK, "b")),
            ),
            (
                {},
                {"list-clients": shown, "list-panes": nested},
                "b",
                types.TmuxAnchor("%5", SOCK, "b"),
            ),
            (
                {},
                {
                    "list-clients": (
                        0,
                        "5 /dev/ttys004\n7 /dev/ttys005 junk 0 /s\n7 /dev/ttys005 %7 /s\n"
                        "7 /dev/ttys005 %7 2 /s\n" + shown[1],
                    )
                },
                "b",
                types.ItermAnchor(("tty", ["/dev/ttys004"]), types.TmuxAnchor("%9", SOCK, "b")),
            ),
            (
                INSIDE,
                {**own, "list-clients": control, "list-panes": nested},
                None,
                types.TmuxAnchor("%3", SOCK, "own"),
            ),
            (
                {},
                {"list-clients": control, "list-panes": nested},
                "b",
                types.TmuxAnchor("%9", SOCK, "b"),
            ),
            (
                {},
                {"list-clients": mixed},
                "b",
                types.ItermAnchor(("tty", ["/dev/ttys004"]), types.TmuxAnchor("%9", SOCK, "b")),
            ),
        ]
        for env, results, split_from, want in cases:
            with self.subTest(env=env, results=results, split_from=split_from), environ(**env):
                self.assertEqual(placed(FakeTmux(results=results)).anchor(split_from), want)
        no_iterm = {k: v for k, v in INSIDE.items() if k != "ITERM_SESSION_ID"}
        cases = [
            ({}, {}, None, "no anchor pane: not in tmux"),
            ({"ITERM_SESSION_ID": "w0t0p0"}, {}, None, "no anchor pane"),
            ({"ITERM_SESSION_ID": "w0t0p0:ABC"}, {}, None, "no anchor pane"),
            (
                {"ITERM_SESSION_ID": "w0t0p0:ABC", "TERM_PROGRAM": "vscode"},
                {},
                None,
                "no anchor pane",
            ),
            (no_iterm, own, None, "no anchor pane: no terminal shows tmux session own"),
            (INSIDE, {"list-clients": (0, "")}, "b", "^no terminal shows tmux session b$"),
        ]
        for env, results, split_from, msg in cases:
            with (
                self.subTest(env=env, split_from=split_from),
                environ(**env),
                self.assertRaisesRegex(tmux_cli.TuiError, msg),
            ):
                placed(FakeTmux(results=results)).anchor(split_from)

    def test_a_dead_pane_sharing_the_clients_tty_is_skipped(self):
        # a dead worker pane keeps its tty, which a later terminal may get
        shown = self.clients((5, "/dev/ttys004", "%9"))
        live, dead = ("0", "/dev/ttys004", "%5"), ("1", "/dev/ttys004", "%6")
        for rows, want in (
            ([live, dead], types.TmuxAnchor("%5", SOCK, "b")),
            (
                [dead],
                types.ItermAnchor(("tty", ["/dev/ttys004"]), types.TmuxAnchor("%9", SOCK, "b")),
            ),
        ):
            with self.subTest(rows=rows), environ():
                fake = FakeTmux(
                    results={"list-clients": shown, "list-panes": expected_panes(*rows)}
                )
                self.assertEqual(placed(fake).anchor("b"), want)

    def test_own_session(self):
        fake = FakeTmux(results={"display-message": (0, "own\n")})
        with environ(**INSIDE):
            self.assertEqual(placed(fake).own_session(), "own")
        self.assertEqual(fake.calls, [OWN])
        fake = FakeTmux()
        with environ():
            self.assertIsNone(placed(fake).own_session())
        self.assertEqual(fake.calls, [])
        with (
            environ(**{**INSIDE, "TMUX_PANE": "%3;"}),
            self.assertRaisesRegex(tmux_cli.TuiError, "TMUX_PANE"),
        ):
            placed(fake).own_session()

    def test_bad_tmux_pane(self):
        for env in (
            {**INSIDE, "TMUX_PANE": pane} for pane in ("", "3", "%3;", "%x", "%3 ", "#{x}")
        ):
            with self.subTest(pane=env["TMUX_PANE"]):
                fake = FakeTmux()
                self.assertRegex(self.open(fake, env), r"TMUX_PANE")
                self.assertEqual(fake.calls, [])
        fake = FakeTmux()
        self.assertRegex(self.open(fake, {"TMUX": "x"}), r"TMUX_PANE")
        self.assertEqual(fake.calls, [])

    def test_own_session_name_checked(self):
        fake = FakeTmux(results={"display-message": (0, "a;\n")})
        self.assertRegex(self.open(fake, INSIDE), r"'a;'")
        self.assertEqual(fake.commands(), ["display-message"])


class OpenerTest(Case):
    def test_in_tmux_its_session(self):
        fake = FakeTmux(results={"display-message": (0, "cmd\n")})
        with environ(**INSIDE, TERM_PROGRAM="iTerm.app"):
            self.assertEqual(placed(fake).opener(), "cmd")
        self.assertEqual(fake.calls, [OWN])

    def test_in_iterm2_the_full_iterm_session_id(self):
        for value in ("w0t0p0:ABC", "w0t1p0:6D7E8F90-1A2B-4C3D-9E8F-0A1B2C3D4E5F"):
            fake = FakeTmux()
            with (
                self.subTest(value=value),
                environ(ITERM_SESSION_ID=value, TERM_PROGRAM="iTerm.app"),
            ):
                self.assertEqual(placed(fake).opener(), value)
            self.assertEqual(fake.calls, [])

    def test_a_bad_iterm_session_id(self):
        for value in (
            "w0t0p0",
            "w0t0p0:",
            ":ABC",
            "w0t0p0:a;b",
            "w0:t0:x",
            "a b:c",
            "w0t0p0:#{x}",
            "w0_t0:x",
        ):
            with (
                self.subTest(value=value),
                environ(ITERM_SESSION_ID=value, TERM_PROGRAM="iTerm.app"),
                self.assertRaisesRegex(tmux_cli.TuiError, r"^bad \$ITERM_SESSION_ID"),
            ):
                placed(FakeTmux()).opener()


class SplitTest(Case):
    def test_sides(self):
        self.assertEqual(list(types.Split), ["right", "below"])
        for split in (None, *types.Split, "below"):
            with self.subTest(split=split), environ(), which():
                self.assertEqual(
                    placed(FakeTmux()).open_pane("s", split=split),
                    "no anchor pane: not in tmux, no iTerm2 pane ($ITERM_SESSION_ID)" + WATCH,
                )
        for bad in ("up", "", "RIGHT", 1, ["right"]):
            fake = FakeTmux()
            with (
                self.subTest(split=bad),
                self.assertRaisesRegex(
                    tmux_cli.TuiError,
                    f"^split must be one of right, below, not {re.escape(repr(bad))}$",
                ),
            ):
                placed(fake).open_pane("s", split=bad)
            self.assertEqual(fake.calls, [])


class RunCommandTest(Case):
    def test_runs_the_template_with_the_session(self):
        fake = FakeTmux()
        self.assertIsNone(placed(fake).run_command("s", "open {{session}} --as {{session}}"))
        self.assertEqual(fake.calls, [["/bin/sh", "-c", "open s --as s"]])
        self.assertEqual(fake.kwargs, [SH_KW])

    def test_session_quoted(self):
        fake = FakeTmux()
        placed(fake).run_command("a;b c", "open {{session}}")
        self.assertEqual(fake.calls, [["/bin/sh", "-c", "open 'a;b c'"]])

    def test_failures_returned(self):
        missing = FileNotFoundError(2, "No such file or directory", "/bin/sh")
        for template, result, why in (
            ("open {{name}} {{session}}", None, "unknown placeholder {{name}}"),
            ("false", (3,), "exit status 3"),
            ("sleep 99", TimeoutExpired("/bin/sh", 30), "timed out after 30 s"),
            ("x", missing, str(missing)),
        ):
            with self.subTest(why=why):
                fake = FakeTmux(results={"/bin/sh": result} if result else {})
                self.assertEqual(placed(fake).run_command("s", template), why)
                self.assertEqual(len(fake.calls), 1 if result else 0)


class WatchTest(Case):
    HOOK = (
        "if-shell -F '#{==:#{hook_client},/dev/pts/4}' "
        f"\"run-shell -b '{COMMAND} hide --if-closed s >/dev/null 2>&1 || true'\""
    )

    UNSET = "set-option -u -t =s: @pane ; set-option -u -t =s: @window"
    STALE = (
        "if-shell -F -t =s: "
        "'#{||:#{!=:#{@pane},%10},#{m:* %10 *,#{S:#{W:#{P: #{pane_id} }}}}}' "
        f"'' \"{UNSET}\""
    )
    SET = [
        *("tmux", "set-option", "-t", "=s:", "@window", "@3"),
        *(";", "set-hook", "-t", "=s:", "client-attached", STALE),
        *(";", "set-hook", "-t", "=s:", "client-session-changed", STALE),
    ]

    def watch(self, pane="%10\n", found="@3 /dev/pts/4\n", **which_kw):
        fake = FakeTmux(results={"show-options": (0, pane), "display-message": (0, found)})
        self.stderr = io.StringIO()
        with environ(), which(**which_kw), redirect_stderr(self.stderr):
            placed(fake).watch("s")
        return fake.calls[1:]

    def test_records_the_panes_window_and_a_hook_for_its_client_only(self):
        self.assertEqual(
            self.watch(),
            [
                ["tmux", "display-message", "-p", "-t", "%10", "#{window_id} #{pane_tty}"],
                [*self.SET, ";", "set-hook", "-t", "=s:", "client-detached", self.HOOK],
            ],
        )
        self.assertEqual(self.stderr.getvalue(), "")

    def test_no_hook_without_a_command_tmux_takes_verbatim(self):
        for session, why in (
            (None, "ensemble-session is not on PATH"),
            ("/a b", "/a b: tmux would misread it"),
        ):
            with self.subTest(why=why):
                calls = self.watch(session=session)
                self.assertEqual(calls[-1], self.SET)
                self.assertEqual(self.stderr.getvalue(), f"tui: show: client-detached: {why}\n")

    def test_nothing_for_an_iterm2_or_unset_pane(self):
        for pane in ("w0t0p0:ABC\n", ""):
            with self.subTest(pane=pane):
                self.assertEqual(self.watch(pane=pane), [])
                self.assertEqual(self.stderr.getvalue(), "")

    def test_a_bad_window_id_or_tty_is_printed_and_sets_nothing(self):
        for found in ("own\n", "@3\n", "@3 /dev/pts/4;x\n", "@3 pts/4\n"):
            with self.subTest(found=found):
                self.assertEqual(len(self.watch(found=found)), 1)
                self.assertEqual(
                    self.stderr.getvalue(),
                    f"tui: show: @window: not a window id and tty: {found.strip()!r}\n",
                )


class ViewerTest(Case):
    """closed and forget over a tmux that keeps @pane and @window, its if-shell -F an equality."""

    def fake(self, pane="%8", window="@2", panes="%1\n%9\n", windows="@1\n@2\n"):
        self.kept = {"@pane": pane, "@window": window}

        def show(argv):
            value = self.kept.get(argv[-1])
            return (0, value + "\n") if value else (1, "", f"invalid option: {argv[-1]}\n")

        def if_shell(argv):
            if argv[5] == f"#{{==:#{{@pane}},{self.kept['@pane']}}}":
                self.kept.update({"@pane": None, "@window": None})
            return (0,)

        return FakeTmux(
            results={
                "show-options": show,
                "list-panes": (0, panes),
                "list-windows": (0, windows),
                "if-shell": if_shell,
            }
        )

    def test_closed_is_a_gone_pane_in_a_present_window(self):
        for pane, window, want in (
            ("%8", "@2", "%8"),
            ("%9", "@2", None),
            ("%8", "@3", None),
            (None, "@2", None),
            ("%8", None, None),
            ("w0t0p0:ABC", "@2", None),
            ("%8", "2", None),
        ):
            with self.subTest(pane=pane, window=window):
                self.assertEqual(placed(self.fake(pane, window)).closed("s"), want)

    def test_forget_unsets_only_while_the_pane_is_still_recorded(self):
        unset = "set-option -u -t =s: @pane ; set-option -u -t =s: @window"
        for recorded, given, want in (("%8", "%8", True), ("%10", "%8", False), ("%8", None, True)):
            with self.subTest(recorded=recorded, given=given):
                fake = self.fake(pane=recorded)
                self.assertEqual(placed(fake).forget("s", given), want)
                self.assertIn(
                    [
                        "tmux",
                        "if-shell",
                        "-F",
                        "-t",
                        "=s:",
                        f"#{{==:#{{@pane}},{given or recorded}}}",
                        unset,
                    ],
                    fake.calls,
                )
                self.assertEqual(self.kept["@pane"], None if want else recorded)

    def test_forget_leaves_an_iterm2_or_unset_pane(self):
        for pane in ("w0t0p0:ABC", None):
            with self.subTest(pane=pane):
                fake = self.fake(pane=pane)
                self.assertFalse(placed(fake).forget("s"))
                self.assertNotIn("if-shell", fake.commands())


class ContainerTest(Case):
    """In a Linux container on a Mac: the caller in tmux session `cmd`, pane %0 (tty /dev/pts/0),
    shown by a client on /dev/pts/1 (iTerm2's tmux -CC through docker exec -it, control mode; else a
    plain docker exec -it tmux attach); no iTerm2 and no iTerm2 variables. pgrep finds nothing:
    procps, busybox's usage error, or none installed.
    """

    SOCK = "/tmp/tmux-0/default"

    ENV = {"TMUX": f"{SOCK},42,0", "TMUX_PANE": "%0", "PATH": "/usr/local/bin:/usr/bin:/bin"}

    OWN = ["tmux", "display-message", "-p", "-t", "%0", "#{session_name}"]

    PGREPS = (
        (1, "", ""),
        (1, "", "BusyBox v1.36.1 multi-call binary.\n\nUsage: pgrep [-flanovx] PATTERN\n"),
        (2, "", "pgrep: invalid option -- 'a'\n"),
        FileNotFoundError(2, "No such file or directory", "pgrep"),
    )

    @classmethod
    def fake(cls, pgrep, rows=(), control="1"):
        def list_panes(argv):
            if argv[-1] == "#{pane_id}":
                return 0, "%0\n%5\n"
            return expected_panes(("0", "/dev/pts/0", "%0"), ("0", "/dev/pts/2", "%5"))(argv)

        return FakeTmux(
            results={
                "display-message": (0, "cmd\n"),
                "list-panes": list_panes,
                "pgrep": pgrep,
                "list-sessions": (0, "".join("\t".join(r) + "\n" for r in rows)),
                "list-clients": (0, f"100 /dev/pts/1 %0 {control} {cls.SOCK}\n"),
                "split-window": (0, "%6\n"),
            }
        )

    def open(self, fake, env=ENV):
        self.stderr = io.StringIO()
        with environ(**env), which(), redirect_stderr(self.stderr):
            return placed(fake).open_pane("s")

    def test_first_pane_a_tmux_split_right_of_the_callers(self):
        fake = self.fake(self.PGREPS[0])
        self.assertIsNone(self.open(fake))
        self.assertEqual(
            fake.calls,
            [
                self.OWN,
                SESSIONS,
                clients_of("cmd"),
                split_window("-h", "%0", self.SOCK),
                *record("cmd", "%6"),
            ],
        )
        self.assertEqual(self.stderr.getvalue(), "")
        for pgrep in self.PGREPS:
            with self.subTest(pgrep=pgrep):
                fake = self.fake(pgrep, control="0")
                self.assertIsNone(self.open(fake))
                self.assertEqual(
                    fake.calls,
                    [
                        self.OWN,
                        SESSIONS,
                        clients_of("cmd"),
                        HOSTS,
                        PGREP,
                        split_window("-h", "%0", self.SOCK),
                        *record("cmd", "%6"),
                    ],
                )
                self.assertEqual(self.stderr.getvalue(), "")

    def test_a_second_pane_stacks_below_the_first(self):
        fake = self.fake(self.PGREPS[0], [("$1", "w1", "cmd", "%5", self.SOCK)])
        self.assertIsNone(self.open(fake))
        self.assertEqual(
            fake.calls,
            [self.OWN, SESSIONS, LIVE, split_window("-v", "%5", self.SOCK), *record("cmd", "%6")],
        )
        self.assertEqual(self.stderr.getvalue(), "")

    def test_no_client_no_pane_and_the_prefixed_attach_command(self):
        fake = self.fake(self.PGREPS[0])
        fake.results["list-clients"] = (0, "")
        why = self.open(fake, {**self.ENV, "TUI_ATTACH_PREFIX": "docker exec -it box"})
        self.assertTrue(why.startswith("no anchor pane: no terminal shows tmux session cmd"), why)
        self.assertTrue(
            why.endswith("; watch it with docker exec -it box tmux attach -t '=s'"), why
        )
        self.assertNotIn("split-window", fake.commands())
        self.assertNotIn("osascript", fake.commands())


class ShownOnceTest(PaneFixture):
    """open_pane opens no second pane while one its opener opened still shows the session."""

    def fake(self, pane, opener="own", live="", clients=(0, "")):
        return FakeTmux(
            results={
                "display-message": (0, "own\n"),
                "list-sessions": (0, f"$1\ts\t{opener}\t{pane}\t{SOCK}\n"),
                "list-panes": (0, live),
                "list-clients": clients,
                "pgrep": (1,),
                "split-window": (0, "%10\n"),
            }
        )

    def test_a_session_its_opener_already_shows_gets_no_second_pane(self):
        attached = self.clients((1, "/dev/ttys009", "%5"))
        for fake, check in (
            (self.fake("%5", live="%2\n%5\n"), LIVE),
            (self.fake("ABC-1", clients=attached), clients_of("s")),
        ):
            with self.subTest(check=check):
                for _ in range(3):
                    self.assertIsNone(self.open(fake, INSIDE))
                    self.assertEqual(self.stderr.getvalue(), "")
                self.assertEqual(fake.calls, [OWN, SESSIONS, check] * 3)

    def test_a_pane_opens_once_none_shows_the_session_or_for_another_opener(self):
        own = self.clients((5, "/dev/ttys004", "%9"))
        for fake in (
            self.fake("%5", live="%2\n", clients=own),
            self.fake("ABC-1", clients=seq((0, ""), own)),
            self.fake("%5", opener="other", live="%5\n", clients=own),
            self.fake("", live="%5\n", clients=own),
        ):
            with self.subTest(calls=fake.calls):
                self.assertIsNone(self.open(fake, INSIDE))
                self.assertEqual(fake.calls[-3:], [split_window("-h", "%3"), *record("own", "%10")])


class PanePlacementTest(PaneFixture):
    def test_iterm2_pane_split_from(self):
        fake = FakeTmux(
            results={
                "display-message": (0, "own\n"),
                "list-clients": self.clients((5, "/dev/ttys004", "%9")),
                "osascript": (0, "ok NEW\n"),
            }
        )
        self.assertIsNone(self.open(fake, INSIDE, split_from="b", split="below"))
        self.assertEqual(
            fake.calls,
            [
                OWN,
                SESSIONS,
                clients_of("b"),
                HOSTS,
                PGREP,
                osa("below", "tty", "s", "/dev/ttys004"),
                *record("own", "NEW"),
            ],
        )

    def test_tmux_split_split_from(self):
        clients = self.clients((5, "/dev/ttys004", "%9"), (9, "/dev/ttys005", "%7"))
        fake = FakeTmux(
            results={"list-clients": clients, "pgrep": (1,), "split-window": (0, "%10\n")}
        )
        self.assertIsNone(self.open(fake, {}, split_from="b", split="below"))
        self.assertEqual(fake.calls[-2:], [split_window("-v", "%7"), *record(None, "%10")])
        nested = FakeTmux(
            results={
                "list-clients": clients,
                "list-panes": expected_panes(("0", "/dev/ttys005", "%5")),
                "split-window": (0, "%10\n"),
            }
        )
        self.assertIsNone(self.open(nested, {}, split_from="b"))
        self.assertEqual(
            nested.calls, [clients_of("b"), HOSTS, split_window("-h", "%5"), *record(None, "%10")]
        )

    def test_control_mode_client_gets_a_tmux_split_of_own_pane(self):
        # iTerm2's tmux -CC: the control-mode client's tty is the hidden gateway tab's; iTerm2 draws
        # the tmux split
        clients = self.clients((300, "/dev/ttys009", "%1", "1"), (100, "/dev/ttys001", "%2"))
        for split, flag in ((None, "-h"), ("right", "-h"), ("below", "-v")):
            with self.subTest(split=split):
                fake = FakeTmux(
                    results={
                        "display-message": (0, "own\n"),
                        "list-clients": clients,
                        "split-window": (0, "%10\n"),
                    }
                )
                self.assertIsNone(self.open(fake, INSIDE, split=split))
                self.assertEqual(
                    fake.calls,
                    [
                        OWN,
                        SESSIONS,
                        clients_of("own"),
                        split_window(flag, "%3"),
                        *record("own", "%10"),
                    ],
                )
                self.assertEqual(self.stderr.getvalue(), "")

    def test_split_from_a_session_only_a_control_mode_client_shows(self):
        clients = self.clients((5, "/dev/ttys009", "%9", "1"))
        for env, head, opener in (({}, [], None), (INSIDE, [OWN, SESSIONS], "own")):
            with self.subTest(env=env):
                fake = FakeTmux(
                    results={
                        "display-message": (0, "own\n"),
                        "list-clients": clients,
                        "split-window": (0, "%10\n"),
                    }
                )
                self.assertIsNone(self.open(fake, env, split_from="b", split="below"))
                self.assertEqual(
                    fake.calls,
                    [*head, clients_of("b"), split_window("-v", "%9"), *record(opener, "%10")],
                )

    def test_tmux_split_failures(self):
        clients = self.clients((5, "/dev/ttys004", "%9"))
        fake = FakeTmux(
            results={
                "list-clients": clients,
                "pgrep": (1,),
                "split-window": (1, "", "no space for new pane\n"),
            }
        )
        self.assertEqual(self.open(fake, {}, split_from="b"), "tmux: no space for new pane")
        self.assertNotIn("set-option", fake.commands())
        fake = FakeTmux(
            results={"list-clients": (0, "5 /dev/ttys004 %9 0 /tmp/#s\n"), "pgrep": (1,)}
        )
        self.assertEqual(
            self.open(fake, {}, split_from="b"), "tmux would misread '/tmp/#s'" + WATCH
        )
        self.assertNotIn("split-window", fake.commands())

    def test_split_from_not_shown_anywhere(self):
        for result in ((0, ""), (1, "", "can't find session: =b\n")):
            with self.subTest(result=result):
                fake = FakeTmux(results={"list-clients": result})
                self.assertTrue(self.open(fake, {}, split_from="b").endswith(WATCH))
                self.assertEqual(fake.commands(), ["list-clients"])

    def test_bad_arguments_raise(self):
        for kw in (
            {"split_from": "a b"},
            {"split_from": "=b"},
            {"split": "up"},
            {"split": "vertically"},
            {"split": ""},
            {"opener": "a b"},
            {"opener": "a:b:c"},
        ):
            with self.subTest(**kw):
                fake = FakeTmux()
                with self.assertRaises(tmux_cli.TuiError):
                    self.open(fake, ITERM, **kw)
                self.assertEqual(fake.calls, [])

    def test_no_anchor_refuses_with_the_attach_command(self):
        for env in (
            {},
            {"ITERM_SESSION_ID": "w0t0p0"},
            {"ITERM_SESSION_ID": ""},
            {"ITERM_SESSION_ID": "w0t0p0:ABC"},
            {**ITERM, "TERM_PROGRAM": "Apple_Terminal"},
            {"TERM_PROGRAM": "iTerm.app"},
            {"TERM_PROGRAM": "iTerm.app", "ITERM_SESSION_ID": ""},
        ):
            for split in (None, "right"):
                with self.subTest(env=env, split=split):
                    fake = FakeTmux()
                    why = self.open(fake, env, split=split)
                    self.assertRegex(
                        why,
                        r"^no anchor pane: not in tmux, no iTerm2 pane \(\$ITERM_SESSION_ID\)"
                        + WATCH
                        + "$",
                    )
                    self.assertEqual(fake.calls, [])
                    self.assertEqual(self.stderr.getvalue(), "")

    def test_the_watch_command_takes_the_prefix(self):
        for env, prefix in PREFIXES:
            with self.subTest(env=env):
                why = self.open(FakeTmux(), env)
                self.assertTrue(why.endswith(f"; watch it with {prefix}tmux attach -t '=s'"), why)

    def test_no_tmux(self):
        fake = FakeTmux()
        self.assertEqual(self.open(fake, ITERM, tmux=None), "tmux not found")
        self.assertEqual(fake.calls, [])

    def test_tmux_path_made_absolute(self):
        here = os.getcwd()
        os.chdir(tempfile.gettempdir())
        self.addCleanup(os.chdir, here)
        fake = FakeTmux(results={"osascript": (0, "ok NEW\n")})
        self.open(fake, ITERM, tmux="bin/tmux")
        self.assertEqual(fake.calls[2][5], os.path.join(os.getcwd(), "bin/tmux"))

    def test_iterm_session_id_failures_refuse(self):
        missing = FileNotFoundError(2, "No such file or directory", "osascript")
        for results, why in (
            ({"pgrep": (1,)}, "iTerm2 is not running"),
            ({"osascript": missing}, f"osascript: {missing}"),
            (
                {"osascript": TimeoutExpired("osascript", 30)},
                "osascript timed out after 30 s",
            ),
            (
                {"osascript": (1, "", "execution error: boom (-1708)\n")},
                "osascript: execution error: boom (-1708)",
            ),
            (
                {"osascript": (0, "no iTerm2 pane shows the anchor\n")},
                "no iTerm2 pane shows the anchor",
            ),
        ):
            with self.subTest(why=why):
                fake = FakeTmux(results=results)
                self.assertEqual(self.open(fake, ITERM), why + WATCH)
                self.assertEqual(self.stderr.getvalue(), "")
                self.assertNotIn("split-window", fake.commands())
                self.assertNotIn("set-option", fake.commands())

    def test_script_is_fixed_and_takes_its_values_as_arguments(self):
        fake = FakeTmux(results={"osascript": (0, "ok NEW\n")})
        self.assertIsNone(self.open(fake, ITERM))
        osascript, flag, script, *values = fake.calls[fake.commands().index("osascript")]
        self.assertEqual(
            (osascript, flag, values), ("osascript", "-e", ["right", "id", TMUX, "s", "ABC"])
        )
        self.assertNotIn(TMUX, script)
        for part in (
            "on run argv",
            'if application "iTerm2" is running',
            'tell application "iTerm2"',
            "quoted form of tmuxPath",
            'quoted form of ("=" & sessionName)',
            "split vertically with default profile command",
            "split horizontally with default profile command",
            "unique id of s",
            "tty of s",
            'return "ok " & (unique id of newSession)',
        ):
            self.assertIn(part, script)
        # its panes end with their tmux session
        self.assertEqual(script.count("with default profile command paneCommand"), 2)
        for word in ("write text", "close"):
            self.assertNotIn(word, script)


class StackTest(Case):
    """Panes stack by opener. The caller is in tmux session `own`, pane %3, shown by a terminal
    client (tty /dev/ttys004) whose pane is %9; list-sessions prints `rows`, list-panes -a the
    `live` pane ids (`hosts`, pane_ids() rows, for the tty format); pgrep finds no iTerm2 unless told;
    a tmux split prints %99.
    """

    NONE = (0, "no iTerm2 pane shows the anchor\n")

    def fake(
        self,
        rows=(),
        live=(),
        own="cmd",
        clients=f"5 /dev/ttys004 %9 0 {SOCK}\n",
        hosts=(),
        **results,
    ):
        def list_panes(argv):
            if "-a" not in argv:  # a grid read: a window with no grid
                return 0, f"@1\t\t{argv[3]}\t80\t24\t{SOCK}\n"
            return (
                (0, "".join(p + "\n" for p in live))
                if argv[-1] == "#{pane_id}"
                else expected_panes(*hosts)(argv)
            )

        return FakeTmux(
            results={
                "display-message": (0, own + "\n"),
                "list-sessions": (0, "".join("\t".join(r) + "\n" for r in rows)),
                "list-panes": list_panes,
                "list-clients": (0, clients),
                "pgrep": (1,),
                "split-window": (0, "%99\n"),
                **results,
            }
        )

    def open(self, fake, env=INSIDE, **kw):
        self.stderr = io.StringIO()
        with environ(**env), which(), redirect_stderr(self.stderr):
            return placed(fake).open_pane("s", **kw)

    def expected_row(self, number, name, pane, opener="cmd"):
        return f"${number}", name, opener, pane, SOCK

    def test_first_pane_right_of_the_opener(self):
        for results in ({}, {"list-sessions": (1, "", "no server running\n")}):
            with self.subTest(results=results):
                fake = self.fake(**results)
                self.assertIsNone(self.open(fake))
                self.assertEqual(
                    fake.calls,
                    [
                        OWN,
                        SESSIONS,
                        clients_of("cmd"),
                        HOSTS,
                        PGREP,
                        split_window("-h", "%3"),
                        *record("cmd", "%99"),
                    ],
                )
                self.assertEqual(self.stderr.getvalue(), "")
        fake = self.fake(pgrep=(0,), osascript=(0, "ok NEW\n"))
        self.assertIsNone(self.open(fake, ITERM))
        self.assertEqual(
            fake.calls,
            [SESSIONS, PGREP, osa("right", "id", "s", "ABC"), *record("w0t0p0:ABC", "NEW")],
        )

    def test_below_the_newest_live_pane_else_right(self):
        # the second below the first; the newest closed: below the previous live one; none live:
        # right
        three = [
            self.expected_row(9, "a", "%9"),
            self.expected_row(11, "c", "%11"),
            self.expected_row(10, "b", "%10"),
        ]
        right = [clients_of("cmd"), HOSTS, PGREP, split_window("-h", "%3")]
        for rows, live, then in (
            ([self.expected_row(1, "a", "%7")], ["%3", "%7", "%9"], [split_window("-v", "%7")]),
            (three, ["%9", "%10", "%11"], [split_window("-v", "%11")]),
            (three, ["%9", "%10"], [split_window("-v", "%10")]),
            (three, ["%9"], [split_window("-v", "%9")]),
            ([self.expected_row(1, "a", "%7")], ["%3", "%9"], right),
        ):
            with self.subTest(rows=rows, live=live):
                fake = self.fake(rows, live)
                self.assertIsNone(self.open(fake))
                self.assertEqual(fake.calls, [OWN, SESSIONS, LIVE, *then, *record("cmd", "%99")])

    def test_tmux_liveness_is_an_exact_line(self):
        fake = self.fake(
            [self.expected_row(1, "a", "%1")], live=["%10", " %1", "%1 ", "x%1", "%1\r"]
        )
        self.assertIsNone(self.open(fake))
        self.assertEqual(
            fake.calls[2:],
            [
                LIVE,
                clients_of("cmd"),
                HOSTS,
                PGREP,
                split_window("-h", "%3"),
                *record("cmd", "%99"),
            ],
        )

    def test_a_worker_gets_its_own_column(self):
        stack = [
            self.expected_row(1, "a", "%7"),
            self.expected_row(2, "W", "%8"),
        ]  # the commander's (cmd); W's pane is the newest
        live = ["%3", "%7", "%8"]
        fake = self.fake(stack, live, own="W", pgrep=(0,), osascript=(0, "ok NEW\n"))
        self.assertIsNone(self.open(fake))
        self.assertEqual(
            fake.calls,
            [
                OWN,
                SESSIONS,
                grid_read("%8"),
                clients_of("W"),
                HOSTS,
                PGREP,
                osa("right", "tty", "s", "/dev/ttys004"),
                *record("W", "NEW"),
            ],
        )
        # W shown by a nested client in the commander's pane %8: that pane splits, never one in W's
        # window
        fake = self.fake(
            stack, live, own="W", hosts=[("0", "/dev/ttys001", "%7"), ("0", "/dev/ttys004", "%8")]
        )
        self.assertIsNone(self.open(fake))
        self.assertEqual(
            fake.calls,
            [
                OWN,
                SESSIONS,
                grid_read("%8"),
                clients_of("W"),
                HOSTS,
                split_window("-h", "%8"),
                *record("W", "%99"),
            ],
        )
        fake = self.fake([*stack, self.expected_row(3, "w2", "%20", "W")], [*live, "%20"], own="W")
        self.assertIsNone(self.open(fake))
        self.assertEqual(
            fake.calls,
            [OWN, SESSIONS, grid_read("%8"), LIVE, split_window("-v", "%20"), *record("W", "%99")],
        )

    def test_explicit_split_or_split_from_overrides_the_stack(self):
        for kw, (session, flag, pane) in (
            ({"split": "right"}, ("cmd", "-h", "%3")),
            ({"split": "below"}, ("cmd", "-v", "%3")),
            ({"split_from": "b"}, ("b", "-h", "%9")),
            ({"split_from": "b", "split": "below"}, ("b", "-v", "%9")),
        ):
            with self.subTest(**kw):
                fake = self.fake([self.expected_row(1, "a", "%7")], live=["%3", "%7"])
                self.assertIsNone(self.open(fake, **kw))
                self.assertEqual(
                    fake.calls,
                    [
                        OWN,
                        SESSIONS,
                        clients_of(session),
                        HOSTS,
                        PGREP,
                        split_window(flag, pane),
                        *record("cmd", "%99"),
                    ],
                )

    def test_iterm2_panes_stack_too(self):
        opener = ITERM["ITERM_SESSION_ID"]
        rows = [
            self.expected_row(4, "d", "ID4", opener),
            self.expected_row(3, "c", "%5", opener),
            self.expected_row(2, "b", "ID2", opener),
            self.expected_row(1, "a", "ID1", opener),
        ]
        tried = [
            [PGREP, osa("below", "id", "s", "ID4")],
            [LIVE, PGREP, osa("below", "id", "s", "ID2", "ID1")],
            [PGREP, osa("right", "id", "s", "ABC")],
        ]
        for n in (1, 2, 3):
            with self.subTest(osascript_calls=n):
                fake = self.fake(
                    rows, pgrep=(0,), osascript=seq(*[self.NONE] * (n - 1), (0, "ok NEW\n"))
                )
                self.assertIsNone(self.open(fake, ITERM))
                self.assertEqual(
                    fake.calls,
                    [SESSIONS, *(c for t in tried[:n] for c in t), *record(opener, "NEW")],
                )
        fake = self.fake(rows, live=["%5"], pgrep=(0,), osascript=self.NONE)
        self.assertIsNone(self.open(fake, ITERM))
        self.assertEqual(
            fake.calls,
            [SESSIONS, *tried[0], LIVE, split_window("-v", "%5"), *record(opener, "%99")],
        )
        fake = self.fake(rows)
        self.assertEqual(self.open(fake, ITERM), "iTerm2 is not running" + WATCH)
        self.assertEqual(fake.calls, [SESSIONS, PGREP, LIVE, PGREP, PGREP])

    def test_applescript_output(self):
        for out, pane in (
            ("ok NEW\n", "NEW"),
            ("ok 6D7E8F90-1A2B\n", "6D7E8F90-1A2B"),
            ("ok 6D7E-8F\n", "6D7E-8F"),
        ):
            with self.subTest(out=out):
                fake = self.fake(pgrep=(0,), osascript=(0, out))
                self.assertIsNone(self.open(fake, ITERM))
                self.assertEqual(fake.calls[-2:], record("w0t0p0:ABC", pane))
        for out, why in (
            ("ok\n", "ok"),
            ("ok \n", "ok"),
            ("ok a b\n", "ok a b"),
            ("ok a;b\n", "ok a;b"),
            ("ok a_b\n", "ok a_b"),
            ("ok a:b\n", "ok a:b"),
            ("ok %1\n", "ok %1"),
            ("OK ABC\n", "OK ABC"),
            ("ok A\nok B\n", "ok A\nok B"),
            ("", "osascript printed nothing"),
        ):
            with self.subTest(out=out):
                fake = self.fake(pgrep=(0,), osascript=(0, out))
                self.assertEqual(self.open(fake, ITERM), why + WATCH)
                self.assertNotIn("set-option", fake.commands())

    def test_options_only_after_a_split(self):
        fake = self.fake()
        self.assertIsNone(placed(fake).run_command("s", "open {{session}}"))
        self.assertEqual(fake.calls, [["/bin/sh", "-c", "open s"]])
        fake = self.fake(**{"split-window": (1, "", "no space for new pane\n")})
        self.assertEqual(self.open(fake), "tmux: no space for new pane")
        self.assertNotIn("set-option", fake.commands())

    def test_a_failure_to_record_is_printed(self):
        for fail, calls in (("@opener", 1), ("@pane", 2)):
            with self.subTest(fail=fail):
                fake = self.fake(
                    **{
                        "set-option": lambda argv, *, fail=fail: (
                            (1, "", "boom\n") if fail in argv else (0,)
                        )
                    }
                )
                self.assertIsNone(self.open(fake))
                self.assertEqual(fake.commands().count("set-option"), calls)
                self.assertEqual(self.stderr.getvalue(), f"tui: show: {fail}: tmux: boom\n")
        fake = self.fake(**{"split-window": (0, "\n")})
        self.assertIsNone(self.open(fake))
        self.assertNotIn("set-option", fake.commands())
        self.assertEqual(self.stderr.getvalue(), "tui: show: @pane: not a pane id: ''\n")

    def test_read_back_values_must_fullmatch(self):
        bad = [
            ("$9", "a", "cmd", "%5;", SOCK),
            ("$8", "b", "cmd", "#{x}", SOCK),
            ("$7", "c", "cmd", "I D", SOCK),
            ("$7", "d", "cmd", "ID;x", SOCK),
            ("$6", "e", "cmd ", "%6", SOCK),
            ("$6", "f", "other", "%6", SOCK),
            ("$6", "g", "c:md:x", "%6", SOCK),
            ("5", "h", "cmd", "%5", SOCK),
            ("$5x", "i", "cmd", "%5", SOCK),
            ("$4", "j", "cmd", "", SOCK),
            ("$4", "k", "cmd", "%4"),
            ("$4", "l", "cmd", "%4", SOCK, "x"),
        ]
        live = ["%2", "%4", "%5", "%6"]
        fake = self.fake(bad, live)
        self.assertIsNone(self.open(fake))
        self.assertEqual(
            fake.calls,
            [
                OWN,
                SESSIONS,
                clients_of("cmd"),
                HOSTS,
                PGREP,
                split_window("-h", "%3"),
                *record("cmd", "%99"),
            ],
        )
        fake = self.fake([*bad, self.expected_row(1, "m", "%2")], live)
        self.assertIsNone(self.open(fake))
        self.assertEqual(
            fake.calls, [OWN, SESSIONS, LIVE, split_window("-v", "%2"), *record("cmd", "%99")]
        )

    def test_a_passed_opener(self):
        fake = self.fake(pgrep=(0,), osascript=(0, "ok NEW\n"))
        self.assertIsNone(self.open(fake, opener="w0t1p0:XYZ"))
        self.assertEqual(
            fake.calls,
            [SESSIONS, PGREP, osa("right", "id", "s", "XYZ"), *record("w0t1p0:XYZ", "NEW")],
        )
        fake = self.fake(own="drv")
        self.assertIsNone(self.open(fake, opener="outer"))
        self.assertEqual(
            fake.calls,
            [
                SESSIONS,
                OWN,
                clients_of("outer"),
                HOSTS,
                PGREP,
                split_window("-h", "%9"),
                *record("outer", "%99"),
            ],
        )
        fake = self.fake()
        self.assertIsNone(self.open(fake, ITERM, opener="outer"))
        self.assertEqual(
            fake.calls,
            [
                SESSIONS,
                clients_of("outer"),
                HOSTS,
                PGREP,
                split_window("-h", "%9"),
                *record("outer", "%99"),
            ],
        )
        fake = self.fake(own="outer")
        self.assertIsNone(self.open(fake, opener="outer"))
        self.assertEqual(fake.calls[-3:], [split_window("-h", "%3"), *record("outer", "%99")])
        fake = self.fake(own="drv", clients="", pgrep=(0,), osascript=(0, "ok NEW\n"))
        self.assertIsNone(self.open(fake, opener="outer"))
        self.assertEqual(
            fake.calls,
            [
                SESSIONS,
                OWN,
                clients_of("outer"),
                PGREP,
                osa("right", "id", "s", "ABC"),
                *record("outer", "NEW"),
            ],
        )
        fake = self.fake(
            [self.expected_row(2, "b", "%8"), self.expected_row(1, "a", "%7", "outer")],
            ["%7", "%8"],
            own="drv",
        )
        self.assertIsNone(self.open(fake, opener="outer"))
        self.assertEqual(
            fake.calls, [SESSIONS, LIVE, split_window("-v", "%7"), *record("outer", "%99")]
        )
        no_iterm = {k: v for k, v in INSIDE.items() if k != "ITERM_SESSION_ID"}
        fake = self.fake(own="drv", clients="")
        self.assertEqual(
            self.open(fake, no_iterm, opener="outer"),
            "no anchor pane: no terminal shows tmux session "
            "outer, no iTerm2 pane ($ITERM_SESSION_ID)" + WATCH,
        )

    def test_a_bad_opener_raises(self):
        for opener in (
            "",
            "a b",
            "w0t0p0:",
            ":ABC",
            "a:b:c",
            "a;b",
            "w0t0p0:A_B",
            "=a",
            "%3",
            "a\n",
            "a:b\n",
            "a:",
            ":b",
            "a_b:c",
            "a:b_c",
            "a;",
            "%1",
        ):
            with self.subTest(opener=opener):
                fake = self.fake()
                with self.assertRaisesRegex(tmux_cli.TuiError, "opener"):
                    self.open(fake, opener=opener)
                self.assertEqual(fake.calls, [])
