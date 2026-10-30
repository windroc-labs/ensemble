"""Pure layout codec, ownership tree and geometry vectors."""

import copy
import itertools
import json

from ensemble.session.viewer import layout as layouts
from ensemble.session.viewer import types
from tests.session.case import Case
from tests.session.tmux_fixtures import (
    MAIN_VERTICAL,
    TILED_SPLIT,
    body,
    grid,
    json_layout,
    slot,
)


class GridTreeTest(Case):
    """Manager %0, its tmux session mgr; rows: (order, name, @worker-opener, worker pane), a row
    without a pane a known name (a tmux session or a known opener); worker panes listed by order,
    the first claim of a pane owning.
    """

    def tree(self, rows, panes, layout=None):
        if layout is None:
            layout = grid([slot(p) for p in panes if p != "%0"], per_column=len(panes))
        else:
            layout = layouts.parse_layout(layout)
        workers = [
            types.WorkerPane(pane, name, opener, "@1", "mgr")
            for _, name, opener, pane in sorted(rows)
            if pane
        ]
        known = {"mgr", *(name for _, name, _, pane in rows if not pane)}
        return layouts.grid_tree("%0", panes, workers, known, layout)

    def test_cells_by_session_name_sub_workers_in_their_parents_cell(self):
        names = [
            "pm-TASK-10-a",
            "engineer-TASK-227-b",
            "researcher-TASK-3-c",
            "engineer-TASK-23-d",
            "pm-TASK-9-e",
            "engineer-TASK-9-f",
        ]
        for case, rows, panes, want in (
            (
                "nodes by session name, then manual panes",
                [(2, "a", "mgr", "%10"), (3, "b", "mgr", "%2"), (4, "c", "", "%9")],
                ["%0", "%10", "%5", "%2", "%9", "%3"],
                [slot("%10"), slot("%2"), slot("%9"), slot("%3"), slot("%5")],
            ),
            (
                "main cells by role, then numbers as numbers",
                [(i + 2, name, "mgr", f"%{i + 1}") for i, name in enumerate(names)],
                ["%0", "%1", "%2", "%3", "%4", "%5", "%6"],
                [slot(p) for p in ("%6", "%4", "%2", "%5", "%1", "%3")],
            ),
            (
                "sub-workers by pane id",
                [
                    (2, "pm-1", "mgr", "%1"),
                    (3, "engineer-2", "mgr", "%2"),
                    (4, "z", "pm-1", "%3"),
                    (5, "a", "pm-1", "%4"),
                    (6, "b", "engineer-2", "%5"),
                ],
                ["%0", "%1", "%2", "%3", "%4", "%5"],
                [slot("%2", slot("%5")), slot("%1", slot("%3"), slot("%4"))],
            ),
            (
                "a grandchild in its parent's cell",
                [
                    (2, "w1", "mgr", "%1"),
                    (3, "w2", "mgr", "%2"),
                    (4, "a", "w2", "%3"),
                    (5, "b", "w2", "%4"),
                    (6, "w3", "mgr", "%5"),
                    (7, "c", "a", "%6"),
                ],
                ["%0", "%1", "%2", "%3", "%4", "%5", "%6"],
                [slot("%1"), slot("%2", slot("%3", slot("%6")), slot("%4")), slot("%5")],
            ),
            (
                "a driver with an opener and no pane takes no cell",
                [
                    (2, "w1", "mgr", "%1"),
                    (3, "r-0123abcd-drive", "mgr", ""),
                    (4, "r-0123abcd", "mgr", "%2"),
                ],
                ["%0", "%1", "%2"],
                [slot("%2"), slot("%1")],
            ),
            (
                "openers that are no node of the window",
                [
                    (2, "x", "mgr", "%77"),
                    (3, "y", "x", "%1"),
                    (4, "z", "mgr", "%0"),
                    (5, "v", "z", "%2"),
                    (6, "u", "", "%3"),
                ],
                ["%0", "%1", "%2", "%3"],
                [slot("%3"), slot("%2"), slot("%1")],
            ),
            (
                "duplicate pane claims: the lower session id owns",
                [
                    (5, "x", "mgr", "%1"),
                    (3, "y", "mgr", "%1"),
                    (6, "z", "x", "%2"),
                    (7, "w", "y", "%3"),
                ],
                ["%0", "%1", "%2", "%3"],
                [slot("%1", slot("%3")), slot("%2")],
            ),
            (
                "an opener cycle goes main",
                [
                    (2, "a", "b", "%1"),
                    (3, "b", "a", "%2"),
                    (4, "c", "a", "%3"),
                    (5, "d", "d", "%4"),
                ],
                ["%0", "%1", "%2", "%3", "%4"],
                [slot("%1"), slot("%2"), slot("%3"), slot("%4")],
            ),
        ):
            with self.subTest(case):
                self.assertEqual(self.tree(rows, panes), want)

    def test_a_closed_pane_keeps_the_rest_in_order(self):
        panes = ["%0", "%1", "%3", "%4", "%5", "%6", "%7"]
        rows = [(i + 1, f"w{i}", "mgr", f"%{i}") for i in (1, 3, 4, 5, 6, 7)]
        cells = self.tree(rows, panes)
        self.assertEqual(cells, [slot(p) for p in panes[1:]])
        self.assertEqual(
            [[c.pane for c in col.children] for col in grid(cells).children[1].children],
            [["%1", "%3", "%4"], ["%5", "%6", "%7"]],
        )

    def test_orphans_take_the_killed_parents_cell(self):
        orphans = slot(None, slot("%4"), slot("%5"))
        cycle = [
            (2, "a", "lead", "%1"),
            (3, "lead", "a", "%2"),
            (4, "k1", "c1", "%3"),
            (5, "k2", "c1", "%4"),
        ]
        for case, layout, rows, panes, want in (
            # a (%1) and lead (%2) open each other; the orphans of gone c1 (%3, %4) sit right of a,
            # or of x (%5, a's): a group main does not reach goes main whole
            (
                "an orphan group main does not reach",
                (
                    "160x48,0,0{79x48,0,0,0,80x48,80,0[80x24,80,0{40x24,80,0,1,39x24,121,0[39x12,121,0,3,39x11,121,13,4]},"
                    "80x23,80,25,2]}"
                ),
                cycle,
                ["%0", "%1", "%2", "%3", "%4"],
                [slot("%1"), slot(None, slot("%3"), slot("%4")), slot("%2")],
            ),
            (
                "an orphan group right of a's child",
                (
                    "160x48,0,0{79x48,0,0,0,80x48,80,0[80x16,80,0{40x16,80,0,5,39x16,121,0[39x8,121,0,3,39x7,121,9,4]},"
                    "80x15,80,17,1,80x15,80,33,2]}"
                ),
                [*cycle, (6, "x", "a", "%5")],
                ["%0", "%1", "%2", "%3", "%4", "%5"],
                [slot("%1"), slot(None, slot("%3"), slot("%4")), slot("%2"), slot("%5")],
            ),
            # w2 (%2, with a %4 and b %5) killed: tmux gave its cell to [a, b]
            (
                "main cell",
                (
                    "160x48,0,0{79x48,0,0,0,80x48,80,0[80x16,80,0,1,80x15,80,17[80x7,80,17,4,80x7,80,25,5],80x15,80,33,3]}"
                ),
                [
                    (2, "w1", "mgr", "%1"),
                    (4, "w3", "mgr", "%3"),
                    (5, "a", "w2", "%4"),
                    (6, "b", "w2", "%5"),
                ],
                ["%0", "%1", "%3", "%4", "%5"],
                [slot("%1"), orphans, slot("%3")],
            ),
            # pm-1 (with a %4 and b %5) killed, its orphans' cell last
            (
                "place by name",
                (
                    "160x48,0,0{79x48,0,0,0,80x48,80,0[80x16,80,0,1,80x15,80,17,3,80x15,80,33[80x7,80,33,4,80x7,80,41,5]]}"
                ),
                [
                    (2, "engineer-1", "mgr", "%1"),
                    (4, "researcher-1", "mgr", "%3"),
                    (5, "a", "pm-1", "%4"),
                    (6, "b", "pm-1", "%5"),
                ],
                ["%0", "%1", "%3", "%4", "%5"],
                [slot("%1"), orphans, slot("%3")],
            ),
            # w1 (%1) has children c (%2, killed; its children a %4, b %5) and d (%3)
            (
                "sub cell",
                (
                    "160x48,0,0{79x48,0,0,0,80x48,80,0{40x48,80,0,1,39x48,121,0[39x24,121,0[39x12,121,0,4,"
                    "39x11,121,13,5],39x23,121,25,3]}}"
                ),
                [
                    (2, "w1", "mgr", "%1"),
                    (4, "d", "w1", "%3"),
                    (5, "a", "c", "%4"),
                    (6, "b", "c", "%5"),
                ],
                ["%0", "%1", "%3", "%4", "%5"],
                [slot("%1", orphans, slot("%3"))],
            ),
            # w2 killed as in the main cell case, manual pane %9 beside its orphans: they skip %9's
            # cell
            (
                "skipping a manual pane's cell",
                (
                    "160x48,0,0{79x48,0,0,0,80x48,80,0[80x16,80,0,1,80x15,80,17{40x15,80,17,9,39x15,121,17[39x7,121,17,4,"
                    "39x7,121,25,5]},80x15,80,33,3]}"
                ),
                [
                    (2, "w1", "mgr", "%1"),
                    (4, "w3", "mgr", "%3"),
                    (5, "a", "w2", "%4"),
                    (6, "b", "w2", "%5"),
                ],
                ["%0", "%1", "%3", "%4", "%5", "%9"],
                [slot("%1"), orphans, slot("%3"), slot("%9")],
            ),
        ):
            with self.subTest(case):
                self.assertEqual(self.tree(rows, panes, layout), want)

    def test_an_unknown_opener_without_a_pane_is_an_orphan_group(self):
        """AC9 (D4): children of w2, neither known nor a worker with a pane, form an orphan group
        where the layout shows them; w2 known (a tmux session or a hidden worker) keeps them main.
        """
        layout = (
            "160x48,0,0{79x48,0,0,0,80x48,80,0[80x16,80,0,1,80x15,80,17[80x7,80,17,4,80x7,80,25,5],"
            "80x15,80,33,3]}"
        )
        rows = [
            (2, "w1", "mgr", "%1"),
            (4, "w3", "mgr", "%3"),
            (5, "a", "w2", "%4"),
            (6, "b", "w2", "%5"),
        ]
        panes = ["%0", "%1", "%3", "%4", "%5"]
        self.assertEqual(
            self.tree(rows, panes, layout),
            [slot("%1"), slot(None, slot("%4"), slot("%5")), slot("%3")],
        )
        self.assertEqual(
            self.tree([*rows, (3, "w2", "", "")], panes, layout),
            [slot("%4"), slot("%5"), slot("%1"), slot("%3")],
        )

    def test_orphans_beside_a_one_pane_first_column_stay_main(self):
        # P (%2; c1 %3, c2 %4) killed. N=1: main [a, P, b]; N=2: main [a, x, P, b], x and P killed
        # together
        rows = [
            (2, "a", "mgr", "%1"),
            (4, "c1", "P", "%3"),
            (5, "c2", "P", "%4"),
            (6, "b", "mgr", "%5"),
        ]
        orphans = slot(None, slot("%3"), slot("%4"))
        for layout in (
            layouts.render_layout(grid([slot("%1"), orphans, slot("%5")], 1)),
            "160x48,0,0{79x48,0,0,0,80x48,80,0{40x48,80,0,1,39x48,121,0[39x24,121,0[39x12,121,0,3,"
            "39x11,121,13,4],39x23,121,25,5]}}",
        ):
            with self.subTest(layout=layout):
                self.assertEqual(
                    self.tree(rows, ["%0", "%1", "%3", "%4", "%5"], layout),
                    [orphans, slot("%1"), slot("%5")],
                )

    def test_two_orphan_groups_beside_a_one_pane_first_column_stay_main(self):
        # N=1: main [a, P1 (c1 %3, c2 %4), P2 (d1 %6, d2 %7)], both killed; either group resolved
        # first
        groups = slot(None, slot("%3"), slot("%4")), slot(None, slot("%6"), slot("%7"))
        layout = layouts.render_layout(grid([slot("%1"), *groups], 1))
        for p1, p2, want in (("p", "q", groups), ("q", "p", groups[::-1])):
            with self.subTest(p1=p1, p2=p2):
                rows = [
                    (2, "a", "mgr", "%1"),
                    (4, "c1", p1, "%3"),
                    (5, "c2", p1, "%4"),
                    (7, "d1", p2, "%6"),
                    (8, "d2", p2, "%7"),
                ]
                self.assertEqual(
                    self.tree(rows, ["%0", "%1", "%3", "%4", "%6", "%7"], layout),
                    [slot("%1"), *want],
                )

    def test_orphans_spanning_main_cells_dissolve(self):
        # the manager's session renamed from old to mgr
        for panes, per_column in (
            (["%0", "%1", "%2", "%3"], 3),
            (["%0", "%1", "%2", "%3"], 2),
            (["%0", "%1"], 3),
        ):
            with self.subTest(panes=panes, per_column=per_column):
                layout = grid([slot(p) for p in panes[1:]], per_column)
                rows = [(int(p[1:]) + 1, f"w{p[1:]}", "old", p) for p in panes[1:]]
                self.assertEqual(
                    self.tree(rows, panes, layouts.render_layout(layout)),
                    [slot(p) for p in panes[1:]],
                )


