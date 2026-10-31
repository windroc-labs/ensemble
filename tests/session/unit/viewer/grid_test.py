"""Grid selection, retile scheduling and bounded lock coordination."""

import errno
import io
import os
import re
import tempfile
import time
from contextlib import redirect_stderr

from ensemble.session.engine import tmux_cli
from ensemble.session.engine.tmux_cli import TmuxCli
from ensemble.session.viewer import grid as grids
from ensemble.session.viewer import layout as layouts
from ensemble.session.viewer import types
from tests.session.case import PLUGIN_BIN, Case
from tests.session.tmux_fixtures import (
    INSIDE,
    NAMES,
    OWN,
    RETILE_ERROR,
    WATCH,
    WORKERS,
    FakeTmux,
    GridFixture,
    TileFixture,
    clients,
    clients_of,
    clock,
    close_hook,
    deadline,
    dump,
    grid,
    grid_read,
    grid_set,
    hold,
    json_layout,
    mute,
    opening_commands,
    record,
    slot,
    split,
    split_window,
    tiled,
    unknown,
    unmute,
    which,
)


class GridOpenTest(GridFixture):
    def test_own_session_starts_a_grid_right_of_its_pane(self):
        fake = self.fake({"%3": self.NEW})
        self.assertIsNone(self.open(fake))
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

    def test_the_largest_pane_but_the_managers_splits_below(self):
        for workers, target in (
            ([("%10", "80", "24"), ("%11", "80", "23")], "%10"),
            ([("%12", "40", "48"), ("%8", "80", "24"), ("%4", "20", "10")], "%12"),
            ([("%8", "80", "24"), ("%12", "40", "48")], "%12"),
        ):
            with self.subTest(target=target, workers=workers):
                fake = self.fake(
                    {"%3": [("@1", "%3", "%3", "79", "48"), *(("@1", "%3", *w) for w in workers)]}
                )
                self.assertIsNone(self.open(fake))
                self.assertEqual(
                    fake.calls[3:],
                    [grid_read("%3"), *self.split("-v", target), *opening_commands()],
                )

    def test_a_stored_manager_not_a_pane_of_the_window_starts_a_new_grid_at_the_anchor(self):
        for manager in ("%99", "x", "%3;", " %3"):
            with self.subTest(manager=manager):
                fake = self.fake(
                    {"%3": [("@1", manager, "%3", "79", "48"), ("@1", manager, "%5", "80", "48")]}
                )
                self.assertIsNone(self.open(fake))
                self.assertEqual(
                    fake.calls[3:], [grid_read("%3"), *self.split("-v", "%5"), *opening_commands()]
                )

    def test_an_opener_not_the_callers_anchors_at_its_most_recent_clients_pane(self):
        many = clients((5, "%8"), (9, "%9"), (7, "%7"))
        for env, head in (
            (INSIDE, [WORKERS, clients_of("mgr"), OWN]),
            ({}, [WORKERS, clients_of("mgr")]),
        ):
            with self.subTest(env=env):
                fake = self.fake(
                    {"%9": [("@2", "", "%9", "160", "48")]},
                    own="drv",
                    clients=many,
                    **{"split-window": split(window="@2")},
                )
                self.assertIsNone(self.open(fake, env, opener="mgr"))
                self.assertEqual(
                    fake.calls,
                    [
                        *head,
                        grid_read("%9"),
                        *self.split("-h", "%9", "@2"),
                        *opening_commands(window="@2", manager="%9"),
                    ],
                )
        # the client's pane in a grid window: that grid
        fake = self.fake(
            {"%9": [("@1", "%3", "%3", "79", "48"), ("@1", "%3", "%9", "80", "48")]}, own="drv"
        )
        self.assertIsNone(self.open(fake, opener="mgr"))
        self.assertEqual(
            fake.calls[3:], [grid_read("%9"), *self.split("-v", "%9"), *opening_commands()]
        )

    def test_a_given_opener_that_is_the_callers_session_anchors_at_its_pane(self):
        fake = self.fake({"%3": self.NEW})
        self.assertIsNone(self.open(fake, opener="mgr"))
        self.assertEqual(
            fake.calls,
            [
                WORKERS,
                clients_of("mgr"),
                OWN,
                grid_read("%3"),
                *self.split("-h", "%3"),
                *opening_commands(),
            ],
        )

    def test_a_worker_opens_a_sub_worker_in_its_panes_window(self):
        rows = [("%10", "w1", "mgr")]
        window = [("@1", "%3", "%3", "79", "48"), ("@1", "%3", "%10", "80", "48")]
        for kw, head in (({}, [OWN, WORKERS]), ({"opener": "w1"}, [WORKERS])):
            with self.subTest(**kw):
                fake = self.fake({"%10": window}, rows, own="w1")
                self.assertIsNone(self.open(fake, **kw))
                self.assertEqual(
                    fake.calls,
                    [
                        *head,
                        grid_read("%10"),
                        *self.split("-v", "%10"),
                        *opening_commands(opener="w1"),
                    ],
                )

    def test_an_unshown_session_fails_and_a_worker_outside_a_grid_opens_beside_its_pane(self):
        fake = self.fake({"%3": self.NEW}, clients=(0, ""))
        self.assertEqual(
            self.open(fake), "no anchor pane: no terminal shows tmux session mgr" + WATCH
        )
        self.assertEqual(fake.calls, [OWN, WORKERS, clients_of("mgr")])
        rows = [("%10", "w1", "x")]
        for windows in (
            {"%10": [("@1", "", "%10", "160", "48")]},
            {"%10": [("@1", "%9", "%10", "160", "48")]},
            {},
        ):
            with self.subTest(windows=windows):
                fake = self.fake(windows, rows, own="w1")
                self.assertIsNone(self.open(fake))
                self.assertEqual(
                    fake.calls,
                    [
                        OWN,
                        WORKERS,
                        grid_read("%10"),
                        split_window("-h", "%10"),
                        *record("w1", "%20"),
                    ],
                )
                self.assertEqual(self.stderr.getvalue(), "")

    def test_a_failed_or_unreadable_grid_read_falls_back_to_todays_path(self):
        for windows in (
            {},
            *(
                {"%3": [row]}
                for row in (
                    ("@1;", "", "%3", "160", "48"),
                    ("1", "", "%3", "160", "48"),
                    ("@1", "", "%3;", "160", "48"),
                    ("@1", "", "%3", "x", "48"),
                    ("@1", "", "%3", "160", "²"),
                    ("@1", "", "%3", "160"),
                    ("@1", "", "%3", "160", "48", "/tmp/s"),
                )
            ),
        ):
            with self.subTest(windows=windows):
                fake = self.fake(windows)
                self.assertIsNone(self.open(fake))
                self.assertEqual(
                    fake.calls,
                    [
                        OWN,
                        WORKERS,
                        clients_of("mgr"),
                        grid_read("%3"),
                        split_window("-h", "%3"),
                        *record("mgr", "%20"),
                    ],
                )
        fake = self.fake({"%3": self.NEW}, **{"list-clients": (1, "", "boom\n")})
        self.assertEqual(self.open(fake), "tmux: boom" + WATCH)
        self.assertEqual(fake.calls, [OWN, WORKERS, clients_of("mgr")])

    def test_split_honored_outside_the_grid(self):
        fake = self.fake({})
        self.assertIsNone(self.open(fake, split="below"))
        self.assertEqual(fake.calls[-3:], [split_window("-v", "%3"), *record("mgr", "%20")])
        self.assertEqual(self.stderr.getvalue(), "")

    def test_a_failed_split_changes_nothing_else(self):
        # the hooks unmuted again after a split that raises too
        fake = self.fake({"%3": self.NEW}, **{"split-window": (1, "", "no space for new pane\n")})
        self.assertEqual(self.open(fake), "tmux: no space for new pane")
        self.assertEqual(fake.calls[3:], [grid_read("%3"), *self.split("-h", "%3"), unmute("@1")])
        self.assertEqual(self.stderr.getvalue(), "")
        fake = self.fake({"%3": self.NEW})
        self.assertEqual(
            self.open(fake, argv=("/fake/attach", "a#b")), "tmux would misread 'a#b'" + WATCH
        )
        self.assertEqual(fake.calls[3:], [grid_read("%3"), mute("@1"), unmute("@1")])

    def test_hooks_need_a_command_tmux_takes_verbatim(self):
        for bad in (
            "/a b/ensemble-session",
            "/a#b/ensemble-session",
            "/a/ensemble-session;",
            "/a/agënt-pm-session",
            "/a'b/ensemble-session",
            "/a$(x)/ensemble-session",
        ):
            with self.subTest(bad=bad):
                fake = self.fake({"%3": self.NEW})
                self.assertIsNone(self.open(fake, command=bad))
                self.assertEqual(
                    fake.calls[-5:],
                    [
                        *record("mgr", "%20", hook=False),
                        grid_set("@1", "%3", hooks=False),
                        *tiled("@1"),
                    ],
                )
                self.assertEqual(
                    self.stderr.getvalue(),
                    f"tui: show: after-kill-pane: {bad}: tmux would misread it\n"
                    f"tui: show: grid: hooks: {bad}: tmux would misread it\n",
                )

    def test_hooks_run_the_ensemble_session_path_finds_else_none(self):
        found = os.path.join(PLUGIN_BIN, "ensemble-session")
        fake = self.fake({"%3": self.NEW})
        self.assertIsNone(self.open(fake, {**INSIDE, "PATH": PLUGIN_BIN}, command=None))
        self.assertEqual(fake.calls[-4], grid_set("@1", "%3", command=found))
        self.assertEqual(fake.calls[-5][-1], close_hook("s", "%20", "@1", command=found))
        fake = self.fake({"%3": self.NEW})
        self.assertIsNone(self.open(fake, {**INSIDE, "PATH": "/usr/bin:/bin"}, command=None))
        self.assertEqual(
            fake.calls[-5:],
            [*record("mgr", "%20", hook=False), grid_set("@1", "%3", hooks=False), *tiled("@1")],
        )
        self.assertEqual(
            self.stderr.getvalue(),
            "tui: show: after-kill-pane: ensemble-session is not on PATH\n"
            "tui: show: grid: hooks: ensemble-session is not on PATH\n",
        )

    def test_retile_modes_and_split_ignored_in_the_grid(self):
        # None uses the default
        splits = ({"split": "below"}, {"split_from": "b"}, {"split": "right", "split_from": "b"})
        for retile, mode, kw, stderr in (
            (None, "open-close", {}, ""),
            *((m, m, {}, "") for m in ("all", "open-close", "off")),
            *(("all", "all", kw, self.NOTICE) for kw in splits),
        ):
            with self.subTest(retile=retile, **kw):
                fake = self.fake({"%3": self.NEW})
                self.assertIsNone(self.open(fake, retile=retile, **kw))
                self.assertEqual(
                    fake.calls,
                    [
                        OWN,
                        WORKERS,
                        clients_of("mgr"),
                        grid_read("%3"),
                        *self.split("-h", "%3"),
                        *opening_commands(retile=mode),
                    ],
                )
                self.assertEqual(self.stderr.getvalue(), stderr)

    def test_retile_modes_with_a_hook_path_tmux_would_misread(self):
        # all and open-close: no hook set, the others unset; the pane gets no close hook
        problem = "/a b/ensemble-session: tmux would misread it\n"
        for retile, printed in (
            ("all", f"tui: show: grid: hooks: {problem}"),
            ("open-close", f"tui: show: grid: hooks: {problem}"),
            ("off", ""),
        ):
            with self.subTest(retile=retile):
                fake = self.fake({"%3": self.NEW})
                self.assertIsNone(self.open(fake, retile=retile, command="/a b/ensemble-session"))
                self.assertEqual(fake.calls[6:], opening_commands(retile=retile, hooks=False))
                self.assertEqual(
                    self.stderr.getvalue(), f"tui: show: after-kill-pane: {problem}{printed}"
                )

    def test_bad_retile_raises_before_tmux(self):
        for bad in ("x", True, 1, ""):
            with self.subTest(retile=bad):
                fake = self.fake({"%3": self.NEW})
                with self.assertRaisesRegex(
                    tmux_cli.TuiError, RETILE_ERROR.format(re.escape(repr(bad)))
                ):
                    self.open(fake, retile=bad)
                self.assertEqual(fake.calls, [])

    def test_a_failure_after_the_split_is_the_shows_and_leaves_the_pane(self):
        fake = self.fake(
            {"%3": self.NEW},
            **{"set-option": lambda argv: (1, "", "boom\n") if argv[2] == "-w" else (0,)},
        )
        self.assertEqual(self.open(fake), "tmux: boom")
        self.assertEqual(fake.calls[-3:], [*record("mgr", "%20"), grid_set("@1", "%3")])
        socket = ["tmux", "display-message", "-p", "-t", "@1", "#{socket_path}"]
        fake = self.fake({"%3": self.NEW})
        fake.results["display-message"] = lambda argv: (
            (1, "", "boom\n") if argv == socket else (0, "mgr\n")
        )
        self.assertEqual(self.open(fake), "tmux: boom")
        self.assertEqual(fake.calls[-4:], [*record("mgr", "%20"), grid_set("@1", "%3"), socket])
        self.assertNotIn("kill-pane", fake.commands())
        self.assertEqual(self.stderr.getvalue(), "")

    def test_per_column(self):
        fake = self.fake({"%3": self.NEW})
        self.assertIsNone(self.open(fake, per_column=2))
        self.assertEqual(fake.calls[-4:], [grid_set("@1", "%3", 2), *tiled("@1")])
        for bad in (0, 10000, -1, True, "3", 2.0):
            with self.subTest(per_column=bad):
                fake = self.fake({"%3": self.NEW})
                with self.assertRaisesRegex(
                    tmux_cli.TuiError, "^per_column must be an int from 1 to 9999"
                ):
                    self.open(fake, per_column=bad)
                self.assertEqual(fake.calls, [])


