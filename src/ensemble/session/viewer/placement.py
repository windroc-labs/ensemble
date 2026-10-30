"""Tmux worker pane placement and caller context."""

from __future__ import annotations

import contextlib
import itertools
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Sequence

from ensemble.session.engine.tmux_cli import (
    CLIENTS,
    NAME,
    PANE,
    TmuxCli,
    TuiError,
    absent,
    error,
    session_name,
)
from ensemble.session.viewer.grid import (
    GRID_BUSY,
    GRID_PANES,
    GRID_ROW,
    HOOK_PATH,
    PER_COLUMN,
    RETILE,
    WINDOW,
    WORKER,
    WORKER_ATTACH,
    WORKER_LABEL,
    WORKER_OPENER,
    Grid,
    hook_command,
    validate_per_column,
    validate_retile,
    worker_panes,
)
from ensemble.session.viewer.types import (
    CloseEntry,
    Retile,
    Split,
    Spot,
    WorkerPane,
)

PLACEHOLDERS = {"session"}
SHOW_TIMEOUT = 30


SLOT = re.compile(r"\{\{([^{}]*)\}\}")
HOOK_SID = re.compile(r"[A-Za-z0-9_.:-]+")
LISTED = "#{S:#{W:#{P: #{pane_id} }}}"
HOOK_ENTRY = re.compile(r"after-kill-pane(?:\[([0-9]+)\](?: (.*))?)?")
CLOSE_ENTRY = re.compile(  # _close_hook as show-hooks prints it
    r"if-shell -F .#\{m:\* (%[0-9]+) \*,.* .set-hook -u -w -t @[0-9]+ "
    r"after-kill-pane\[([0-9]+)\] ; run-shell -b .\S+ hide --if-closed --sid .*"
)
SPLIT = "#{pane_id}\t#{window_id}"
LABEL = f"#{{?#{{{WORKER_LABEL}}},[#{{{WORKER_LABEL}}}],}}"
BORDERS = {"pane-border-status": "bottom", "pane-border-format": LABEL}
SAVED = "@worker-saved-"  # +option: its own value under a label border, unset if none
LABELS = f"#{{window_id}}\t#{{{WORKER_LABEL}}}\t#{{pane-border-format}}"


NO_PANE = "no anchor pane: {}"


def _layout(
    split: str | None, split_from: str | None, opener: str | None, fallbacks: Sequence[str]
) -> None:
    if split is not None and split not in tuple(Split):
        raise TuiError(f"split must be one of {', '.join(Split)}, not {split!r}")
    if split_from is not None:
        session_name(split_from)
    for who in (opener, *fallbacks):
        if who is not None and not NAME.fullmatch(who):
            raise TuiError(f"invalid opener {who!r}: want a tmux session name")


def _newest(name: str, workers: Sequence[WorkerPane]) -> str | None:
    found = [w.pane for w in workers if w.name == name]
    return max(found, key=lambda p: int(p[1:])) if found else None


def _close_hook(
    command: str,
    name: str,
    pane: str,
    window: str,
    *,
    sid: str | None,
    generation: str | None,
    index: int,
) -> str | None:
    """after-kill-pane[index]'s hide of `name`'s run once pane is gone; None off the allow-lists."""
    if not (
        HOOK_PATH.fullmatch(command)
        and NAME.fullmatch(name)
        and PANE.fullmatch(pane)
        and WINDOW.fullmatch(window)
        and sid is not None
        and HOOK_SID.fullmatch(sid)
        and generation is not None
        and HOOK_SID.fullmatch(generation)
    ):
        return None
    run = (
        f"run-shell -b '{command} hide --if-closed --sid {sid} --generation {generation} -- "
        f"{name} >/dev/null 2>&1 || true'"
    )
    unset = f"set-hook -u -w -t {window} after-kill-pane[{index}]"
    return f"if-shell -F '#{{m:* {pane} *,{LISTED}}}' '' \"{unset} ; {run}\""