class GridLayoutTest(Case):
    """Window 160x48, manager %0 79 wide."""

    def tiles(self, cell):
        """Each container's children tile it exactly, one-cell borders between them."""
        if cell.kind == layouts.LEAF:
            return
        self.assertGreaterEqual(len(cell.children), 2)
        across = cell.kind == layouts.LEFT_RIGHT
        at = cell.x if across else cell.y
        for child in cell.children:
            if across:
                self.assertEqual((child.x, child.y, child.h), (at, cell.y, cell.h))
            else:
                self.assertEqual((child.y, child.x, child.w), (at, cell.x, cell.w))
            at += (child.w if across else child.h) + 1
            self.tiles(child)
        self.assertEqual(at - 1, cell.x + cell.w if across else cell.y + cell.h)

    def check(self, root, n, per_column, width=160, height=48):
        """FR-3: main cell i (pane %i+1) in column i // N, row i % N; equal widths and heights, each
        +-1.
        """
        self.tiles(root)
        self.assertEqual(
            (root.kind, root.w, root.h, root.x, root.y),
            (layouts.LEFT_RIGHT, width, height, 0, 0),
        )
        manager, area = root.children
        self.assertEqual((manager.pane, manager.h), ("%0", height))
        c = -(-n // per_column)
        if c > 1:
            self.assertEqual(area.kind, layouts.LEFT_RIGHT)
        columns = list(area.children) if c > 1 else [area]
        self.assertEqual(len(columns), c)
        self.assertLessEqual(max(col.w for col in columns) - min(col.w for col in columns), 1)
        for j, column in enumerate(columns):
            cells = list(column.children) if column.kind == layouts.TOP_BOTTOM else [column]
            self.assertEqual(
                [cell.pane for cell in cells],
                [f"%{i + 1}" for i in range(j * per_column, min(n, (j + 1) * per_column))],
            )
            self.assertLessEqual(max(cell.h for cell in cells) - min(cell.h for cell in cells), 1)

    def test_fr3_layouts(self):
        # the root: the manager, then the columns
        for per_column, n, want in (
            (3, 1, "160x48,0,0{79x48,0,0,0,80x48,80,0,1}"),
            (3, 2, "160x48,0,0{79x48,0,0,0,80x48,80,0[80x24,80,0,1,80x23,80,25,2]}"),
            (
                3,
                4,
                (
                    "160x48,0,0{79x48,0,0,0,80x48,80,0{40x48,80,0[40x16,80,0,1,40x15,80,17,2,40x15,80,33,3],"
                    "39x48,121,0,4}}"
                ),
            ),
            (
                3,
                7,
                (
                    "160x48,0,0{79x48,0,0,0,80x48,80,0{26x48,80,0[26x16,80,0,1,26x15,80,17,2,26x15,80,33,3],"
                    "26x48,107,0[26x16,107,0,4,26x15,107,17,5,26x15,107,33,6],26x48,134,0,7}}"
                ),
            ),
            (
                2,
                3,
                (
                    "160x48,0,0{79x48,0,0,0,80x48,80,0{40x48,80,0[40x24,80,0,1,40x23,80,25,2],39x48,121,0,3}}"
                ),
            ),
        ):
            with self.subTest(per_column=per_column, n=n):
                root = grid(n, per_column)
                self.assertEqual(body(root), want)
                self.check(root, n, per_column)

    def test_fr3_any_count(self):
        for per_column in (1, 2, 3, 4):
            for n in range(1, 13):
                with self.subTest(per_column=per_column, n=n):
                    self.check(grid(n, per_column), n, per_column)

    def test_manager_width_kept(self):
        for width in (1, 30, 100):
            with self.subTest(width=width):
                root = grid(4, manager_width=width)
                self.assertEqual(root.children[0], types.Cell(width, 48, 0, 0, "%0"))
                self.check(root, 4, 3)

    def test_compress_when_too_wide(self):
        root = grid(7, manager_width=158)
        self.assertEqual(root.children[0].w, 154)
        self.assertEqual([col.w for col in root.children[1].children], [1, 1, 1])
        self.check(root, 7, 3)
        self.assertEqual(
            body(grid([slot("%1", slot("%2"))], manager_width=200)),
            "160x48,0,0{156x48,0,0,0,3x48,157,0{1x48,157,0,1,1x48,159,0,2}}",
        )
        self.assertEqual(
            grid([slot("%1", slot("%2", slot("%3")))], manager_width=200).children[0].w, 152
        )

    def test_a_manager_as_wide_as_the_window_gets_half(self):
        for width in (160, 161):
            with self.subTest(width=width):
                root = grid(2, width=width, manager_width=width)
                self.assertEqual(root, grid(2, width=width, manager_width=(width - 1) // 2))
                self.assertEqual(root.children[0].w, (width - 1) // 2)
        self.assertEqual(grid(2, manager_width=159).children[0].w, 158)

    def test_infeasible(self):
        self.assertEqual(grid(7, width=7).children[0].w, 1)
        self.assertIsNone(grid(7, width=6))
        self.assertIsNotNone(grid(3, height=5))
        self.assertIsNone(grid(3, height=4))
        self.assertIsNone(grid([slot("%1", slot("%2"), slot("%3"))], height=2))
        self.assertIsNone(grid(1, manager_width=0))

    def test_sub_cells(self):
        for case, cells, want in (
            (
                "sub-workers: the right half, equal heights",
                [slot("%1"), slot("%2", slot("%3"), slot("%4")), slot("%5")],
                "160x48,0,0{79x48,0,0,0,80x48,80,0[80x16,80,0,1,80x15,80,17{40x15,80,17,2,"
                "39x15,121,17[39x7,121,17,3,39x7,121,25,4]},80x15,80,33,5]}",
            ),
            (
                "a grandchild halves the child's cell",
                [slot("%1"), slot("%2", slot("%3", slot("%6")), slot("%4")), slot("%5")],
                (
                    "160x48,0,0{79x48,0,0,0,80x48,80,0[80x16,80,0,1,80x15,80,17{40x15,80,17,2,"
                    "39x15,121,17[39x7,121,17{19x7,121,17,3,19x7,141,17,6},39x7,121,25,4]},80x15,80,33,5]}"
                ),
            ),
            (
                "an orphan group stacks full width",
                [slot("%1"), slot(None, slot("%3", slot("%6")), slot("%4")), slot("%5")],
                "160x48,0,0{79x48,0,0,0,80x48,80,0[80x16,80,0,1,80x15,80,17[80x7,80,17{"
                "40x7,80,17,3,39x7,121,17,6},80x7,80,25,4],80x15,80,33,5]}",
            ),
            (
                "one child: no container",
                [slot(None, slot("%3"))],
                "160x48,0,0{79x48,0,0,0,80x48,80,0,3}",
            ),
            (
                "one cell: no container",
                [slot("%1", slot("%2"))],
                "160x48,0,0{79x48,0,0,0,80x48,80,0{40x48,80,0,1,39x48,121,0,2}}",
            ),
        ):
            with self.subTest(case):
                root = grid(cells)
                self.assertEqual(body(root), want)
                self.tiles(root)

    def test_leaves_depth_first(self):
        root = grid(
            [slot("%1"), slot("%2", slot("%3", slot("%6")), slot("%4")), slot("%5"), slot("%7")]
        )
        self.assertEqual(
            [leaf.pane for leaf in layouts.leaf_cells(root)],
            ["%0", "%1", "%2", "%3", "%6", "%4", "%5", "%7"],
        )
        self.assertEqual(
            layouts.pane_ids(root),
            ["%0", "%1", "%2", "%3", "%6", "%4", "%5", "%7"],
        )

    def test_panes_require_leaf_pane(self):
        with self.assertRaises(AssertionError):
            layouts.pane_ids(types.Cell(1, 1, 0, 0))


class SwapsTest(Case):
    def test_every_order_of_four_correct_and_minimal(self):
        have = ["%0", "%1", "%2", "%3"]
        for want in itertools.permutations(have):
            with self.subTest(want=want):
                swaps = layouts.swaps(have, list(want))
                order = list(have)
                for source, target in swaps:
                    i, j = order.index(source), order.index(target)
                    order[i], order[j] = order[j], order[i]
                self.assertEqual(order, list(want))
                seen, cycles = set(), 0
                for start in have:
                    cycles += start not in seen
                    while start not in seen:
                        seen.add(start)
                        start = want[have.index(start)]
                self.assertEqual(len(swaps), len(have) - cycles)


class LayoutStringTest(Case):
    def test_parse(self):
        Cell, lr, tb = types.Cell, layouts.LEFT_RIGHT, layouts.TOP_BOTTOM
        want = Cell(
            160,
            48,
            0,
            0,
            kind=lr,
            children=(
                Cell(80, 48, 0, 0, "%0"),
                Cell(
                    79,
                    48,
                    81,
                    0,
                    kind=tb,
                    children=(Cell(79, 24, 81, 0, "%1"), Cell(79, 23, 81, 25, "%2")),
                ),
            ),
        )
        self.assertEqual(layouts.parse_layout(MAIN_VERTICAL), want)
        self.assertEqual(layouts.parse_layout(MAIN_VERTICAL.split(",", 1)[1]), want)

    def test_round_trip(self):
        for sample in (MAIN_VERTICAL, TILED_SPLIT):
            self.assertEqual(layouts.render_layout(layouts.parse_layout(sample)), sample)

    def test_malformed(self):
        text = MAIN_VERTICAL.split(",", 1)[1]
        for bad in (
            "",
            "7f31",
            "7f31,",
            "0000," + text,
            "7F31," + text,
            text[:-1],
            text + "}",
            text + ",1x1,0,0,9",
            text.replace("]", "}"),
            "160x48,0,0",
            "160x48,0,0{}",
            "160x48,0,0,%0",
            "160x48,0,0[1x1,0,0,1x1,0,2,2]",
            "160x48,0,0,1\n",
        ):
            with self.subTest(bad=bad):
                self.assertIsNone(layouts.parse_layout(bad))

    def test_json_matches_the_old_format(self):
        for case, sample in (
            ("main-vertical", MAIN_VERTICAL),
            ("tiled split", TILED_SPLIT),
            ("13 workers", layouts.render_layout(grid(13, 3))),
        ):
            with self.subTest(case):
                cell = layouts.parse_layout(sample)
                self.assertIsNotNone(cell)
                self.assertEqual(layouts.parse_layout(json_layout(cell)), cell)
                self.assertEqual(layouts.parse_layout(" \n" + json_layout(cell)), cell)

    def test_json_tmux_3_8_shape(self):
        # as tmux 3.8 printed it on the mini (2026-10-25): no checksum, keys i, a, l beside I
        printed = (
            '{"V":2,"L":{"t":"h","w":212,"h":58,"x":0,"y":0,"c":['
            '{"t":"p","w":106,"h":58,"x":0,"y":0,"i":0,"a":true,"I":"%0"},'
            '{"t":"v","w":105,"h":58,"x":107,"y":0,"c":['
            '{"t":"p","w":105,"h":3,"x":107,"y":0,"i":1,"l":1,"I":"%32"},'
            '{"t":"p","w":105,"h":54,"x":107,"y":4,"i":2,"l":2,"I":"%33"}]}]}}'
        )
        Cell, lr, tb = types.Cell, layouts.LEFT_RIGHT, layouts.TOP_BOTTOM
        want = Cell(
            212,
            58,
            0,
            0,
            kind=lr,
            children=(
                Cell(106, 58, 0, 0, "%0"),
                Cell(
                    105,
                    58,
                    107,
                    0,
                    kind=tb,
                    children=(Cell(105, 3, 107, 0, "%32"), Cell(105, 54, 107, 4, "%33")),
                ),
            ),
        )
        self.assertEqual(layouts.parse_layout(printed), want)
        self.assertEqual(
            layouts.render_layout(want),
            "5d6b,212x58,0,0{106x58,0,0,0,105x58,107,0[105x3,107,0,32,105x54,107,4,33]}",
        )

    def test_json_malformed(self):
        good = json.loads(json_layout(layouts.parse_layout(MAIN_VERTICAL)))
        pane = good["L"]["c"][0]

        def edit(where, change):
            def run():
                doc = copy.deepcopy(good)
                change(where(doc))
                return json.dumps(doc)

            return run

        root, first, right = (
            lambda d: d,
            lambda d: d["L"]["c"][0],
            lambda d: d["L"]["c"][1],
        )
        for case, bad in (
            ("not JSON", lambda: "{"),
            ("trailing text", lambda: json.dumps(good) + "x"),
            ("two values", lambda: json.dumps(good) + json.dumps(good)),
            ("a list", lambda: json.dumps([good])),
            ("a string", lambda: json.dumps("{}")),
            ("no V", edit(root, lambda d: d.pop("V"))),
            ("V 1", edit(root, lambda d: d.update(V=1))),
            ("V 3", edit(root, lambda d: d.update(V=3))),
            ("V true", edit(root, lambda d: d.update(V=True))),
            ("V string", edit(root, lambda d: d.update(V="2"))),
            ("no L", edit(root, lambda d: d.pop("L"))),
            ("L a list", edit(root, lambda d: d.update(L=[pane]))),
            ("unknown t", edit(first, lambda c: c.update(t="f"))),
            ("no t", edit(first, lambda c: c.pop("t"))),
            ("t a list", edit(first, lambda c: c.update(t=["p"]))),
            *(
                (f"{key} {how}", edit(first, lambda c, k=key, v=value: c.update({k: v})))
                for key in "whxy"
                for how, value in (("-1", -1), ("1.0", 1.0), ("true", True), ("string", "1"))
            ),
            *((f"no {key}", edit(right, lambda c, k=key: c.pop(k))) for key in "whxy"),
            ("pane without I", edit(first, lambda c: c.pop("I"))),
            ("I without %", edit(first, lambda c: c.update(I="0"))),
            ("I not a pane id", edit(first, lambda c: c.update(I="%x"))),
            ("I a number", edit(first, lambda c: c.update(I=0))),
            ("pane with c", edit(first, lambda c: c.update(c=[]))),
            ("container without c", edit(right, lambda c: c.pop("c"))),
            ("c not a list", edit(right, lambda c: c.update(c=pane))),
            ("one child", edit(right, lambda c: c.update(c=c["c"][:1]))),
            ("no children", edit(right, lambda c: c.update(c=[]))),
            ("child not an object", edit(right, lambda c: c["c"].append("%3"))),
            ("floating pane", edit(first, lambda c: c.update(z=0))),
            ("z on a container", edit(right, lambda c: c.update(z=1))),
            ("deep nesting", lambda: '{"V":2,"L":' + '{"c":' * 100000 + "1" + "}" * 100001),
        ):
            with self.subTest(case):
                self.assertIsNone(layouts.parse_layout(bad()))
