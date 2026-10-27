"""Pure tmux layout codec and grid geometry."""

from __future__ import annotations

import json
import re

from ensemble.session.viewer.types import (
    Cell,
    Slot,
)

LEAF, LEFT_RIGHT, TOP_BOTTOM = "", "{}", "[]"


LAYOUT_CELL = re.compile(r"([0-9]+)x([0-9]+),([0-9]+),([0-9]+)(?:,([0-9]+)|([{[]))")


def _checksum(body: str) -> str:
    csum = 0
    for byte in body.encode():
        csum = ((csum >> 1) + ((csum & 1) << 15) + byte) & 0xFFFF
    return f"{csum:04x}"


def parse_layout(text: str) -> Cell | None:
    """The cell tree of tmux 3.8's JSON, `<checksum>,<body>` or a body; None when malformed or
    floating.
    """
    if text.lstrip().startswith("{"):
        try:
            doc = json.loads(text)
            if not isinstance(doc, dict) or type(doc.get("V")) is not int or doc["V"] != 2:
                return None
            return _json_cell(doc.get("L"))
        except (ValueError, RecursionError):
            return None
    csum, sep, body = text.partition(",")
    if sep and re.fullmatch(r"[0-9a-f]{4}", csum):
        if _checksum(body) != csum:
            return None
        text = body
    try:
        cell, end = _parse_cell(text, 0)
    except ValueError:
        return None
    return cell if end == len(text) else None


def _parse_cell(text: str, at: int) -> tuple[Cell, int]:
    m = LAYOUT_CELL.match(text, at)
    if m is None:
        raise ValueError(at)
    w, h, x, y = (int(v) for v in m.group(1, 2, 3, 4))
    if m.group(5) is not None:
        return Cell(w, h, x, y, f"%{m.group(5)}"), m.end()
    kind = LEFT_RIGHT if m.group(6) == "{" else TOP_BOTTOM
    at, children = m.end(), []
    while True:
        child, at = _parse_cell(text, at)
        children.append(child)
        if text.startswith(kind[1], at):
            return Cell(w, h, x, y, kind=kind, children=tuple(children)), at + 1
        if not text.startswith(",", at):
            raise ValueError(at)
        at += 1


def _json_cell(node: object) -> Cell:
    """t h (side by side), v (stacked) or p (pane I); ValueError when malformed or floating (z)."""
    if not isinstance(node, dict) or "z" in node:
        raise ValueError(node)
    size = [v for v in (node.get(k) for k in "whxy") if type(v) is int and v >= 0]
    if len(size) != 4:
        raise ValueError(node)
    w, h, x, y = size
    t = node.get("t")
    kind = {"h": LEFT_RIGHT, "v": TOP_BOTTOM, "p": LEAF}.get(t) if isinstance(t, str) else None
    children = node.get("c")
    if kind == LEAF:
        pane = node.get("I")
        if "c" in node or not isinstance(pane, str) or not re.fullmatch(r"%[0-9]+", pane):
            raise ValueError(node)
        return Cell(w, h, x, y, pane)
    if kind is None or not isinstance(children, list) or len(children) < 2:
        raise ValueError(node)
    return Cell(w, h, x, y, kind=kind, children=tuple(_json_cell(c) for c in children))


def render_layout(cell: Cell) -> str:
    body = _body(cell)
    return f"{_checksum(body)},{body}"


def _body(c: Cell) -> str:
    head = f"{c.w}x{c.h},{c.x},{c.y}"
    if c.kind == LEAF:
        assert c.pane is not None
        return f"{head},{c.pane[1:]}"
    return head + c.kind[0] + ",".join(_body(child) for child in c.children) + c.kind[1]


def leaf_cells(cell: Cell) -> list[Cell]:
    """Depth first: select-layout's pane order."""
    return (
        [cell]
        if cell.kind == LEAF
        else [leaf for child in cell.children for leaf in leaf_cells(child)]
    )


def pane_ids(cell: Cell) -> list[str]:
    panes: list[str] = []
    for leaf in leaf_cells(cell):
        assert leaf.pane is not None
        panes.append(leaf.pane)
    return panes


def _paths(cell: Cell, above: tuple[Cell, ...] = ()) -> dict[str, list[Cell]]:
    if cell.kind == LEAF:
        assert cell.pane is not None
        return {cell.pane: [*above, cell]}
    return {
        pane: path
        for child in cell.children
        for pane, path in _paths(child, (*above, cell)).items()
    }


def _natural(name: str) -> list[str | int]:
    """Text and number chunks: w2 before w10."""
    return [int(c) if i % 2 else c for i, c in enumerate(re.split(r"([0-9]+)", name))]


