"""Tmux worker pane placement and caller context."""

from __future__ import annotations

import contextlib
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
    Retile,
    Split,
    WorkerPane,
)

PLACEHOLDERS = {"session"}
SHOW_TIMEOUT = 30


SLOT = re.compile(r"\{\{([^{}]*)\}\}")
HOOK_SID = re.compile(r"[A-Za-z0-9_.:-]+")
LISTED = "#{S:#{W:#{P: #{pane_id} }}}"
CLOSE_ENTRY = re.compile(r"after-kill-pane\[([0-9]+)\] ")
SPLIT = "#{pane_id}\t#{window_id}"
LABEL = f"#{{?#{{{WORKER_LABEL}}},[#{{{WORKER_LABEL}}}],}}"


NO_PANE = "no anchor pane: {}"


def _layout(split: str | None, split_from: str | None, opener: str | None = None) -> None:
    if split is not None and split not in tuple(Split):
        raise TuiError(f"split must be one of {', '.join(Split)}, not {split!r}")
    if split_from is not None:
        session_name(split_from)
    if opener is not None and not NAME.fullmatch(opener):
        raise TuiError(f"invalid opener {opener!r}: want a tmux session name")


def _newest(name: str, workers: Sequence[WorkerPane]) -> str | None:
    """The highest-numbered worker pane of `name`, if any."""
    found = [w.pane for w in workers if w.name == name]
    return max(found, key=lambda p: int(p[1:])) if found else None


