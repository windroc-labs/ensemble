"""Tmux worker pane placement and caller context through recorded command transport."""

import io
import re
from contextlib import redirect_stderr

from ensemble.session.engine import tmux_cli
from ensemble.session.viewer import placement, types
from tests.session.case import SID, Case
from tests.session.tmux_fixtures import (
    ARGV,
    COMMAND,
    GENERATION,
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
        run = {"printed": PRINTED, "sid": SID, "generation": GENERATION, "label": False}
        with which(kw.pop("command", COMMAND)), redirect_stderr(self.stderr):
            placed(fake)._record(name, opener, pane, window, **{**run, **kw})
        return fake.calls

    def test_record_sets_pane_options_and_close_hook(self):
        """AC6: after a hooks read, one call sets @worker, @worker-attach, @worker-opener and the
        after-kill-pane[N] hook hiding run S/G of w1 once %20 is gone.
        """
        hook = (
            "if-shell -F '#{m:* %20 *,#{S:#{W:#{P: #{pane_id} }}}}' '' "
            '"set-hook -u -w -t @1 after-kill-pane[20] ; '
            "run-shell -b '/opt/a_b.c@1+2-3/bin/ensemble-session hide --if-closed --sid S "
            "--generation G -- w1 >/dev/null 2>&1 || true'\""
        )
        self.assertEqual(
            self.record(sid="S", generation="G"),
            [
                [
                    *("tmux", "show-hooks", "-w", "-t", "@1", "after-kill-pane"),
                    *(";", "list-panes", "-a", "-F", "#{pane_id}"),
                ],
                [
                    *("tmux", "set-option", "-p", "-t", "%20", "@worker", "w1"),
                    *(";", "set-option", "-p", "-t", "%20", "@worker-attach", PRINTED),
                    *(";", "set-option", "-p", "-t", "%20", "@worker-opener", "mgr"),
                    *(";", "set-hook", "-w", "-t", "@1", "after-kill-pane[20]", hook),
                ],
            ],
        )
        self.assertEqual(self.stderr.getvalue(), "")
        self.assertEqual(self.record(opener=None), record(None, "%20", name="w1"))

    def test_close_hook_carries_the_run(self):
        """AC6 (D2): the hook names the run's sid and generation; either missing or bad: none."""
        for sid, generation in ((SID, "g1"), ("a.b:c_d-1", "0f8f-1:2")):
            with self.subTest(sid=sid, generation=generation):
                calls = self.record(sid=sid, generation=generation)
                self.assertEqual(
                    calls, record("mgr", "%20", name="w1", sid=sid, generation=generation)
                )
                self.assertEqual(self.stderr.getvalue(), "")
        for bad in (None, "", "a b", "a;b", "a'b", "a$(x)", "#{pane_id}"):
            for run in ({"sid": bad}, {"generation": bad}):
                with self.subTest(**run):
                    self.assertEqual(
                        self.record(**run), record("mgr", "%20", name="w1", hook=False)
                    )
                    self.assertRegex(
                        self.stderr.getvalue(), r"^tui: show: after-kill-pane: refused run .*\n$"
                    )

    def test_close_hook_refuses_any_value_outside_its_allow_list(self):
        """AC6: every value in the hook passes its allow-list, else no hook."""
        good = {"sid": SID, "generation": GENERATION}
        self.assertEqual(
            placement._close_hook(COMMAND, "w1", "%20", "@1", **good),
            close_hook("w1", "%20", "@1"),
        )
        for at, bad in (
            (0, "/opt/#{pane_id}/ensemble-session"),
            (0, "/a b"),
            (0, "/a';x"),
            (1, "x';touch p;'"),
            (1, ""),
            (2, "20"),
            (2, "%20;"),
            (3, "1"),
            (3, "@1 "),
        ):
            with self.subTest(at=at, bad=bad):
                values = [COMMAND, "w1", "%20", "@1"]
                values[at] = bad
                self.assertIsNone(placement._close_hook(*values, **good))
        for key, bad in (
            ("sid", None),
            ("sid", "a b"),
            ("generation", None),
            ("generation", "g'1"),
        ):
            with self.subTest(**{key: bad}):
                hook = placement._close_hook(COMMAND, "w1", "%20", "@1", **{**good, key: bad})
                self.assertIsNone(hook)

    def test_record_prunes_the_entries_of_gone_panes(self):
        """AC6: the hook's call unsets each after-kill-pane[M] whose %M is gone; nothing else."""
        read = (
            'after-kill-pane[5] if-shell -F "#{m:* %5 *,x}" \'\' "run-shell true"\n'
            "after-kill-pane[7] run-shell true\n"
            "after-kill-pane[11] run-shell true\n"
            "after-kill-pane[x] run-shell true\n"
            "after-kill-pane[9]\n"
            "pane-exited[12] run-shell true\n"
            " after-kill-pane[13] run-shell true\n"
            "%3\n%7\n%20\n"
        )
        fake = FakeTmux(results={"show-hooks": (0, read)})
        self.assertEqual(
            self.record(fake=fake), record("mgr", "%20", name="w1", pruned=("5", "11"))
        )
        for result in ((1, "", "boom\n"), (0, "")):
            with self.subTest(result=result):
                fake = FakeTmux(results={"show-hooks": result})
                self.assertEqual(self.record(fake=fake), record("mgr", "%20", name="w1"))
        fake = FakeTmux(results={"show-hooks": OSError(2, "no tmux")})
        self.assertEqual(self.record(fake=fake)[1:], record("mgr", "%20", name="w1")[1:])

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
        self.assertEqual(len(self.record(fake=fake)), 2)
        self.assertEqual(self.stderr.getvalue(), "tui: show: @worker: tmux: boom\n")

    def test_label_sets_the_border_only_when_asked(self):
        """AC12 (D6): a labeled worker's pane holds its name in @worker-label, shown in the
        window's bottom pane border; unlabeled, no border option is set.
        """
        labeled = self.record(label=True)[-1]
        self.assertEqual(labeled, record("mgr", "%20", name="w1", label=True)[-1])
        for option in (
            "set-option -p -t %20 @worker-label w1 ;",
            "set-option -w -t @1 pane-border-status bottom ;",
            "set-option -w -t @1 pane-border-format #{?#{@worker-label},[#{@worker-label}],} ;",
        ):
            self.assertIn(option, " ".join(labeled))
        plain = self.record()[-1]
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
            "1\t%7\ts\tmgr\t@1\tmgr\t",
            ("%8", "s", "a b", "@2", "other"),
            ("%5", "s", "mgr", "@1", "linked"),
        )
        fake = FakeTmux(results={"list-panes": rows})
        self.assertEqual(
            placed(fake).panes("s"),
            [
                types.WorkerPane("%5", "s", "mgr", "@1", "mgr", PRINTED),
                types.WorkerPane("%8", "s", "", "@2", "other", PRINTED),
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

    def test_an_earlier_runs_pane_is_not_reused(self):
        """AC9: a pane of s running another attach command (an earlier run's, still closing) is not
        this run's: a new pane opens, tagged with this run's attach command.
        """
        fake = FakeTmux(
            results={
                "display-message": (0, "own\n"),
                "list-panes": lambda argv: (
                    worker_rows(("%5", "s", "own", "@1", "mgr", "/fake/attach old"))
                    if "-a" in argv
                    else (1,)
                ),
                "list-clients": clients((5, "%9")),
                "split-window": split(),
            }
        )
        self.assertIsNone(self.open(fake, INSIDE))
        self.assertEqual(fake.commands().count("split-window"), 1)
        self.assertEqual(fake.calls[-2:], record("own", "%20"))

    def test_a_dead_pane_or_another_workers_does_not_count(self):
        for rows in (
            ["1\t%5\ts\town\t@1\tmgr\t"],
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
            ({"split": "right"}, (), [split_window("-h", "%3")]),
            ({"split": "below"}, (), [split_window("-v", "%3")]),
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


class SpotTest(GridFixture):
    """_spot for worker s, outside tmux: tmux session mgr's most recent client shows %9; a tmux
    session not in `sessions` is gone; `windows` as GridFixture's.
    """

    W1 = types.WorkerPane("%10", "w1", "mgr", "@1", "mgr")
    GRID = [("@1", "%3", "%3", "79", "48"), ("@1", "%3", "%10", "80", "48")]  # w1 in mgr's grid

    def tmux(self, windows=None, sessions=("mgr",), rows=(), **results):
        def listed(argv):
            if argv[3][1:] not in sessions:
                return 1, "", f"can't find session: {argv[3][1:]}\n"
            return clients((5, "%9"))

        return self.fake(windows or {}, rows, clients=listed, **results)

    def spot(self, fake, opener: str | None = "mgr", workers=(), **kw):
        kw = {"own": None, "split": None, "split_from": None, "fallbacks": (), **kw}
        with environ():
            return placed(fake)._spot("s", opener, workers=list(workers), **kw)

    def test_each_branch(self):
        right, below = types.Split.RIGHT, types.Split.BELOW
        new = {"%9": [("@2", "", "%9", "160", "48")]}
        stacked = types.WorkerPane("%7", "a", "mgr", "@1", "mgr")
        for name, windows, opener, workers, kw, spot in (
            ("grid", new, "mgr", (), {}, ("%9", right, ("@2", "%9"))),
            ("grid below", {"%10": self.GRID}, "w1", [self.W1], {}, ("%10", below, ("@1", "%3"))),
            ("split_from", {}, "mgr", (), {"split_from": "b"}, ("%9", right, None)),
            (
                "split_from below",
                {},
                None,
                (),
                {"split_from": "b", "split": "below"},
                ("%9", below, None),
            ),
            ("split", {}, "mgr", [stacked], {"split": "below"}, ("%9", below, None)),
            ("stack", {}, "mgr", [stacked], {}, ("%7", below, None)),
            ("opener pane", {}, "mgr", (), {}, ("%9", right, None)),
        ):
            with self.subTest(name):
                fake = self.tmux(windows, sessions=("mgr", "b"))
                found = self.spot(fake, opener, workers, **kw)
                self.assertEqual(found, types.Spot(*spot))
        for opener, sessions, why in (
            (None, (), "no anchor pane: not in tmux"),
            ("mgr", (), "no anchor pane: no terminal shows tmux session mgr"),
        ):
            with self.subTest(opener=opener), self.assertRaisesRegex(tmux_cli.TuiError, why):
                self.spot(self.tmux(sessions=sessions), opener)

    def test_a_fallback_stands_in_for_an_opener_without_a_pane_or_client(self):
        """D5: hidden a (no worker pane, no tmux session): the first of its fallbacks with a
        worker pane or a client gives the grid, else the opener pane; the stack still keys on a.
        """
        right, below = types.Split.RIGHT, types.Split.BELOW
        mine = types.WorkerPane("%7", "c", "a", "@1", "mgr")
        theirs = types.WorkerPane("%8", "d", "mgr", "@1", "mgr")
        new = {"%9": [("@2", "", "%9", "160", "48")]}
        for name, windows, workers, fallbacks, spot in (
            ("grid", new, (), ("x", "mgr"), ("%9", right, ("@2", "%9"))),
            (
                "worker pane's grid",
                {"%10": self.GRID},
                [self.W1],
                ("w1",),
                ("%10", below, ("@1", "%3")),
            ),
            ("opener pane", {}, (), ("x", "mgr"), ("%9", right, None)),
            ("stack", {}, [mine, theirs], ("mgr",), ("%7", below, None)),
        ):
            with self.subTest(name):
                fake = self.tmux(windows)
                found = self.spot(fake, "a", workers, fallbacks=fallbacks)
                self.assertEqual(found, types.Spot(*spot))
        fake = self.tmux(new)
        self.spot(fake, "a", fallbacks=("x", "mgr", "w1"))
        self.assertEqual(
            fake.calls, [clients_of("a"), clients_of("x"), clients_of("mgr"), grid_read("%9")]
        )
        with self.assertRaisesRegex(
            tmux_cli.TuiError, "^no anchor pane: no terminal shows tmux session a$"
        ):
            self.spot(self.tmux(), "a", fallbacks=("x",))

    def test_no_fallback_for_an_opener_with_a_pane_or_client_or_a_failed_read(self):
        """An opener's own pane or client wins over a fallback's grid; a failed client read is
        that failure, not a fallback.
        """
        grid = {"%10": self.GRID}
        for opener, workers, spot in (
            ("mgr", (), ("%9", types.Split.RIGHT, None)),
            (
                "a",
                [types.WorkerPane("%5", "a", "mgr", "@3", "mgr")],
                ("%5", types.Split.RIGHT, None),
            ),
        ):
            with self.subTest(opener=opener):
                fake = self.tmux(grid)
                found = self.spot(
                    fake, opener, [*workers, self.W1], split="right", fallbacks=("w1",)
                )
                self.assertEqual(found, types.Spot(*spot))
        fake = self.tmux(grid, **{"list-clients": (1, "", "boom\n")})
        with self.assertRaisesRegex(tmux_cli.TuiError, "^tmux: boom$"):
            self.spot(fake, "a", [self.W1], fallbacks=("w1",))
        self.assertEqual(fake.calls, [clients_of("a")])

    def test_open_pane_records_the_opener_not_its_fallback(self):
        """AC9: s shown for hidden a lands in mgr's grid; its @worker-opener stays a."""
        fake = self.tmux(
            {"%9": [("@2", "", "%9", "160", "48")]}, **{"split-window": split(window="@2")}
        )
        self.assertIsNone(self.open(fake, {}, opener="a", fallbacks=("mgr",)))
        self.assertEqual(
            fake.calls,
            [
                WORKERS,
                clients_of("a"),
                clients_of("mgr"),
                grid_read("%9"),
                *self.split("-h", "%9", "@2"),
                *opening_commands(window="@2", manager="%9", opener="a"),
            ],
        )
        for bad in ("a b", "", "a;"):
            with self.subTest(bad=bad), self.assertRaisesRegex(tmux_cli.TuiError, "invalid opener"):
                fake = FakeTmux()
                self.open(fake, {}, opener="a", fallbacks=("mgr", bad))
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
                *opening_commands(),
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
            fake.calls[4:9],
            [mute("@1"), split_window("-h", "%3"), *record("mgr", "%20"), grid_set("@1", "%3")],
        )
        self.assertEqual(fake.calls[7][5:7], ["@worker", "s"])
        self.assertIn("set-option -u -w -t @1 @grid-busy", " ".join(fake.calls[8]))
        self.assertFalse(any("@grid-busy" in c for c in fake.calls[5:8]))
