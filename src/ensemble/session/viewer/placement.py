"""Tmux display placement and caller context."""

from __future__ import annotations

import contextlib
import os
import re
import shlex
import shutil
import subprocess
import sys

from ensemble.session.engine.tmux_cli import (
    CLIENTS,
    NAME,
    PANE,
    PANES,
    SESSION_ID,
    SESSIONS,
    TmuxCli,
    TuiError,
    attach_command,
    error,
    session_name,
)
from ensemble.session.viewer.grid import (
    GRID_BUSY,
    GRID_PANES,
    GRID_ROW,
    PER_COLUMN,
    RETILE,
    WINDOW,
    Grid,
    hook_command,
    validate_per_column,
    validate_retile,
)
from ensemble.session.viewer.types import (
    Retile,
    Split,
    TmuxAnchor,
)

PLACEHOLDERS = {"session"}
SHOW_TIMEOUT = 30


SLOT = re.compile(r"\{\{([^{}]*)\}\}")
TTY = re.compile(r"/dev/[A-Za-z0-9/]+")
LISTED = "#{S:#{W:#{P: #{pane_id} }}}"


NO_PANE = "no anchor pane: {}"


def _layout(split: str | None, split_from: str | None, opener: str | None = None) -> None:
    if split is not None and split not in tuple(Split):
        raise TuiError(f"split must be one of {', '.join(Split)}, not {split!r}")
    if split_from is not None:
        session_name(split_from)
    if opener is not None and not NAME.fullmatch(opener):
        raise TuiError(f"invalid opener {opener!r}: want a tmux session name")