def grid_tree(
    manager: str,
    panes: list[str],
    sessions: list[tuple[int, str, str, str]],
    layout: Cell,
) -> list[Slot]:
    """The main grid's cells in order: a session's node sits in its @opener's, an orphan group (its
    @opener gone) where the layout shows it, the rest in main; main by _natural name, siblings by
    pane id, manual panes last.
    """
    owner = {}
    for _, name, opener, pane in sorted(sessions):
        if pane in panes and pane != manager and pane not in owner:
            owner[pane] = name, opener
    node = {name: pane for pane, (name, _) in owner.items()}
    names = {row[1] for row in sessions}
    parent = {}  # a node's pane, or an orphan group's name -> its parent's; None: main
    for pane, (_, opener) in owner.items():
        parent[pane] = node.get(opener, opener if opener and opener not in names else None)

    def under(key) -> list[str]:
        return [
            *([key] if key in owner else []),
            *(p for k, up in parent.items() if up == key for p in under(k)),
        ]

    paths = _paths(layout)
    rank = {pane: i for i, pane in enumerate(paths)}
    columns = (
        layout.children[1]
        if layout.kind == LEFT_RIGHT
        and len(layout.children) == 2
        and layout.children[0].pane == manager
        else layout
    )
    groups = sorted({up for up in parent.values() if up is not None and up not in owner})
    spans = {q: set(under(q)) for q in groups}
    mains = [p for p in panes if p != manager and parent.get(p) is None]
    for q in groups:
        path = paths[min(spans[q])]
        for pane in spans[q]:
            path = [a for a, b in zip(path, paths[pane], strict=False) if a is b]
        if path[-1] is layout or path[-1] is columns:
            parent.update((k, None) for k, up in list(parent.items()) if up == q)
            continue
        parent[q] = None
        for cell in reversed(path):
            head = cell.children[0] if cell.kind == LEFT_RIGHT else None
            if head is None or head.kind != LEAF or head.pane in spans[q]:
                continue
            # one main pane
            if (
                head.pane == manager
                or cell is columns
                and (len(cell.children) != 2 or mains != [head.pane])
            ):
                break
            if head.pane in owner:
                parent[q] = head.pane
                break
    reached, todo = set(), [None]
    while todo:
        key = todo.pop()
        found = [k for k, up in parent.items() if up == key and k not in reached]
        reached.update(found)
        todo += found
    parent.update((k, None) for k in list(parent) if k not in reached and parent[k] not in spans)

    def first(key) -> int:
        return min(rank[p] for p in under(key))

    def slots(key) -> list[Slot]:
        if key is None:
            order = sorted(
                (k for k, up in parent.items() if up is None),
                key=lambda k: _natural(owner[k][0] if k in owner else k),
            )
        else:
            live = sorted(
                (k for k, up in parent.items() if up == key and k in owner),
                key=lambda p: int(p[1:]),
            )
            order = list(live)
            for q in sorted(
                (k for k, up in parent.items() if up == key and k not in owner), key=first
            ):
                later = next((k for k in live if first(k) > first(q)), None)
                order.insert(len(order) if later is None else order.index(later), q)
        return [Slot(k if k in owner else None, tuple(slots(k))) for k in order]

    manual = sorted((p for p in panes if p != manager and p not in owner), key=lambda p: int(p[1:]))
    return [*slots(None), *(Slot(p) for p in manual)]


def grid_layout(
    cells: list[Slot], manager: str, *, width: int, height: int, manager_width: int, per_column: int
) -> Cell | None:
    """The manager left at manager_width (narrowed to fit), cells in columns of per_column; None: it
    does not fit.
    """
    columns = tuple(
        Slot(None, tuple(cells[i : i + per_column])) for i in range(0, len(cells), per_column)
    )
    c = len(columns)
    mw = min(
        manager_width if manager_width != width else (width - 1) // 2,
        width - 1 - (c * max(_min_width(s) for s in cells) + c - 1),
    )
    if mw < 1:
        return None
    root = Cell(
        width,
        height,
        0,
        0,
        kind=LEFT_RIGHT,
        children=(
            Cell(mw, height, 0, 0, manager),
            _line(
                columns,
                LEFT_RIGHT,
                w=width - mw - 1,
                h=height,
                x=mw + 1,
                y=0,
            ),
        ),
    )
    return root if all(leaf.h >= 1 for leaf in leaf_cells(root)) else None


def _min_width(s: Slot) -> int:
    if not s.children:
        return 1
    least = max(_min_width(child) for child in s.children)
    return least if s.pane is None else 2 * least + 1


def _place(s: Slot, *, w: int, h: int, x: int, y: int) -> Cell:
    """A pane takes the left half, its children stack right; an orphan group stacks."""
    if not s.children:
        return Cell(w, h, x, y, s.pane)
    if s.pane is None:
        return _line(s.children, TOP_BOTTOM, w=w, h=h, x=x, y=y)
    left = w // 2
    return Cell(
        w,
        h,
        x,
        y,
        kind=LEFT_RIGHT,
        children=(
            Cell(left, h, x, y, s.pane),
            _line(
                s.children,
                TOP_BOTTOM,
                w=w - left - 1,
                h=h,
                x=x + left + 1,
                y=y,
            ),
        ),
    )


def _line(slots: tuple[Slot, ...], kind: str, *, w: int, h: int, x: int, y: int) -> Cell:
    """Side by side or stacked, equal sizes, the remainder to the first."""
    if len(slots) == 1:
        return _place(slots[0], w=w, h=h, x=x, y=y)
    across = kind == LEFT_RIGHT
    size, extra = divmod((w if across else h) - len(slots) + 1, len(slots))
    children, at = [], x if across else y
    for i, s in enumerate(slots):
        n = size + (i < extra)
        children.append(
            _place(s, w=n, h=h, x=at, y=y) if across else _place(s, w=w, h=n, x=x, y=at)
        )
        at += n + 1
    return Cell(w, h, x, y, kind=kind, children=tuple(children))


def swaps(have: list[str], want: list[str]) -> list[tuple[str, str]]:
    """The fewest swap-pane pairs turning index order have into want."""
    have, swaps = list(have), []
    for i, pane in enumerate(want):
        if have[i] != pane:
            j = have.index(pane)
            swaps.append((pane, have[i]))
            have[i], have[j] = pane, have[i]
    return swaps
