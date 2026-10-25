"""Assertions for a private tmux server grid and its cell geometry."""

from typing import NamedTuple


class Box(NamedTuple):
    """right and bottom exclusive: a neighbour past the 1-cell border starts at right + 1."""

    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


def _union(boxes: list[Box]) -> Box:
    return Box(
        min(b.left for b in boxes),
        min(b.top for b in boxes),
        max(b.right for b in boxes),
        max(b.bottom for b in boxes),
    )


def _sessions(cell) -> list[str]:
    return (
        [cell]
        if isinstance(cell, str)
        else [cell[0], *cell[1]]
        if isinstance(cell, tuple)
        else list(cell)
    )


class GridCheck:
    def __init__(self, case, server, manager, columns):
        self.case, self.server, self.manager, self.columns = case, server, manager, columns
        self.window, self.width, self.stored = (
            server._out(
                "display-message",
                "-p",
                "-t",
                f"={manager}:",
                "#{window_id}\t#{window_width}\t#{@grid-manager}",
            )
            .rstrip("\n")
            .split("\t")
        )
        self.panes = server.panes(self.window)
        self.at = dict(
            line.split("\t", 1)
            for line in server._out("list-sessions", "-F", "#{session_name}\t#{@pane}").splitlines()
        )
        names = {pane: name for name, pane in self.at.items() if pane in self.panes}
        names[self.stored] = f"{manager} (manager)"
        self.layout = "\n".join(
            f"  {p} {names.get(p, '?')}: left {x} top {y} {w}x{h}"
            for p, (x, y, w, h) in sorted(self.panes.items(), key=lambda kv: kv[1][:2])
        )

    def fail(self, problem):
        self.case.fail(
            f"grid: {problem}\n want {self.columns!r}\n window {self.window} {self.width} wide:\n{self.layout}"
        )

    def box(self, name):
        if self.at.get(name) not in self.panes:
            self.fail(f"{name}: @pane {self.at.get(name)!r} is no pane of the window")
        x, y, w, h = self.panes[self.at[name]]
        return Box(x, y, x + w, y + h)

    def stacked(self, boxes, what):
        if any(a.bottom >= b.top for a, b in zip(boxes, boxes[1:], strict=False)):
            self.fail(f"{what}: not stacked top to bottom in the order given")

    def cell_box(self, cell):
        if isinstance(cell, str):
            return self.box(cell)
        if isinstance(cell, tuple) and len(cell) == 2 and isinstance(cell[1], list) and cell[1]:
            return self.parent_cell(cell)
        if isinstance(cell, list) and cell:
            return self.orphan_cell(cell)
        self.fail(f"bad cell {cell!r}: want a name, (parent, [subs…]) or [subs…]")

    def parent_cell(self, cell):
        parent, subs = self.box(cell[0]), [self.box(s) for s in cell[1]]
        whole = _union([parent, *subs])
        if (parent.left, parent.top, parent.bottom) != (whole.left, whole.top, whole.bottom):
            self.fail(f"{cell!r}: the parent is not at the cell's left, full height")
        if len({s.left for s in subs}) != 1 or subs[0].left <= parent.right:
            self.fail(f"{cell!r}: the subs share no left right of the parent")
        self.stacked(subs, repr(cell))
        if (subs[0].top, subs[-1].bottom) != (whole.top, whole.bottom):
            self.fail(f"{cell!r}: the subs are not full cell height")
        return whole

    def orphan_cell(self, cell):
        subs = [self.box(s) for s in cell]
        whole = _union(subs)
        if any((s.left, s.right) != (whole.left, whole.right) for s in subs):
            self.fail(f"{cell!r}: the orphan group's subs are not full cell width")
        self.stacked(subs, repr(cell))
        return whole

    def manager_box(self, manager_width):
        first = min(self.panes, key=lambda p: int(p[1:]))
        if self.stored != first:
            self.fail(f"@grid-manager {self.stored!r} is not the window's first pane {first}")
        x, y, w, h = self.panes[self.stored]
        manager = Box(x, y, x + w, y + h)
        if manager.left != 0 or manager_width not in (None, manager.width):
            self.fail(
                f"manager at left {manager.left}, {manager.width} wide; want left 0, width {manager_width or 'any'}"
            )
        return manager

    def ownership(self):
        owned = [
            self.at[name] for column in self.columns for cell in column for name in _sessions(cell)
        ]
        if len(set(owned)) != len(owned):
            self.fail("two sessions given share a pane")
        if extra := sorted(set(self.panes) - {self.stored, *owned}):
            self.fail(f"panes of no session given: {extra}")

    def column_bounds(self, cells, manager):
        boxes = [_union(column) for column in cells]
        if boxes and (boxes[0].left != manager.right + 1 or boxes[-1].right != int(self.width)):
            self.fail(
                f"the columns span {boxes[0].left} to {boxes[-1].right}; want {manager.right + 1} to {self.width}"
            )
        if any(a.right >= b.left for a, b in zip(boxes, boxes[1:], strict=False)):
            self.fail("the columns overlap or are out of order")
        if boxes and max(b.width for b in boxes) - min(b.width for b in boxes) > 1:
            self.fail(f"column widths {[b.width for b in boxes]} differ by more than 1")

    def column(self, number, cells, manager):
        if any((c.left, c.right) != (cells[0].left, cells[0].right) for c in cells):
            self.fail(f"column {number}: its cells differ in left or width")
        self.stacked(cells, f"column {number}")
        if (cells[0].top, cells[-1].bottom) != (manager.top, manager.bottom):
            self.fail(
                f"column {number}: spans {cells[0].top} to {cells[-1].bottom}; want the manager's {manager.top} to {manager.bottom}"
            )
        if max(c.height for c in cells) - min(c.height for c in cells) > 1:
            self.fail(
                f"column {number}: cell heights {[c.height for c in cells]} differ by more than 1"
            )

    def check(self, manager_width):
        manager = self.manager_box(manager_width)
        if not all(self.columns):
            self.fail("an empty column")
        cells = [[self.cell_box(cell) for cell in column] for column in self.columns]
        self.ownership()
        self.column_bounds(cells, manager)
        for number, column in enumerate(cells, 1):
            self.column(number, column, manager)
        return True