def _close_hook(
    command: str, name: str, pane: str, window: str, *, sid: str | None, generation: str | None
) -> str | None:
    """after-kill-pane[N]'s: once pane is gone, unsets itself and hides run sid/generation of
    `name`; None when a value fails its allow-list.
    """
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
    unset = f"set-hook -u -w -t {window} after-kill-pane[{pane[1:]}]"
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
    ) -> str | None:
        """Keeps a worker pane of `name`, else splits one running argv in its grid, stack or
        explicit anchor (label: its name in the window's pane border); returns failure.
        """
        _layout(split, split_from, opener)
        validate_per_column(per_column)
        validate_retile(retile)
        own = grid = new = window = None
        try:
            if opener is None:
                try:
                    opener, own = self.opener(), True
                except TuiError:
                    if split_from is None:
                        raise
            try:
                workers = self.workers()
            except TuiError:
                workers = []
            if any(w.name == name and w.attach == printed for w in workers):
                return None  # an earlier run's pane, still closing, is not this run's
            grid = self._grid(opener, workers, own=own) if opener is not None else None
            if grid is not None:
                grid_window, manager, at, side = grid
                if split is not None or split_from is not None:
                    print("tui: show: grid: --split/--split-from ignored", file=sys.stderr)
                # muted: an old mode's hook tile could land after a later drag; _grid_up unmutes
                self.tmux.run(["set-option", "-w", "-t", grid_window, GRID_BUSY, grid_window])
                try:
                    new, window, why = self._split(argv, side, at)
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
                    if new is None:
                        with contextlib.suppress(TuiError):
                            self.tmux.run(["set-option", "-u", "-w", "-t", grid_window, GRID_BUSY])
            elif split_from is not None:
                new, window, why = self._split(argv, split or "right", self.anchor(split_from))
            elif split is not None:
                if opener is None:
                    raise TuiError(NO_PANE.format("not in tmux"))
                new, window, why = self._split(
                    argv, split, self._opener_pane(opener, own=own, workers=workers)
                )
            else:
                if opener is None:
                    raise TuiError(NO_PANE.format("not in tmux"))
                stacked = self._stack(name, opener, workers)
                new, window, why = (
                    self._split(argv, "below", stacked)
                    if stacked is not None
                    else self._split(
                        argv, "right", self._opener_pane(opener, own=own, workers=workers)
                    )
                )
        except TuiError as e:
            return f"{e}; watch it with {printed}"
        if new is None or window is None:
            return why
        if grid is None:
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
            return None
        return self.grid.configure(
            grid_window,
            manager,
            per_column=PER_COLUMN if per_column is None else per_column,
            retile=RETILE if retile is None else Retile(retile),
        )

    def workers(self) -> list[WorkerPane]:
        return worker_panes(self.tmux)

    def panes(self, name: str) -> list[WorkerPane]:
        """name's live worker panes; TuiError when tmux fails."""
        return [w for w in self.workers() if w.name == name]

    def close(self, worker: WorkerPane) -> None:
        """Kills worker's pane, its close hook unset first; a gone pane is no error."""
        res = self.tmux.run(
            [
                *("set-hook", "-u", "-w", "-t", worker.window),
                f"after-kill-pane[{worker.pane[1:]}]",
                *(";", "kill-pane", "-t", worker.pane),
            ]
        )
        if res.returncode and not absent(res):
            raise TuiError(f"tmux: {error(res)}")

    def _grid(
        self, opener: str, workers: Sequence[WorkerPane], *, own: bool | None
    ) -> tuple[str, str, str, str] | None:
        """Grid window, manager, anchor pane and side; None when no grid is found."""
        shown = _newest(opener, workers)
        try:
            if shown is not None:
                found = self._window(shown, None)
            else:
                clients = self._clients(opener)
                if not clients:
                    return None
                if own is None:
                    own = self.own_session() == opener
                pane = os.environ["TMUX_PANE"] if own else clients[0][1]
                found = self._window(pane, pane)
        except TuiError:
            return None
        if found is None:
            return None
        window, manager, areas = found
        others = [p for p in areas if p != manager]
        if not others:
            return window, manager, manager, "right"
        return window, manager, max(others, key=lambda p: (areas[p], int(p[1:]))), "below"

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
        """Tags pane as name's worker pane running `printed`, with its close hook and (label) its
        name in its window's bottom pane border; a failure is only printed.
        """
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
            args += [
                *(";", "set-option", "-p", "-t", pane, WORKER_LABEL, name),
                *(";", "set-option", "-w", "-t", window, "pane-border-status", "bottom"),
                *(";", "set-option", "-w", "-t", window, "pane-border-format", LABEL),
            ]
        command, problem = hook_command()
        hook = (
            None
            if problem
            else _close_hook(command, name, pane, window, sid=sid, generation=generation)
        )
        if hook is None:
            why = problem or f"refused run {sid!r:.80} {generation!r:.80}"
            print(f"tui: show: after-kill-pane: {why}", file=sys.stderr)
        else:
            for index in self._gone_entries(window):
                args += [";", "set-hook", "-u", "-w", "-t", window, f"after-kill-pane[{index}]"]
            args += [";", "set-hook", "-w", "-t", window, f"after-kill-pane[{pane[1:]}]", hook]
        try:
            self.tmux.checked(args)
        except TuiError as e:
            print(f"tui: show: @worker: {e}", file=sys.stderr)

    def _gone_entries(self, window: str) -> list[str]:
        """Each M of window's after-kill-pane[M] whose %M is gone; none when tmux fails."""
        try:
            res = self.tmux.run(
                [
                    *("show-hooks", "-w", "-t", window, "after-kill-pane"),
                    *(";", "list-panes", "-a", "-F", "#{pane_id}"),
                ]
            )
        except TuiError:
            return []
        if res.returncode:
            return []
        lines = res.stdout.splitlines()
        live = {line for line in lines if PANE.fullmatch(line)}
        found = (CLOSE_ENTRY.match(line) for line in lines)
        return [m[1] for m in found if m is not None and f"%{m[1]}" not in live]

    def run_command(self, session: str, template: str) -> str | None:
        """Runs template with /bin/sh, each {{session}} the quoted session name; why it failed, else
        None.
        """
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

    def anchor(self, split_from: str | None = None) -> str:
        """split_from's newest worker pane, else the pane its tmux session shows, else the caller's
        session's; TuiError when none.
        """
        if split_from is not None:
            found = _newest(split_from, self.workers()) or self._shown(split_from, own=False)
            if found is None:
                raise TuiError(f"no terminal shows {split_from}")
            return found
        own = self.own_session()
        if own is None:
            raise TuiError(NO_PANE.format("not in tmux"))
        return self._opener_pane(own, own=True)

    def _opener_pane(
        self, opener: str, *, own: bool | None, workers: Sequence[WorkerPane] = ()
    ) -> str:
        """opener's newest worker pane, else the pane its tmux session shows."""
        if (found := _newest(opener, workers)) is not None:
            return found
        if own is None:
            own = self.own_session() == opener
        found = self._shown(opener, own=own)
        if found is None:
            raise TuiError(NO_PANE.format(f"no terminal shows tmux session {opener}"))
        return found

    def _shown(self, session: str, *, own: bool) -> str | None:
        """The pane the most recent client of tmux session `session` shows; $TMUX_PANE when own."""
        clients = self._clients(session)
        if not clients:
            return None
        return os.environ["TMUX_PANE"] if own else clients[0][1]

    def _clients(self, session: str) -> list[list[str]]:
        out = self.tmux.checked(["list-clients", "-t", f"={session}", "-F", CLIENTS]).stdout
        return sorted(
            (
                c
                for c in (line.split("\t") for line in out.splitlines())
                if len(c) == 2 and c[0].isdigit() and PANE.fullmatch(c[1])
            ),
            key=lambda c: int(c[0]),
            reverse=True,
        )