class Placement:
    def __init__(self, *, tmux: TmuxCli, grid: Grid) -> None:
        self.tmux, self.grid = tmux, grid

    def open_pane(
        self,
        name: str,
        argv: Sequence[str],
        printed: str,
        *,
        sid: str | None = None,
        generation: str | None = None,
        split: str | None = None,
        split_from: str | None = None,
        opener: str | None = None,
        per_column: int | None = None,
        retile: str | None = None,
        label: bool = False,
        fallbacks: Sequence[str] = (),
    ) -> str | None:
        """Keeps `name`'s worker pane, else splits one running argv at its spot; why it failed."""
        _layout(split, split_from, opener, fallbacks)
        validate_per_column(per_column)
        validate_retile(retile)
        own = new = window = None
        try:
            if opener is None:
                try:
                    opener, own = self.opener(), True
                except TuiError:
                    if split_from is None:
                        raise
            workers = self.workers()
            if any(w.name == name and w.attach == printed for w in workers):
                return None  # an earlier run's pane, still closing, is not this run's
            spot = self._spot(
                name,
                opener,
                own=own,
                split=split,
                split_from=split_from,
                workers=workers,
                fallbacks=fallbacks,
            )
            if spot.grid is not None:
                if split is not None or split_from is not None:
                    print("tui: show: grid: --split/--split-from ignored", file=sys.stderr)
                # muted: an old mode's hook tile could land after a later drag; _grid_up unmutes
                self.tmux.run(["set-option", "-w", "-t", spot.grid[0], GRID_BUSY, spot.grid[0]])
            try:
                new, window, why = self._split(argv, spot.side, spot.pane)
                if new is not None and window is not None:
                    # tagged before any tile
                    self._record(
                        name,
                        opener,
                        new,
                        window,
                        printed=printed,
                        sid=sid,
                        generation=generation,
                        label=label,
                    )
            finally:
                if new is None and spot.grid is not None:
                    with contextlib.suppress(TuiError):
                        self.tmux.run(["set-option", "-u", "-w", "-t", spot.grid[0], GRID_BUSY])
        except TuiError as e:
            return f"{e}; watch it with {printed}"
        if new is None or window is None:
            return why
        if spot.grid is None:
            return None
        return self.grid.configure(
            *spot.grid,
            per_column=PER_COLUMN if per_column is None else per_column,
            retile=RETILE if retile is None else Retile(retile),
        )

    def workers(self) -> list[WorkerPane]:
        return worker_panes(self.tmux)

    def panes(self, name: str) -> list[WorkerPane]:
        """name's live worker panes; TuiError when tmux fails."""
        return [w for w in self.workers() if w.name == name]

    def close(self, worker: WorkerPane) -> None:
        """Kills worker's pane, its close hooks unset first; a gone pane is no error."""
        args = []
        try:
            _, ours, _ = self._entries(worker.window)
        except TuiError as e:
            print(f"tui: close: after-kill-pane: {e}", file=sys.stderr)
            ours = []
        for entry in ours:
            if entry.pane == worker.pane:
                args += ["set-hook", "-u", "-w", "-t", worker.window]
                args += [f"after-kill-pane[{entry.index}]", ";"]
        res = self.tmux.run([*args, "kill-pane", "-t", worker.pane])
        if res.returncode and not absent(res):
            raise TuiError(f"tmux: {error(res)}")
        self.unlabel()

    def unlabel(self) -> None:
        """Gives windows left without a labelled pane their own borders back; failures print."""
        try:
            res = self.tmux.run(["list-panes", "-a", "-F", LABELS])
            if res.returncode:
                if absent(res):
                    return
                raise TuiError(f"tmux: {error(res)}")
            rows = [line.split("\t", 2) for line in res.stdout.splitlines()]
            if any(len(row) != 3 or not WINDOW.fullmatch(row[0]) for row in rows):
                raise TuiError(f"tmux: list-panes printed {res.stdout!r:.80}")
            labelled = {window for window, label, _ in rows if label}
            for window in sorted({w for w, _, shown in rows if shown == LABEL} - labelled):
                if self._own(window, "pane-border-format") == LABEL:
                    args = []
                    for option in BORDERS:
                        args += self._set(window, option, self._own(window, SAVED + option))
                        args += [";", "set-option", "-u", "-w", "-t", window, SAVED + option]
                    self.tmux.checked(args[1:])
        except TuiError as e:
            print(f"tui: close: label: {e}", file=sys.stderr)

    def _own(self, window: str, option: str) -> str | None:
        """window's own value of option (None: unset)."""
        out = self.tmux.checked(["show-options", "-w", "-q", "-v", "-t", window, option]).stdout
        if not out:
            return None
        if out.count("\n") != 1 or not out.endswith("\n"):
            raise TuiError(f"tmux: show-options printed {out!r:.80}")
        return out[:-1]

    def _set(self, window: str, option: str, value: str | None) -> list[str]:
        if value is None:
            return [";", "set-option", "-u", "-w", "-t", window, option]
        if value.endswith(";"):
            raise TuiError(f"tmux would misread {value!r:.80}")
        return [";", "set-option", "-w", "-t", window, option, value]

    def _label_args(self, window: str) -> list[str]:
        """Calls setting window's label border, saving its own unless already labelled."""
        args = []
        own = self._own(window, "pane-border-format")
        if own != LABEL:
            for option in BORDERS:
                value = own if option == "pane-border-format" else self._own(window, option)
                args += self._set(window, SAVED + option, value)
        for option, value in BORDERS.items():
            args += self._set(window, option, value)
        return args

    def _spot(
        self,
        name: str,
        opener: str | None,
        *,
        own: bool | None,
        split: str | None,
        split_from: str | None,
        workers: Sequence[WorkerPane],
        fallbacks: Sequence[str],
    ) -> Spot:
        at: tuple[str, str | None] | None = None
        failed: TuiError | None = None
        if opener is not None:
            try:
                at = self._opener_at(opener, own=own, workers=workers, fallbacks=fallbacks)
            except TuiError as e:
                failed = e
        if at is not None and (gridded := self._grid(*at)) is not None:
            return gridded
        side = Split(split or Split.RIGHT)
        if split_from is not None:
            return Spot(self.anchor(split_from, workers), side, None)
        if opener is None:
            raise TuiError(NO_PANE.format("not in tmux"))
        if split is None and (stacked := self._stack(name, opener, workers)) is not None:
            return Spot(stacked, Split.BELOW, None)
        if at is None:
            raise failed or TuiError(NO_PANE.format(f"no terminal shows tmux session {opener}"))
        return Spot(at[0], side, None)

    def _opener_at(
        self,
        opener: str,
        *,
        own: bool | None,
        workers: Sequence[WorkerPane],
        fallbacks: Sequence[str],
    ) -> tuple[str, str | None] | None:
        """The first of opener and fallbacks with a worker pane or a client: (pane, manager)."""
        for who in (opener, *fallbacks):
            if (pane := _newest(who, workers)) is not None:
                return pane, None
            if (pane := self._shown(who, own=own if who == opener else None)) is not None:
                return pane, pane
        return None

    def _grid(self, pane: str, manager: str | None) -> Spot | None:
        try:
            found = self._window(pane, manager)
        except TuiError:
            return None
        if found is None:
            return None
        window, manager, areas = found
        others = [p for p in areas if p != manager]
        if not others:
            return Spot(manager, Split.RIGHT, (window, manager))
        anchor = max(others, key=lambda p: (areas[p], int(p[1:])))
        return Spot(anchor, Split.BELOW, (window, manager))

    def _window(self, pane: str, manager: str | None) -> tuple[str, str, dict[str, int]] | None:
        """pane's window id, manager and pane areas; None when tmux fails."""
        res = self.tmux.run(["list-panes", "-t", pane, "-F", GRID_PANES])
        rows = []
        if res.returncode:
            return None
        for line in res.stdout.splitlines():
            match = GRID_ROW.fullmatch(line)
            if match is None:
                return None
            rows.append(match)
        if not rows:
            return None
        areas = {m[3]: int(m[4]) * int(m[5]) for m in rows}
        window, stored = rows[0].group(1, 2)
        manager = stored if stored in areas else manager
        return None if manager is None else (window, manager, areas)

    def _stack(self, name: str, opener: str, workers: Sequence[WorkerPane]) -> str | None:
        """The newest worker pane opener opened for another worker, if any."""
        found = [w.pane for w in workers if w.name != name and w.opener == opener]
        return max(found, key=lambda p: int(p[1:])) if found else None

    def _split(
        self, argv: Sequence[str], side: str, pane: str
    ) -> tuple[str | None, str | None, str | None]:
        """(new pane, its window, None), else (None, None, why)."""
        for arg in argv:
            if "#" in arg or arg.endswith(";"):
                raise TuiError(f"tmux would misread {arg!r}")
        try:
            res = self.tmux.run(
                [
                    *("split-window", "-d", "-h" if side == "right" else "-v"),
                    *("-P", "-F", SPLIT, "-t", pane, "--", *argv),
                ],
            )
        except TuiError as e:
            return None, None, str(e)
        if res.returncode:
            return None, None, f"tmux: {error(res)}"
        found = res.stdout.strip()
        new, _, window = found.partition("\t")
        if not (PANE.fullmatch(new) and WINDOW.fullmatch(window)):
            return None, None, f"tmux: split-window printed {found!r:.80}"
        return new, window, None

    def _record(
        self,
        name: str,
        opener: str | None,
        pane: str,
        window: str,
        *,
        printed: str,
        sid: str | None,
        generation: str | None,
        label: bool,
    ) -> None:
        """Tags pane as name's worker pane running `printed`; a failure is only printed."""
        if not (
            PANE.fullmatch(pane)
            and WINDOW.fullmatch(window)
            and NAME.fullmatch(name)
            and (opener is None or NAME.fullmatch(opener))
        ):
            print(
                f"tui: show: @worker: refused {pane!r:.20} {window!r:.20} {name!r:.80} "
                f"{opener!r:.80}",
                file=sys.stderr,
            )
            return
        args = ["set-option", "-p", "-t", pane, WORKER, name]
        args += [";", "set-option", "-p", "-t", pane, WORKER_ATTACH, printed]
        if opener is not None:
            args += [";", "set-option", "-p", "-t", pane, WORKER_OPENER, opener]
        if label:
            args += [";", "set-option", "-p", "-t", pane, WORKER_LABEL, name]
            try:
                args += self._label_args(window)
            except TuiError as e:
                print(f"tui: show: label: {e}", file=sys.stderr)
        command, problem = hook_command()
        if problem is None:
            try:
                args += self._close_args(
                    command, name, pane, window, sid=sid, generation=generation
                )
            except TuiError as e:
                problem = str(e)
        if problem is not None:
            print(f"tui: show: after-kill-pane: {problem}", file=sys.stderr)
        try:
            self.tmux.checked(args)
        except TuiError as e:
            print(f"tui: show: @worker: {e}", file=sys.stderr)

    def _close_args(
        self,
        command: str,
        name: str,
        pane: str,
        window: str,
        *,
        sid: str | None,
        generation: str | None,
    ) -> list[str]:
        """Calls unsetting window's gone panes' close hooks, setting pane's at a free index."""
        taken, ours, live = self._entries(window)
        gone = {entry.index for entry in ours if entry.pane not in live}
        used = taken - gone
        index = int(pane[1:])
        if index in used:
            index = next(i for i in itertools.count() if i not in used)
        hook = _close_hook(command, name, pane, window, sid=sid, generation=generation, index=index)
        if hook is None:
            raise TuiError(f"refused run {sid!r:.80} {generation!r:.80}")
        args = []
        for stale in sorted(gone):
            args += [";", "set-hook", "-u", "-w", "-t", window, f"after-kill-pane[{stale}]"]
        return [*args, ";", "set-hook", "-w", "-t", window, f"after-kill-pane[{index}]", hook]

    def _entries(self, window: str) -> tuple[set[int], list[CloseEntry], set[str]]:
        """window's after-kill-pane indexes, its close hooks and the live panes."""
        res = self.tmux.run(
            [
                *("show-hooks", "-w", "-t", window, "after-kill-pane"),
                *(";", "list-panes", "-a", "-F", "#{pane_id}"),
            ]
        )
        if res.returncode:
            if absent(res):
                return set(), [], set()
            raise TuiError(f"tmux: {error(res)}")
        taken, ours, live = set(), [], set()
        for line in res.stdout.splitlines():
            if PANE.fullmatch(line):
                live.add(line)
                continue
            entry = HOOK_ENTRY.fullmatch(line)
            if entry is None:
                raise TuiError(f"tmux: show-hooks printed {line!r:.80}")
            if entry[1] is None:
                continue
            taken.add(int(entry[1]))
            hook = CLOSE_ENTRY.fullmatch(entry[2] or "")
            if hook is not None and hook[2] == entry[1]:
                ours.append(CloseEntry(int(entry[1]), hook[1]))
        return taken, ours, live

    def run_command(self, session: str, template: str) -> str | None:
        """Runs template with /bin/sh, {{session}} the quoted name; why it failed, else None."""
        unknown = [m.group(0) for m in SLOT.finditer(template) if m.group(1) not in PLACEHOLDERS]
        if unknown:
            return f"unknown placeholder {unknown[0]}"
        try:
            res = self.tmux.runner(
                ["/bin/sh", "-c", SLOT.sub(lambda _match: shlex.quote(session), template)],
                stdin=subprocess.DEVNULL,
                timeout=SHOW_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return f"timed out after {SHOW_TIMEOUT} s"
        except OSError as e:
            return str(e)
        return f"exit status {res.returncode}" if res.returncode else None

    def own_session(self) -> str | None:
        """$TMUX_PANE's session; None outside tmux."""
        if not os.environ.get("TMUX"):
            return None
        pane = os.environ.get("TMUX_PANE", "")
        if not PANE.fullmatch(pane):
            raise TuiError(f"bad $TMUX_PANE {pane!r}")
        name = self.tmux.checked(
            ["display-message", "-p", "-t", pane, "#{session_name}"]
        ).stdout.strip()
        if not NAME.fullmatch(name):
            raise TuiError(f"own tmux session {name!r}: want [A-Za-z0-9_-]+")
        return name

    def opener(self) -> str:
        """The caller's tmux session; TuiError outside tmux."""
        own = self.own_session()
        if own is not None:
            return own
        raise TuiError(NO_PANE.format("not in tmux"))

    def anchor(
        self, split_from: str | None = None, workers: Sequence[WorkerPane] | None = None
    ) -> str:
        """split_from's newest worker pane, else the pane its tmux session shows, else the caller's
        session's; TuiError when none.
        """
        if split_from is not None:
            workers = self.workers() if workers is None else workers
            found = _newest(split_from, workers) or self._shown(split_from, own=False)
            if found is None:
                raise TuiError(f"no terminal shows {split_from}")
            return found
        own = self.own_session()
        if own is None:
            raise TuiError(NO_PANE.format("not in tmux"))
        found = self._shown(own, own=True)
        if found is None:
            raise TuiError(NO_PANE.format(f"no terminal shows tmux session {own}"))
        return found

    def _shown(self, session: str, *, own: bool | None) -> str | None:
        """The pane the most recent client of tmux session `session` shows; $TMUX_PANE when own."""
        clients = self._clients(session)
        if not clients:
            return None
        if own is None:
            own = self.own_session() == session
        return os.environ["TMUX_PANE"] if own else clients[0][1]

    def _clients(self, session: str) -> list[list[str]]:
        res = self.tmux.run(["list-clients", "-t", f"={session}", "-F", CLIENTS])
        if res.returncode:
            if absent(res):
                return []
            raise TuiError(f"tmux: {error(res)}")
        out = res.stdout
        return sorted(
            (
                c
                for c in (line.split("\t") for line in out.splitlines())
                if len(c) == 2 and c[0].isdigit() and PANE.fullmatch(c[1])
            ),
            key=lambda c: int(c[0]),
            reverse=True,
        )
