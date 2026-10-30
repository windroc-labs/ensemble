"""Tmux worker pane placement and caller context through recorded command transport."""

import io
import re
from contextlib import redirect_stderr

from ensemble.session.engine import tmux_cli
from ensemble.session.viewer import types
from tests.session.case import Case
from tests.session.tmux_fixtures import (
    ARGV,
    COMMAND,
    INSIDE,
    OWN,
    PRINTED,
    SH_KW,
    WATCH,
    WORKERS,
    FakeTmux,
    GridFixture,
    PaneFixture,
    clients,
    clients_of,
    close_hook,
    environ,
    grid_read,
    grid_set,
    mute,
    opening_commands,
    placed,
    record,
    split,
    split_window,
    which,
    worker_rows,
)
from tests.utils.process_fixtures import TimeoutExpired

SID = "0f8fad5b-d9cb-469f-a165-70867728950e"


class PaneAnchorTest(PaneFixture):
    def test_anchor(self):
        # the most recently active client's pane; rows tmux could not have printed are skipped
        shown = (0, "5\t%8\n9\t%9\nx\t%1\n12\t%2;\n15\n7\t%7\n")
        for env, split_from, pane, calls in (
            (INSIDE, None, "%3", [OWN, clients_of("own")]),
            ({}, "b", "%9", [WORKERS, clients_of("b")]),
        ):
            with self.subTest(env=env, split_from=split_from), environ(**env):
                fake = FakeTmux(results={"display-message": (0, "own\n"), "list-clients": shown})
                self.assertEqual(placed(fake).anchor(split_from), pane)
                self.assertEqual(fake.calls, calls)
        for env, split_from, message in (
            ({}, None, "^no anchor pane: not in tmux$"),
            (INSIDE, None, "^no anchor pane: no terminal shows tmux session own$"),
            ({}, "b", "^no terminal shows b$"),
        ):
            with (
                self.subTest(env=env),
                environ(**env),
                self.assertRaisesRegex(tmux_cli.TuiError, message),
            ):
                placed(FakeTmux(results={"display-message": (0, "own\n")})).anchor(split_from)

    def test_split_from_a_worker_anchors_at_its_pane(self):
        """AC3: --split-from X anchors at X's newest worker pane, asking no client."""
        rows = worker_rows(("%5", "b", "mgr"), ("%12", "b", "mgr"), ("%30", "c", "b"))
        fake = FakeTmux(results={"list-panes": rows})
        with environ():
            self.assertEqual(placed(fake).anchor("b"), "%12")
        self.assertEqual(fake.calls, [WORKERS])
        for split_kw, flag in (({}, "-h"), ({"split": "below"}, "-v")):
            with self.subTest(**split_kw):
                fake = FakeTmux(results={"list-panes": rows, "split-window": split()})
                self.assertIsNone(self.open(fake, {}, split_from="b", **split_kw))
                self.assertEqual(
                    fake.calls,
                    [WORKERS, WORKERS, split_window(flag, "%12"), *record(None, "%20")],
                )
                self.assertNotIn("list-clients", fake.commands())

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
        with environ(**INSIDE):
            self.assertEqual(placed(fake).opener(), "cmd")
        self.assertEqual(fake.calls, [OWN])