class RetileTest(Case):
    def test_modes(self):
        self.assertEqual(list(types.Retile), ["all", "open-close", "off"])
        for retile in (None, *types.Retile, "all"):
            with self.subTest(retile=retile):
                self.assertIsNone(grids.validate_retile(retile))
        for bad in ("x", True, 1, "", "ALL"):
            with (
                self.subTest(retile=bad),
                self.assertRaisesRegex(
                    tmux_cli.TuiError, RETILE_ERROR.format(re.escape(repr(bad)))
                ),
            ):
                grids.validate_retile(bad)


class TileApplicationTest(TileFixture):
    def test_swaps_and_select_layout_sent_muted_then_unmuted(self):
        # manual pane %4, split from %1, follows it: last, in a second column
        layout = "2cc7,160x48,0,0{79x48,0,0,0,80x48,80,0[80x8,80,0,1,80x7,80,9,4,80x15,80,17,2,80x15,80,33,3]}"
        fake = self.tile(layout, ["%0", "%1", "%4", "%2", "%3"], self.W3)
        self.assertEqual(
            fake.calls,
            [
                self.SOCKET,
                self.READ,
                self.PANES,
                WORKERS,
                NAMES,
                [
                    "tmux",
                    "set-option",
                    "-w",
                    "-t",
                    "@3",
                    "@grid-tiles",
                    "1000000",
                    ";",
                    "set-option",
                    "-w",
                    "-t",
                    "@3",
                    "@grid-panes",
                    "%0 %1 %2 %3 %4",
                    ";",
                    "set-option",
                    "-w",
                    "-t",
                    "@3",
                    "@grid-busy",
                    "@3",
                    ";",
                    "swap-pane",
                    "-d",
                    "-s",
                    "%2",
                    "-t",
                    "%4",
                    ";",
                    "swap-pane",
                    "-d",
                    "-s",
                    "%3",
                    "-t",
                    "%4",
                    ";",
                    "select-layout",
                    "-t",
                    "@3",
                    "ac6e,160x48,0,0{79x48,0,0,0,80x48,80,0{40x48,80,0[40x16,80,0,1,"
                    "40x15,80,17,2,40x15,80,33,3],39x48,121,0,4}}",
                ],
                ["tmux", "set-option", "-u", "-w", "-t", "@3", "@grid-busy"],
            ],
        )

    def test_a_window_split_top_bottom_before_the_grid(self):
        # manager full width on top, manual pane %1 below it split for w1 (%2): the manager gets
        # half
        layout = dump("160x48,0,0[160x24,0,0,0,160x11,0,25,1,160x11,0,37,2]")
        fake = self.tile(layout, ["%0", "%1", "%2"], [("%2", "w1", "mgr")])
        want = layouts.render_layout(grid([slot("%2"), slot("%1")]))
        self.assertEqual(
            fake.calls[5:],
            self.applied(
                [
                    "tmux",
                    "swap-pane",
                    "-d",
                    "-s",
                    "%2",
                    "-t",
                    "%1",
                    ";",
                    "select-layout",
                    "-t",
                    "@3",
                    want,
                ]
            ),
        )
        self.assertEqual(
            want.split(",", 1)[1], "160x48,0,0{79x48,0,0,0,80x48,80,0[80x24,80,0,2,80x23,80,25,1]}"
        )

    def test_no_op_when_the_layouts_cells_match_or_the_grid_would_not_fit(self):
        for case, layout, panes, rows, n, size in (
            ("cells match", self.THREE, ["%0", "%1", "%2", "%3"], self.W3, "", (160, 48)),
            # a pass over the target would move them, but none follows a no-op
            (
                "cells match in another tree",
                dump(
                    "200x50,0,0{80x50,0,0,0,119x50,81,0{59x50,81,0,2,29x50,141,0,1,29x50,171,0,3}}"
                ),
                ["%0", "%2", "%1", "%3"],
                [("%1", "w1", "old1"), ("%2", "w2", "old3"), ("%3", "w3", "w1")],
                "1",
                (200, 50),
            ),
            # 4 high: a column of three cells needs 5
            (
                "too small",
                dump("160x4,0,0{79x4,0,0,0,80x4,80,0{26x4,80,0,1,26x4,107,0,2,26x4,134,0,3}}"),
                ["%0", "%1", "%2", "%3"],
                self.W3,
                "",
                (160, 4),
            ),
        ):
            with self.subTest(case):
                fake = self.tile(layout, panes, rows, n=n, size=size)
                self.assertEqual(
                    fake.calls, [self.SOCKET, self.READ, self.PANES, WORKERS, NAMES, self.UNMUTE]
                )

    def test_an_already_laid_out_window_records_its_pane_set_alone(self):
        # pane ids in numeric order, so a later --if-panes-changed tile skips a drag
        panes = ["%0", "%9", "%10"]
        layout = layouts.render_layout(grid([slot("%9"), slot("%10")]))
        record = ["tmux", "set-option", "-w", "-t", "@3", "@grid-panes", "%0 %9 %10"]
        for recorded in (
            "",
            "%0 %9",
            "%0 %10 %9",
            "%0 %9 %10;",
            " %0 %9 %10",
            "%0  %9 %10",
            "%0,%9,%10",
        ):
            for flag in (False, True):
                with self.subTest(recorded=recorded, if_panes_changed=flag):
                    fake = self.tile(layout, panes, recorded=recorded, if_panes_changed=flag)
                    self.assertEqual(
                        fake.calls,
                        [self.SOCKET, self.READ, self.PANES, WORKERS, NAMES, record, self.UNMUTE],
                    )
        fake = self.tile(layout, panes)
        self.assertEqual(
            fake.calls, [self.SOCKET, self.READ, self.PANES, WORKERS, NAMES, self.UNMUTE]
        )
        with self.assertRaisesRegex(tmux_cli.TuiError, "^tmux: boom$"):
            self.tile(layout, panes, recorded="", fail=("set-option",))

    def test_if_panes_changed_skips_while_the_pane_set_is_the_recorded_one(self):
        # a drag keeps the pane set: no pass; without the flag it snaps back
        fake = self.tile(*self.WIDE, if_panes_changed=True)
        self.assertEqual(fake.calls, [self.SOCKET, self.READ, self.PANES, self.UNMUTE])
        fake = self.tile(*self.WIDE)
        self.assertEqual(
            fake.calls,
            [self.SOCKET, self.READ, self.PANES, WORKERS, NAMES, *self.applied(self.WIDE_SENT)],
        )
        # another set, unset or junk: the pass runs and records it
        for recorded in ("", "%0 %1", "%0 %1 %2 %3", "%0 %2 %1", "%0 %1 %2;", "%0 %1 %2\x00", "x"):
            with self.subTest(recorded=recorded):
                fake = self.tile(*self.WIDE, recorded=recorded, if_panes_changed=True)
                self.assertEqual(
                    fake.calls,
                    [
                        self.SOCKET,
                        self.READ,
                        self.PANES,
                        WORKERS,
                        NAMES,
                        *self.applied(self.WIDE_SENT),
                    ],
                )

    def test_if_panes_changed_zoomed_no_grid_or_torn_down_records_nothing(self):
        for kw in ({"zoomed": "1"}, {"manager": ""}):
            with self.subTest(**kw):
                fake = self.tile(*self.WIDE, recorded="", **kw, if_panes_changed=True)
                self.assertEqual(fake.calls, [self.SOCKET, self.READ, self.UNMUTE])
        fake = self.tile(
            dump("160x48,0,0[160x16,0,0,1,160x15,0,17,2,160x15,0,33,3]"),
            ["%1", "%2", "%3"],
            self.W3,
            recorded="%0 %1 %2 %3",
            if_panes_changed=True,
        )
        self.assertEqual(
            fake.calls, [self.SOCKET, self.READ, self.PANES, self.TEARDOWN, self.UNMUTE]
        )
        fake = self.tile(
            "bogus", ["%0", "%1", "%2", "%3"], self.W3, recorded="", if_panes_changed=True
        )
        self.assertEqual(fake.calls, [self.SOCKET, self.READ, self.PANES, self.UNMUTE])

    def test_a_state_needing_several_passes_gets_its_fixed_point_at_once(self):
        # orphans of gone openers in an old layout: 2 and 3 passes each, a pass over the result
        # sends nothing
        for n, layout, panes, rows, swap, want in (
            (
                "1",
                "200x50,0,0[200x25,0,0{100x25,0,0,2,99x25,101,0,1},200x24,0,26,0]",
                ["%2", "%1", "%0"],
                [("%1", "w1", "old1"), ("%2", "w2", "old2")],
                ("%0", "%2"),
                "200x50,0,0{99x50,0,0,0,100x50,100,0{50x50,100,0,1,49x50,151,0,2}}",
            ),
            (
                "2",
                "200x50,0,0[200x16,0,0,0,200x16,0,17,3,200x16,0,34{100x16,0,34,2,99x16,101,34,1}]",
                ["%0", "%3", "%2", "%1"],
                [("%1", "w1", "old2"), ("%2", "w2", "old1"), ("%3", "w3", "w2")],
                ("%2", "%3"),
                (
                    "200x50,0,0{99x50,0,0,0,100x50,100,0[100x25,100,0{50x25,100,0,2,49x25,151,0,3},100x24,100,26,1]}"
                ),
            ),
        ):
            with self.subTest(n=n):
                fake = self.tile(dump(layout), panes, rows, n=n, size=(200, 50))
                self.assertEqual(
                    fake.calls[5:],
                    self.applied(
                        [
                            "tmux",
                            "swap-pane",
                            "-d",
                            "-s",
                            swap[0],
                            "-t",
                            swap[1],
                            ";",
                            "select-layout",
                            "-t",
                            "@3",
                            dump(want),
                        ]
                    ),
                )
                after = [leaf.pane for leaf in layouts.leaf_cells(layouts.parse_layout(want))]
                fake = self.tile(dump(want), after, rows, n=n, size=(200, 50))
                self.assertEqual(
                    fake.calls, [self.SOCKET, self.READ, self.PANES, WORKERS, NAMES, self.UNMUTE]
                )

    def test_a_closed_worker_pane_frees_its_cell_and_a_dead_one_is_a_manual_pane(self):
        # w1 (%1) closed; w2's attach command exited where remain-on-exit kept its pane %2 dead:
        # no worker pane, so a manual pane, last
        layout = dump("160x48,0,0{79x48,0,0,0,80x48,80,0[80x32,80,0,2,80x15,80,33,3]}")
        dead = "1\t%2\tw2\tmgr\t@3\tmgr\t"
        for rows, swaps, cells in (
            (self.W3[1:], [], [slot("%2"), slot("%3")]),
            (
                [dead, self.W3[2]],
                ["swap-pane", "-d", "-s", "%3", "-t", "%2", ";"],
                [slot("%3"), slot("%2")],
            ),
        ):
            with self.subTest(rows=rows):
                fake = self.tile(layout, ["%0", "%2", "%3"], rows)
                self.assertEqual(
                    fake.calls[5:],
                    self.applied(
                        [
                            "tmux",
                            *swaps,
                            *("select-layout", "-t", "@3"),
                            layouts.render_layout(grid(cells)),
                        ]
                    ),
                )

    def test_a_known_opener_without_a_pane_keeps_its_children_main(self):
        """a and b's opener w2 has no pane and no tmux session; known (a hidden worker)
        it keeps them main, unknown they stay an orphan group.
        """
        layout = dump(
            "160x48,0,0{79x48,0,0,0,80x48,80,0[80x16,80,0,1,80x15,80,17[80x7,80,17,4,80x7,80,25,5],"
            "80x15,80,33,3]}"
        )
        panes = ["%0", "%1", "%4", "%5", "%3"]
        rows = [("%1", "w1", "mgr"), ("%3", "w3", "mgr"), ("%4", "a", "w2"), ("%5", "b", "w2")]
        steady = [self.SOCKET, self.READ, self.PANES, WORKERS, NAMES, self.UNMUTE]
        for known in (unknown, {"w1", "a"}.__contains__):
            with self.subTest(known=known):
                self.assertEqual(self.tile(layout, panes, rows, known=known).calls, steady)
        fake = self.tile(layout, panes, rows, known={"w2"}.__contains__)
        self.assertEqual(fake.calls[:5], steady[:5])
        self.assertEqual(
            fake.calls[5][-1],
            layouts.render_layout(grid([slot("%4"), slot("%5"), slot("%1"), slot("%3")])),
        )

    def test_zoomed_or_no_grid_window_is_read_then_unmuted(self):
        for kw in ({"zoomed": "1"}, {"manager": ""}):
            with self.subTest(**kw):
                fake = self.tile(*self.WIDE, **kw)
                self.assertEqual(fake.calls, [self.SOCKET, self.READ, self.UNMUTE])

    def test_teardown_when_the_manager_is_gone_invalid_or_alone(self):
        for manager, layout, panes in (
            (
                "%0",
                dump("160x48,0,0[160x16,0,0,1,160x15,0,17,2,160x15,0,33,3]"),
                ["%1", "%2", "%3"],
            ),
            ("0", self.THREE, ["%0", "%1", "%2", "%3"]),
            ("%0;", self.THREE, ["%0", "%1", "%2", "%3"]),
            ("%0", dump("160x48,0,0,0"), ["%0"]),
        ):
            with self.subTest(manager=manager, panes=panes):
                fake = self.tile(layout, panes, self.W3, manager=manager)
                self.assertEqual(
                    fake.calls, [self.SOCKET, self.READ, self.PANES, self.TEARDOWN, self.UNMUTE]
                )

    def test_nothing_changed_when_the_layout_is_unreadable_or_not_the_windows_panes(self):
        four = ["%0", "%1", "%2", "%3"]
        for layout, panes in (
            ("bogus", four),
            (self.THREE[:-1], four),
            ("0000" + self.THREE[4:], four),
            (self.THREE, four[:3]),
            (self.THREE, [*four, "%4"]),
            (self.THREE, [*four[:3], "%5"]),
            (dump("160x48,0,0{79x48,0,0,0,80x48,80,0[80x24,80,0,1,80x23,80,25,1]}"), four[:2]),
        ):
            with self.subTest(layout=layout, panes=panes):
                fake = self.tile(layout, panes, self.W3)
                self.assertEqual(fake.calls, [self.SOCKET, self.READ, self.PANES, self.UNMUTE])

    def test_a_later_pass_too_small_keeps_the_last_that_fits(self):
        layout = dump(
            "116x11,0,0{51x11,0,0,0,64x11,52,0[64x3,52,0{32x3,52,0,3,31x3,85,0,1},64x3,52,4{32x3,52,4,4,"
            "31x3,85,4,5},64x3,52,8{32x3,52,8,6,31x3,85,8,2}]}"
        )
        rows = [
            ("%1", "w1", "mgr"),
            ("%2", "w2", "old1"),
            ("%3", "w3", "mgr"),
            ("%4", "w4", "w2"),
            ("%5", "w5", "old1"),
            ("%6", "w6", "mgr"),
        ]
        fake = self.tile(
            layout, ["%0", "%3", "%1", "%4", "%5", "%6", "%2"], rows, n="4", size=(116, 11)
        )
        self.assertEqual(
            fake.calls[5:],
            self.applied(
                [
                    "tmux",
                    "swap-pane",
                    "-d",
                    "-s",
                    "%1",
                    "-t",
                    "%3",
                    ";",
                    "swap-pane",
                    "-d",
                    "-s",
                    "%2",
                    "-t",
                    "%3",
                    ";",
                    "swap-pane",
                    "-d",
                    "-s",
                    "%3",
                    "-t",
                    "%5",
                    ";",
                    "swap-pane",
                    "-d",
                    "-s",
                    "%5",
                    "-t",
                    "%6",
                    ";",
                    "select-layout",
                    "-t",
                    "@3",
                    "6fdc,116x11,0,0{51x11,0,0,0,64x11,52,0{32x11,52,0[32x2,52,0,1,32x2,52,3{"
                    "16x2,52,3,2,15x2,69,3,4},32x2,52,6,3,32x2,52,9,5],31x11,85,0,6}}",
                ]
            ),
        )

    def test_worker_pane_fields_read_back_only_after_a_fullmatch(self):
        # a dead pane, a bad pane id, name or window, or a short row: skipped (its pane a manual
        # pane); an @worker-opener that is no name: unset
        rows = [
            ("%1", "w1", "mgr"),
            "1\t%2\tw2\tmgr\t@3\tmgr\t",
            ("%4", "w 4", "mgr"),
            ("%3", "w5", "w1"),
            ("%5", "w6", "%1"),
            "0\t%2;\tw7\tmgr\t@3\tmgr\t",
            "0\t%2\tw8\tmgr\t@3;\tmgr\t",
            "0\t%4\tw9\tmgr\t@3\tmgr",
        ]
        panes = ["%0", "%1", "%2", "%3", "%4", "%5"]
        fake = self.tile(
            layouts.render_layout(grid([slot(p) for p in panes[1:]], 6)), panes, rows, n="6"
        )
        cells = [slot("%1", slot("%3")), slot("%5"), slot("%2"), slot("%4")]
        self.assertEqual(fake.calls[5][-1], layouts.render_layout(grid(cells, 6)))

    def test_per_column_argument_else_the_window_option_else_the_default(self):
        panes = ["%0", "%1", "%2", "%3", "%4"]
        rows = [(f"%{i}", f"w{i}", "mgr") for i in range(1, 5)]
        for n, kw, per_column in (
            ("2", {}, 2),
            ("9999", {}, 9999),
            ("1", {"per_column": 2}, 2),
            ("2", {"per_column": 3}, None),
            ("", {}, None),
            ("0", {}, None),
            ("02", {}, None),
            ("10000", {}, None),
            ("2 ", {}, None),
            ("x", {}, None),
        ):
            with self.subTest(n=n, **kw):
                fake = self.tile(layouts.render_layout(grid(4)), panes, rows, n=n, **kw)
                want = layouts.render_layout(grid(4, per_column)) if per_column else None
                self.assertEqual(
                    fake.calls[5:],
                    self.applied(["tmux", "select-layout", "-t", "@3", want])
                    if want
                    else [self.UNMUTE],
                )

    def test_tile_reads_a_json_layout(self):
        # tmux 3.8 prints JSON: the same calls, select-layout given the old format
        panes = [f"%{i}" for i in range(14)]
        rows = [(f"%{i}", f"w{i}", "mgr") for i in range(1, 14)]
        stacked = grid(13, 13)
        old = self.tile(layouts.render_layout(stacked), panes, rows, n="3")
        new = self.tile(json_layout(stacked), panes, rows, n="3")
        self.assertEqual(new.calls, old.calls)
        self.assertEqual(new.calls[5][-1], layouts.render_layout(grid(13, 3)))

    def test_a_json_grid_is_steady(self):
        fake = self.tile(json_layout(grid(3)), ["%0", "%1", "%2", "%3"], self.W3)
        self.assertEqual(
            fake.calls, [self.SOCKET, self.READ, self.PANES, WORKERS, NAMES, self.UNMUTE]
        )