class Placement:
    def __init__(self, *, tmux: TmuxCli, grid: Grid) -> None:
        self.tmux, self.grid = tmux, grid

    def open_pane(
        self,
        session: str,
        *,
        split: str | None = None,
        split_from: str | None = None,
        opener: str | None = None,
        per_column: int | None = None,
        retile: str | None = None,
    ) -> str | None:
        """Reuses a shown pane, else splits in its grid, stack or explicit anchor; returns failure."""
        _layout(split, split_from, opener)
        validate_per_column(per_column)
        validate_retile(retile)
        tmux = shutil.which("tmux")
        if tmux is None:
            return "tmux not found"
        tmux = os.path.abspath(tmux)
        own = grid = new = None
        try:
            if opener is None:
                try:
                    opener, own = self.opener(), True
                except TuiError:
                    if split_from is None:
                        raise
            sessions = self._sessions() if opener is not None else []
            if opener is not None and self._open(session, opener, sessions):
                return None
            grid = self._grid(opener, sessions, own=own) if opener is not None else None
            if grid is not None:
                window, manager, at, side = grid
                if split is not None or split_from is not None:
                    print("tui: show: grid: --split/--split-from ignored", file=sys.stderr)
                # muted: an old mode's hook tile could land after a later drag; _grid_up unmutes
                self.tmux.run(["set-option", "-w", "-t", window, GRID_BUSY, window])
                try:
                    new, why = self._split(session, side, at, tmux)
                finally:
                    if new is None:
                        with contextlib.suppress(TuiError):
                            self.tmux.run(["set-option", "-u", "-w", "-t", window, GRID_BUSY])
            elif split_from is not None:
                new, why = self._split(session, split or "right", self.anchor(split_from), tmux)
            elif split is not None:
                if opener is None:
                    raise TuiError(NO_PANE.format("not in tmux"))
                new, why = self._split(session, split, self._opener_pane(opener, own=own), tmux)
            else:
                if opener is None:
                    raise TuiError(NO_PANE.format("not in tmux"))
                new, why = self._stack(session, opener, sessions, tmux) or self._split(
                    session, "right", self._opener_pane(opener, own=own), tmux
                )
        except TuiError as e:
            return f"{e}; watch it with {attach_command(session)}"
        if new is None:
            return why
        self._record(session, opener, new)
        if grid is None:
            return None
        return self.grid.configure(
            window,
            manager,
            per_column=PER_COLUMN if per_column is None else per_column,
            retile=RETILE if retile is None else Retile(retile),
        )

    def _sessions(self) -> list[list[str]]:
        res = self.tmux.run(["list-sessions", "-F", SESSIONS])
        lines = res.stdout.split("\n") if res.returncode == 0 else []
        return [
            f
            for f in (line.split("\t") for line in lines)
            if len(f) == 5 and SESSION_ID.fullmatch(f[0])
        ]

    def _open(self, session: str, opener: str, sessions: list[list[str]]) -> bool:
        """Whether a pane opener opened still shows session: its @opener and live tmux @pane."""
        row = next((f for f in sessions if f[1] == session), None)
        if row is None or row[2] != opener or not PANE.fullmatch(row[3]):
            return False
        try:
            panes = self.tmux.checked(["list-panes", "-a", "-F", "#{pane_id}"]).stdout.split()
            return row[3] in panes
        except TuiError:
            return False

    def _grid(
        self, opener: str, sessions: list[list[str]], *, own: bool | None
    ) -> tuple[str, str, TmuxAnchor, str] | None:
        """Grid window, manager, anchor and side; None when no grid is found."""
        row = next((f for f in sessions if f[1] == opener), None)
        if row is None:
            return None
        try:
            found = self._window(row[3], None) if PANE.fullmatch(row[3]) else None
            if found is None:
                clients = [] if NAME.fullmatch(row[2]) else self._clients(opener)
                if not clients:
                    return None
                if own is None:
                    own = self.own_session() == opener
                pane = os.environ["TMUX_PANE"] if own else clients[0][2]
                found = self._window(pane, pane)
        except TuiError:
            return None
        if found is None:
            return None
        window, manager, areas, socket = found
        workers = [p for p in areas if p != manager]
        if not workers:
            return window, manager, TmuxAnchor(manager, socket), "right"
        return (
            window,
            manager,
            TmuxAnchor(max(workers, key=lambda p: (areas[p], int(p[1:]))), socket),
            "below",
        )

    def _window(
        self, pane: str, manager: str | None
    ) -> tuple[str, str, dict[str, int], str] | None:
        """pane's window id, manager, pane areas and socket; None when tmux fails."""
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
        return None if manager is None else (window, manager, areas, rows[0][6])

    def _stack(
        self, session: str, opener: str, sessions: list[list[str]], tmux: str
    ) -> tuple[str | None, str | None] | None:
        """None when no @pane of the opener's other sessions is open."""
        rows = [
            (int(f[0][1:]), f[3], f[4])
            for f in sessions
            if f[1] != session and f[2] == opener and PANE.fullmatch(f[3])
        ]
        if rows:
            res = self.tmux.run(["list-panes", "-a", "-F", "#{pane_id}"])
            live = set(res.stdout.split("\n")) if res.returncode == 0 else set()
            for _, pane, socket in sorted(rows, reverse=True):
                if pane in live:
                    return self._split(session, "below", TmuxAnchor(pane, socket), tmux)
        return None

    def _split(
        self, session: str, side: str, a: TmuxAnchor, tmux: str
    ) -> tuple[str | None, str | None]:
        command = [
            "/usr/bin/env",
            "-u",
            "TMUX",
            tmux,
            "-S",
            a.socket,
            "attach",
            "-t",
            f"={session}",
        ]  # argv: as a sh -c string it died at once (tmux 3.7)
        for arg in command:
            if "#" in arg or arg.endswith(";"):
                raise TuiError(f"tmux would misread {arg!r}")
        try:
            res = self.tmux.run(
                [
                    "split-window",
                    "-d",
                    "-h" if side == "right" else "-v",
                    "-P",
                    "-F",
                    "#{pane_id}",
                    "-t",
                    a.pane,
                    *command,
                ],
            )
        except TuiError as e:
            return None, str(e)
        return (None, f"tmux: {error(res)}") if res.returncode else (res.stdout.strip(), None)

    def _record(self, session: str, opener: str | None, pane: str) -> None:
        """For the next placement; a failure is only printed."""
        if not PANE.fullmatch(pane):
            print(f"tui: show: @pane: not a pane id: {pane!r}", file=sys.stderr)
            return
        for key, value in (("@opener", opener), ("@pane", pane)):
            if value is None:
                continue
            try:
                self.tmux.checked(["set-option", "-t", f"={session}:", key, value])
            except TuiError as e:
                print(f"tui: show: {key}: {e}", file=sys.stderr)
                return

    def watch(self, session: str) -> None:
        """Tracks @window and hooks: detach hides a closed view; arrivals forget a gone pane,
        whose tty may be reused. Failures are printed."""
        command, problem = hook_command()
        target = f"={session}:"
        try:
            pane = self.tmux.option(session, "@pane") or ""
            if not PANE.fullmatch(pane):
                return
            found = self.tmux.checked(
                ["display-message", "-p", "-t", pane, "#{window_id} #{pane_tty}"]
            ).stdout.strip()
            window, _, tty = found.partition(" ")
            if not (WINDOW.fullmatch(window) and TTY.fullmatch(tty)):
                raise TuiError(f"not a window id and tty: {found!r:.80}")
            kept = f"#{{||:#{{!=:#{{@pane}},{pane}}},#{{m:* {pane} *,{LISTED}}}}}"
            stale = f"if-shell -F -t {target} '{kept}' '' \"{_unset(target)}\""
            args = ["set-option", "-t", target, "@window", window]
            for arrived in ("client-attached", "client-session-changed"):
                args += [";", "set-hook", "-t", target, arrived, stale]
            if not problem:
                run = f"run-shell -b '{command} hide --if-closed {session} >/dev/null 2>&1 || true'"
                # a client's name is its tty: the viewer's is its pane's
                hook = f"if-shell -F '#{{==:#{{hook_client}},{tty}}}' \"{run}\""
                args += [";", "set-hook", "-t", target, "client-detached", hook]
            self.tmux.checked(args)
        except TuiError as e:
            print(f"tui: show: @window: {e}", file=sys.stderr)
            return
        if problem:
            print(f"tui: show: client-detached: {problem}", file=sys.stderr)

    def shown(self, session: str) -> str | None:
        """The live tmux pane that show opened (@pane), if any."""
        pane = self.tmux.option(session, "@pane")
        live = pane is not None and PANE.fullmatch(pane) and self.tmux.process(pane) is not None
        return pane if live else None

    def closed(self, session: str) -> str | None:
        """The tmux pane show opened (@pane), once it is gone and the window it was in (@window)
        remains; else None.
        """
        pane, window = self.tmux.option(session, "@pane"), self.tmux.option(session, "@window")
        if not (PANE.fullmatch(pane or "") and WINDOW.fullmatch(window or "")):
            return None
        panes = self.tmux.checked(["list-panes", "-a", "-F", "#{pane_id}"]).stdout.split()
        windows = self.tmux.checked(["list-windows", "-a", "-F", "#{window_id}"]).stdout.split()
        return pane if pane not in panes and window in windows else None

    def forget(self, session: str, pane: str | None = None) -> bool:
        """Atomically unsets @pane and @window only if pane still matches; returns whether unset."""
        pane = self.tmux.option(session, "@pane") if pane is None else pane
        if not PANE.fullmatch(pane or ""):
            return False
        target = f"={session}:"
        self.tmux.checked(
            ["if-shell", "-F", "-t", target, f"#{{==:#{{@pane}},{pane}}}", _unset(target)]
        )
        return self.tmux.option(session, "@pane") is None

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

    def anchor(self, split_from: str | None = None) -> TmuxAnchor:
        """split_from's shown pane, else the caller's session's; TuiError when none."""
        if split_from is not None:
            found = self._shown(split_from, own=False)
            if found is None:
                raise TuiError(f"no terminal shows tmux session {split_from}")
            return found
        own = self.own_session()
        if own is None:
            raise TuiError(NO_PANE.format("not in tmux"))
        return self._opener_pane(own, own=True)

    def _opener_pane(self, opener: str, *, own: bool | None) -> TmuxAnchor:
        if own is None:
            own = self.own_session() == opener
        found = self._shown(opener, own=own)
        if found is None:
            raise TuiError(NO_PANE.format(f"no terminal shows tmux session {opener}"))
        return found

    def _shown(self, session: str, *, own: bool) -> TmuxAnchor | None:
        """The pane the most recent client shows the session in: a nested client's host pane,
        $TMUX_PANE when own, else the client's; a control-mode client gets its tmux pane only.
        """
        clients = self._clients(session)
        if not clients:
            return None
        _, tty, pane, control, socket = clients[0]
        pane = os.environ["TMUX_PANE"] if own else pane
        if control == "1":
            return TmuxAnchor(pane, socket, session)
        hosts = {
            f[1]: f[2]
            for f in (
                line.split(" ", 2)
                for line in self.tmux.checked(["list-panes", "-a", "-F", PANES]).stdout.splitlines()
            )
            if len(f) == 3 and f[0] != "1"
        }
        if PANE.fullmatch(hosts.get(tty, "")):
            return TmuxAnchor(hosts[tty], socket, session)
        return TmuxAnchor(pane, socket, session)

    def _clients(self, session: str) -> list[list[str]]:
        out = self.tmux.checked(["list-clients", "-t", f"={session}", "-F", CLIENTS]).stdout
        return sorted(
            (
                c
                for c in (line.split(" ", 4) for line in out.splitlines())
                if len(c) == 5
                and c[0].isdigit()
                and c[1]
                and PANE.fullmatch(c[2])
                and c[3] in ("0", "1")
                and c[4]
            ),
            key=lambda c: int(c[0]),
            reverse=True,
        )


def _unset(target: str) -> str:
    return f"set-option -u -t {target} @pane ; set-option -u -t {target} @window"