class SplitTest(Case):
    def test_sides(self):
        self.assertEqual(list(types.Split), ["right", "below"])
        for split_ in (None, *types.Split, "below"):
            with self.subTest(split=split_), environ(), which():
                self.assertEqual(
                    placed(FakeTmux()).open_pane("s", ARGV, PRINTED, split=split_),
                    "no anchor pane: not in tmux" + WATCH,
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
                placed(fake).open_pane("s", ARGV, PRINTED, split=bad)
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


class RecordTest(Case):
    """_record tags a new pane %20 of window @1 as worker w1's."""

    def record(self, *, name="w1", opener="mgr", pane="%20", window="@1", fake=None, **kw):
        fake = fake or FakeTmux()
        self.stderr = io.StringIO()
        with which(kw.pop("command", COMMAND)), redirect_stderr(self.stderr):
            placed(fake)._record(name, opener, pane, window, **{"sid": None, "label": False, **kw})
        return fake.calls

    def test_record_sets_pane_options_and_close_hook(self):
        """AC6: one call sets @worker, @worker-opener and the window's after-kill-pane[N] hook,
        which once %20 is gone unsets itself and hides run S of w1 as closed by hand.
        """
        hook = (
            "if-shell -F '#{m:* %20 *,#{S:#{W:#{P: #{pane_id} }}}}' '' "
            '"set-hook -u -w -t @1 after-kill-pane[20] ; '
            "run-shell -b '/opt/a_b.c@1+2-3/bin/ensemble-session hide --if-closed --sid S -- w1 "
            ">/dev/null 2>&1 || true'\""
        )
        self.assertEqual(
            self.record(sid="S"),
            [
                [
                    *("tmux", "set-option", "-p", "-t", "%20", "@worker", "w1"),
                    *(";", "set-option", "-p", "-t", "%20", "@worker-opener", "mgr"),
                    *(";", "set-hook", "-w", "-t", "@1", "after-kill-pane[20]", hook),
                ]
            ],
        )
        self.assertEqual(self.stderr.getvalue(), "")
        self.assertEqual(self.record(opener=None, sid="S"), record(None, "%20", name="w1", sid="S"))

    def test_close_hook_carries_the_sid(self):
        """AC8 (D2): the hook names the run's sid, so it marks no later run of the name; a sid
        outside HOOK_SID, or none, gets the name-only hook.
        """
        for sid in (SID, "a.b:c_d-1"):
            with self.subTest(sid=sid):
                hook = self.record(sid=sid)[0][-1]
                self.assertEqual(hook, close_hook("w1", "%20", "@1", sid))
                self.assertIn(f"hide --if-closed --sid {sid} -- w1 ", hook)
        for sid in (None, "", "a b", "a;b", "a'b", "a$(x)"):
            with self.subTest(sid=sid):
                hook = self.record(sid=sid)[0][-1]
                self.assertEqual(hook, close_hook("w1", "%20", "@1"))
                self.assertNotIn("--sid", hook)

    def test_record_refuses_invalid_ids_and_sets_nothing(self):
        """AC6, AC11: a pane, window, name or opener tmux could misread tags nothing."""
        for kw in (
            {"pane": "20"},
            {"pane": "%x"},
            {"pane": "%20;"},
            {"window": "1"},
            {"window": "@1 "},
            {"name": "a b"},
            {"name": "w1;"},
            {"opener": ""},
            {"opener": "a:b"},
        ):
            with self.subTest(**kw):
                self.assertEqual(self.record(**kw), [])
                self.assertRegex(self.stderr.getvalue(), r"^tui: show: @worker: refused .*\n$")

    def test_no_hook_without_a_command_tmux_takes_verbatim(self):
        for command, why in (
            (None, "ensemble-session is not on PATH"),
            ("/a b", "/a b: tmux would misread it"),
        ):
            with self.subTest(why=why):
                with environ(PATH="/nonexistent"):
                    calls = self.record(command=command)
                self.assertEqual(calls, record("mgr", "%20", name="w1", hook=False))
                self.assertEqual(self.stderr.getvalue(), f"tui: show: after-kill-pane: {why}\n")

    def test_a_failure_to_record_is_printed(self):
        fake = FakeTmux(fail=("set-option",))
        self.assertEqual(len(self.record(fake=fake)), 1)
        self.assertEqual(self.stderr.getvalue(), "tui: show: @worker: tmux: boom\n")

    def test_label_sets_the_border_only_when_asked(self):
        """AC12 (D6): a labeled worker's pane holds its name in @worker-label, shown in the
        window's bottom pane border; unlabeled, no border option is set.
        """
        labeled = self.record(label=True)[0]
        self.assertEqual(labeled, record("mgr", "%20", name="w1", label=True)[0])
        for option in (
            "set-option -p -t %20 @worker-label w1 ;",
            "set-option -w -t @1 pane-border-status bottom ;",
            "set-option -w -t @1 pane-border-format #{?#{@worker-label},[#{@worker-label}],} ;",
        ):
            self.assertIn(option, " ".join(labeled))
        plain = self.record()[0]
        for option in ("@worker-label", "pane-border-status", "pane-border-format"):
            self.assertNotIn(option, plain)


class CloseTest(Case):
    PANE = types.WorkerPane("%20", "s", "mgr", "@1", "mgr")

    CLOSE = [
        *("tmux", "set-hook", "-u", "-w", "-t", "@1", "after-kill-pane[20]"),
        *(";", "kill-pane", "-t", "%20"),
    ]

    def test_close_unsets_the_close_hook_then_kills_the_pane_in_one_call(self):
        fake = FakeTmux()
        placed(fake).close(self.PANE)
        self.assertEqual(fake.calls, [self.CLOSE])

    def test_a_gone_pane_window_or_server_is_no_error_another_failure_raises(self):
        for err in ("can't find pane: %20", "can't find window: @1", "no server running on /x"):
            with self.subTest(err=err):
                fake = FakeTmux(results={"set-hook": (1, "", err + "\n")})
                placed(fake).close(self.PANE)
                self.assertEqual(fake.calls, [self.CLOSE])
        fake = FakeTmux(results={"set-hook": (1, "", "boom\n")})
        with self.assertRaisesRegex(tmux_cli.TuiError, "^tmux: boom$"):
            placed(fake).close(self.PANE)


class WorkersTest(Case):
    def test_panes_are_the_names_live_worker_panes_and_a_tmux_failure_raises(self):
        rows = worker_rows(
            ("%5", "s", "mgr", "@1", "mgr"),
            ("%6", "t", "s"),
            "1\t%7\ts\tmgr\t@1\tmgr",
            ("%8", "s", "a b", "@2", "other"),
            ("%5", "s", "mgr", "@1", "linked"),
        )
        fake = FakeTmux(results={"list-panes": rows})
        self.assertEqual(
            placed(fake).panes("s"),
            [
                types.WorkerPane("%5", "s", "mgr", "@1", "mgr"),
                types.WorkerPane("%8", "s", "", "@2", "other"),
            ],
        )
        self.assertEqual(fake.calls, [WORKERS])
        fake = FakeTmux(fail=("list-panes",))
        with self.assertRaisesRegex(tmux_cli.TuiError, "^tmux: boom$"):
            placed(fake).panes("s")

    def test_no_server_has_no_worker_panes(self):
        for err in (
            "no server running on /tmp/tmux-501/default",
            "error connecting to /x (No such file or directory)",
        ):
            with self.subTest(err=err):
                fake = FakeTmux(results={"list-panes": (1, "", err + "\n")})
                self.assertEqual(placed(fake).workers(), [])


class ReuseTest(PaneFixture):
    """open_pane opens no second worker pane while one of the worker lives."""

    def test_a_live_worker_pane_is_reused_whatever_its_opener(self):
        """AC9 (D3): any live worker pane of s, whoever opened it, is the one; nothing opens."""
        for opener in ("own", "other", ""):
            with self.subTest(opener=opener):
                fake = FakeTmux(
                    results={
                        "display-message": (0, "own\n"),
                        "list-panes": worker_rows(("%5", "s", opener)),
                    }
                )
                for _ in range(3):
                    self.assertIsNone(self.open(fake, INSIDE))
                    self.assertIsNone(self.open(fake, INSIDE, opener="x", split="below"))
                self.assertEqual(fake.calls, [OWN, WORKERS, WORKERS] * 3)
                self.assertEqual(self.stderr.getvalue(), "")

    def test_showing_twice_opens_one_pane(self):
        """AC9: the second show finds the first's worker pane."""
        tagged = []

        def set_option(argv):
            if argv[5:7] == ["@worker", "s"]:
                tagged.append(("%20", "s", "own"))
            return (0,)

        fake = FakeTmux(
            results={
                "display-message": (0, "own\n"),
                "list-panes": lambda argv: worker_rows(*tagged) if "-a" in argv else (1,),
                "list-clients": clients((5, "%9")),
                "split-window": split(),
                "set-option": set_option,
            }
        )
        self.assertIsNone(self.open(fake, INSIDE))
        self.assertIsNone(self.open(fake, INSIDE))
        self.assertEqual(fake.commands().count("split-window"), 1)
        self.assertEqual(fake.calls[-2:], [OWN, WORKERS])

    def test_a_dead_pane_or_another_workers_does_not_count(self):
        for rows in (
            ["1\t%5\ts\town\t@1\tmgr"],
            [("%5", "t", "own")],
            [("%5", "s;", "own")],
        ):
            with self.subTest(rows=rows):
                fake = FakeTmux(
                    results={
                        "display-message": (0, "own\n"),
                        "list-panes": lambda argv, rows=rows: (
                            worker_rows(*rows) if "-a" in argv else (1,)
                        ),
                        "list-clients": clients((5, "%9")),
                        "split-window": split(),
                    }
                )
                self.assertIsNone(self.open(fake, INSIDE, split="right"))
                self.assertIn("split-window", fake.commands())


class PanePlacementTest(PaneFixture):
    """The caller is in tmux session own, pane %3, its window's grid read failing; tmux session
    b's most recent client shows %9; a split prints %20 in @1.
    """

    def fake(self, rows=(), **results):
        return FakeTmux(
            results={
                "display-message": (0, "own\n"),
                "list-panes": lambda argv: worker_rows(*rows) if "-a" in argv else (1,),
                "list-clients": clients((5, "%9")),
                "split-window": split(),
                **results,
            }
        )

    def test_split_stack_and_split_from_run_the_attach_argv(self):
        """AC3: each placement splits a pane running exactly the Host's CommandView.argv."""
        head = [OWN, WORKERS, clients_of("own"), grid_read("%3")]
        for kw, rows, then in (
            ({"split": "right"}, (), [clients_of("own"), split_window("-h", "%3")]),
            ({"split": "below"}, (), [clients_of("own"), split_window("-v", "%3")]),
            ({"split_from": "b"}, (), [WORKERS, clients_of("b"), split_window("-h", "%9")]),
            (
                {"split_from": "b", "split": "below"},
                (),
                [WORKERS, clients_of("b"), split_window("-v", "%9")],
            ),
            ({}, [("%7", "a", "own")], [split_window("-v", "%7")]),
        ):
            with self.subTest(**kw, rows=rows):
                fake = self.fake(rows)
                self.assertIsNone(self.open(fake, INSIDE, **kw))
                self.assertEqual(fake.calls, [*head, *then, *record("own", "%20")])
                command = next(c for c in fake.calls if c[1] == "split-window")
                self.assertEqual(command[command.index("--") + 1 :], list(ARGV))
                self.assertEqual(self.stderr.getvalue(), "")

    def test_a_worker_opener_outside_a_grid_anchors_at_its_pane(self):
        """AC10: a worker's own sub-worker opens beside its pane, asking no client of its name."""
        for kw, flag in (({}, "-h"), ({"split": "below"}, "-v")):
            with self.subTest(**kw):
                fake = self.fake([("%7", "a", "own")])
                self.assertIsNone(self.open(fake, INSIDE, opener="a", **kw))
                self.assertEqual(
                    fake.calls,
                    [WORKERS, grid_read("%7"), split_window(flag, "%7"), *record("a", "%20")],
                )

    def test_failure_names_the_printed_attach_command(self):
        """AC4: a placement failure ends `; watch it with <CommandView.printed>`."""
        for env, kw, fake, why in (
            ({}, {}, self.fake(), "no anchor pane: not in tmux"),
            ({}, {"split": "right"}, self.fake(), "no anchor pane: not in tmux"),
            (
                {},
                {"split_from": "b"},
                self.fake(**{"list-clients": (0, "")}),
                "no terminal shows b",
            ),
            (
                INSIDE,
                {},
                self.fake(**{"list-clients": (0, "")}),
                "no anchor pane: no terminal shows tmux session own",
            ),
            (
                {},
                {"split_from": "b"},
                self.fake(**{"list-clients": (1, "", "boom\n")}),
                "tmux: boom",
            ),
        ):
            with self.subTest(env=env, why=why, **kw):
                self.assertEqual(self.open(fake, env, **kw), why + WATCH)
                self.assertNotIn("split-window", fake.commands())
        self.assertEqual(WATCH, f"; watch it with {PRINTED}")
        fake = self.fake()
        self.stderr = io.StringIO()
        with environ(), which(), redirect_stderr(self.stderr):
            why = placed(fake).open_pane("s", ("/a#b",), "docker exec -it c1 x", split_from="b")
        self.assertEqual(why, "tmux would misread '/a#b'; watch it with docker exec -it c1 x")
        self.assertNotIn("split-window", fake.commands())

    def test_tmux_split_failures(self):
        fake = self.fake(**{"split-window": (1, "", "no space for new pane\n")})
        self.assertEqual(self.open(fake, {}, split_from="b"), "tmux: no space for new pane")
        self.assertNotIn("set-option", fake.commands())
        for printed in ("\n", "%20\n", "%20\t1\n", "x\t@1\n", "%20\t@1\tx\n"):
            with self.subTest(printed=printed):
                fake = self.fake(**{"split-window": (0, printed)})
                self.assertEqual(
                    self.open(fake, {}, split_from="b"),
                    f"tmux: split-window printed {printed.strip()!r}",
                )
                self.assertNotIn("set-option", fake.commands())

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
                    self.open(fake, {}, **kw)
                self.assertEqual(fake.calls, [])


class GridShowTest(GridFixture):
    def test_grid_split_runs_the_attach_argv(self):
        """AC1: a grid show splits a pane running exactly the Host's CommandView.argv, tagged
        with the run's sid in its close hook; no nested tmux client.
        """
        fake = self.fake({"%3": self.NEW})
        self.assertIsNone(self.open(fake, sid=SID))
        self.assertEqual(
            fake.calls,
            [
                OWN,
                WORKERS,
                clients_of("mgr"),
                grid_read("%3"),
                *self.split("-h", "%3"),
                *opening_commands(sid=SID),
            ],
        )
        command = fake.calls[5]
        self.assertEqual(command[command.index("--") + 1 :], list(ARGV))
        self.assertNotIn("attach", " ".join(command[: command.index("--")]))

    def test_grid_show_tags_the_pane_before_unmuting_and_tiling(self):
        """AC11: @grid-busy set, the split, the pane's @worker tags, then the grid unmuted and
        tiled, so no tile places the new pane as a manual pane.
        """
        fake = self.fake({"%3": self.NEW})
        self.assertIsNone(self.open(fake))
        self.assertEqual(
            fake.calls[4:8],
            [mute("@1"), split_window("-h", "%3"), *record("mgr", "%20"), grid_set("@1", "%3")],
        )
        self.assertEqual(fake.calls[6][5:7], ["@worker", "s"])
        self.assertIn("set-option -u -w -t @1 @grid-busy", " ".join(fake.calls[7]))
        self.assertFalse(any("@grid-busy" in c for c in fake.calls[5:7]))