class TileCoordinationTest(TileFixture):
    def test_a_hook_run_returns_at_once_while_another_tile_holds_the_lock(self):
        for kw in ({}, {"recorded": "", "if_panes_changed": True}):
            with self.subTest(**kw):
                release = hold(self, self.lock)
                fake = self.tile(*self.WIDE, wait=False, **kw)
                self.assertEqual(fake.calls, [self.SOCKET])
                release()
                fake = self.tile(*self.WIDE, wait=False, **kw)
                self.assertEqual(
                    fake.calls,
                    [
                        self.SOCKET,
                        self.READ,
                        self.PANES,
                        WORKERS,
                        NAMES,
                        *self.applied(self.WIDE_SENT),
                    ],
                )

    def test_an_explicit_tile_waits_for_the_lock_then_tiles(self):
        release = hold(self, self.lock)
        fake, seen = self.fake(*self.WIDE), []

        def poll(sleeps):
            seen.append(list(fake.calls))
            if sleeps == 3:
                release()

        slept = clock(self, poll)
        grids.Grid(TmuxCli(fake), known=unknown).tile("@3")
        self.assertEqual(slept, [grids.TILE_POLL] * 3)
        self.assertEqual(seen, [[self.SOCKET]] * 3)
        self.assertEqual(
            fake.calls,
            [
                self.SOCKET,
                self.READ,
                self.PANES,
                WORKERS,
                NAMES,
                *self.applied(self.WIDE_SENT, str(int(time.monotonic() * 1000))),
            ],
        )

    def test_an_explicit_tile_gives_up_after_tile_wait(self):
        self.assertEqual((grids.TILE_POLL, grids.TILE_WAIT), (0.05, 5))
        hold(self, self.lock)
        slept = clock(self)
        fake = self.fake(*self.WIDE)
        with self.assertRaisesRegex(tmux_cli.TuiError, "^grid @3: busy$"):
            grids.Grid(TmuxCli(fake), known=unknown).tile("@3")
        self.assertEqual(fake.calls, [self.SOCKET])
        self.assertEqual(set(slept), {grids.TILE_POLL})
        self.assertTrue(5 <= sum(slept) < 5.1)  # float steps: one poll more

    def test_a_directory_at_the_lock_path_raises_before_the_pass_and_grid_up_returns_it(self):
        os.mkdir(self.lock)
        want = f"^grid lock {re.escape(self.lock)}: {os.strerror(errno.EISDIR)}$"
        fake = self.fake(*self.WIDE)
        with self.assertRaisesRegex(tmux_cli.TuiError, want):
            grids.Grid(TmuxCli(fake), known=unknown).tile("@3")
        self.assertEqual(fake.calls, [self.SOCKET])
        fake = self.fake(*self.WIDE)
        with which():
            got = grids.Grid(TmuxCli(fake), known=unknown).configure(
                "@3", "%0", per_column=3, retile="all"
            )
        self.assertRegex(got, want)
        self.assertEqual(fake.calls, [grid_set("@3", "%0"), self.SOCKET])

    def test_an_empty_or_relative_socket_path_raises_before_the_lock(self):
        # the lock would land in the cwd
        cwd = tempfile.TemporaryDirectory()
        self.addCleanup(cwd.cleanup)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(cwd.name)
        for sock in ("", "default"):
            with self.subTest(sock=sock):
                fake = self.fake(*self.WIDE, sock=sock)
                with self.assertRaisesRegex(
                    tmux_cli.TuiError,
                    f"^grid @3: socket path {re.escape(repr(sock))}: want an absolute path$",
                ):
                    grids.Grid(TmuxCli(fake), known=unknown).tile("@3")
                self.assertEqual(fake.calls, [self.SOCKET])
                self.assertEqual(os.listdir(cwd.name), [])

    def test_a_fifo_at_the_lock_path_does_not_hang(self):
        os.mkfifo(self.lock, 0o600)
        fake = self.fake(*self.WIDE)
        with deadline(), self.assertRaisesRegex(tmux_cli.TuiError, "not a regular file$"):
            grids.Grid(TmuxCli(fake), known=unknown).tile("@3")
        self.assertEqual(fake.calls, [self.SOCKET])

    def test_lock_is_exclusive_per_socket_window_and_released_after_exit(self):
        with grids._grid_lock(self.sock, "@3", wait=False) as held:
            self.assertTrue(held)
            with grids._grid_lock(self.sock, "@3", wait=False) as other:
                self.assertFalse(other)
            with grids._grid_lock(self.sock, "@4", wait=False) as other:
                self.assertTrue(other)
            with grids._grid_lock(self.sock + "-other", "@3", wait=False) as other:
                self.assertTrue(other)
        with grids._grid_lock(self.sock, "@3", wait=False) as held:
            self.assertTrue(held)

    def test_grid_tiles_keeps_the_re_tiles_of_the_last_tile_window_then_adds_now(self):
        # now 1000000 ms: the window is 990001..1000000; junk, stale and future times are dropped
        junk = [
            "x",
            "-999999",
            "+999999",
            "999999.0",
            "1e6",
            "\uff19\uff19\uff19",
            "0000000000999999",
            "",
        ]
        tiles = " ".join(
            [
                *["990000", "5", "0"] * 50,
                *junk,
                "990001",
                "1000001",
                "999999999999999",
                "000000000999998",
                "999999\t1000000",
            ]
        )
        fake = self.tile(*self.WIDE, tiles=tiles)
        self.assertEqual(
            fake.calls[5:], self.applied(self.WIDE_SENT, "990001 999998 999999 1000000 1000000")
        )
        fake = self.tile(self.THREE, ["%0", "%1", "%2", "%3"], self.W3, tiles=tiles)
        self.assertEqual(
            fake.calls, [self.SOCKET, self.READ, self.PANES, WORKERS, NAMES, self.UNMUTE]
        )

    def test_more_than_tile_limit_re_tiles_in_tile_window_tear_the_grid_down(self):
        self.assertEqual((grids.TILE_LIMIT, grids.TILE_WINDOW), (50, 10))
        line = "tui: grid @3: auto re-tiling stopped: 51 re-tiles in 10 s; the next worker that opens rebuilds it"
        recent = [str(990001 + i * 199) for i in range(50)]
        for message in ((0,), (1, "", "no client\n"), OSError("gone")):
            with self.subTest(message=message):
                err = io.StringIO()
                with redirect_stderr(err):
                    fake = self.tile(*self.WIDE, tiles=" ".join(recent), message=message)
                self.assertEqual(
                    fake.calls,
                    [
                        self.SOCKET,
                        self.READ,
                        self.PANES,
                        WORKERS,
                        NAMES,
                        self.TEARDOWN,
                        ["tmux", "display-message", "-d", "10000", "-t", "@3", line],
                        self.UNMUTE,
                    ],
                )
                self.assertEqual(err.getvalue(), line + "\n")
        fake = self.tile(*self.WIDE, tiles=" ".join(recent[1:]))
        self.assertEqual(
            fake.calls[5:], self.applied(self.WIDE_SENT, " ".join([*recent[1:], "1000000"]))
        )

    def test_configure_tiles_the_window_by_its_per_column(self):
        sent = {}
        for n in (1, 3):
            with self.subTest(per_column=n):
                window = (self.THREE, ["%0", "%1", "%2", "%3"], self.W3)
                fake = self.fake(*window)
                with which():
                    got = grids.Grid(TmuxCli(fake), known=unknown).configure(
                        "@3", "%0", per_column=n, retile="all"
                    )
                self.assertIsNone(got)
                sent[n] = fake.calls[1:]
                self.assertEqual(sent[n], self.tile(*window, per_column=n).calls)
        self.assertNotEqual(sent[1], sent[3])

    def test_after_a_teardown_grid_up_under_off_sets_no_hook(self):
        recent = " ".join(str(990001 + i * 199) for i in range(50))
        with redirect_stderr(io.StringIO()):
            fake = self.tile(*self.WIDE, tiles=recent)
        self.assertEqual(fake.calls[5], self.TEARDOWN)
        del fake.calls[:]
        with which():
            self.assertIsNone(
                grids.Grid(TmuxCli(fake), known=unknown).configure(
                    "@3", "%0", per_column=3, retile="off"
                )
            )
        self.assertEqual(fake.calls, [grid_set("@3", "%0", retile="off")])

    def test_bad_window_or_per_column_raises_before_tmux(self):
        cases = [
            ("3", {}, "invalid window id '3': want @[0-9]+"),
            *((window, {}, "invalid window id") for window in ("@3;", "%3", "@", " @3", "@1a")),
            ("@3", {"per_column": 0}, "per_column must be an int from 1 to 9999, not 0"),
            *(("@3", {"per_column": n}, "per_column") for n in (10000, True, "3", 2.0)),
        ]
        for window, kw, error in cases:
            with self.subTest(window=window, **kw):
                fake = FakeTmux()
                with self.assertRaisesRegex(tmux_cli.TuiError, "^" + re.escape(error)):
                    grids.Grid(TmuxCli(fake), known=unknown).tile(window, **kw)
                self.assertEqual(fake.calls, [])

    def test_a_tmux_failure_raises(self):
        # @grid-busy unset all the same, its failure not masking the re-tile's; the lock freed
        fail = {"set-option": lambda argv: (1, "", "boom\n" if "@grid-tiles" in argv else "gone\n")}
        fake = self.fake(*self.WIDE, **fail)
        with self.assertRaisesRegex(tmux_cli.TuiError, "^tmux: boom$"):
            grids.Grid(TmuxCli(fake), known=unknown).tile("@3")
        self.assertEqual(fake.calls[5:], self.applied(self.WIDE_SENT))
        self.assertEqual(self.tile(*self.WIDE, wait=False).calls[:2], [self.SOCKET, self.READ])
        with self.assertRaisesRegex(tmux_cli.TuiError, "^tmux: boom$"):
            self.tile(dump("160x48,0,0,0"), ["%0"], fail=("set-hook",))
        with self.assertRaisesRegex(tmux_cli.TuiError, "^tmux: boom$"):
            grids.Grid(TmuxCli(FakeTmux(fail=("display-message",))), known=unknown).tile("@3")
