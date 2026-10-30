"""Tmux through an injected subprocess runner and sleep; listings skip VIEW sessions."""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess

from ensemble.session.engine.types import (
    HIDDEN_SIZE,
    PaneStatus,
)


class TuiError(Exception):
    pass


NAME = re.compile(r"[A-Za-z0-9_-]+")


PANE = re.compile(r"%[0-9]+")


SESSION_ID = re.compile(r"\$[0-9]+")


ABSENT = re.compile(
    r"can't find (session|pane).*|no such session: .*|no server running.*|"
    r"error connecting to .* \(No such file or directory\)",
    re.S,
)


CLIENTS = "#{client_activity} #{client_tty} #{pane_id} #{client_control_mode} #{socket_path}"


PANES = "#{pane_dead} #{pane_tty} #{pane_id}"


PROCESS_ID = re.compile(r"[0-9]+ \$[0-9]+ %[0-9]+ [0-9]+")
PROCESS_GONE = re.compile(r"[0-9]+ (\$[0-9]+)? (%[0-9]+ [0-9]*| )| *")


USER_OPTION = re.compile(r"@[A-Za-z0-9_-]+")


PROCESS = "#{pid} #{session_id} #{pane_id} #{pane_pid}"


DEAD = "#{pane_dead} #{pane_dead_status} #{pane_dead_signal}"


SESSIONS = "#{session_id}\t#{session_name}\t#{@opener}\t#{@pane}\t#{socket_path}"


VIEW = "@view"
VIEWLESS = ["-f", f"#{{==:#{{{VIEW}}},}}"]
VIEWS = ["-f", f"#{{!=:#{{{VIEW}}},}}"]


def sized(size: tuple[int, int], *, border: bool = False) -> tuple[str, ...]:
    """tmux's -x/-y for a pane of size (columns, rows), border: under a border row."""
    return ("-x", str(size[0]), "-y", str(size[1] + border))


LISTING = "#{session_name}\t#{@opener}"


SHOWN = "#{client_activity}\t#{client_tty}\t#{session_name}"


LISTED_PANES = "#{pane_dead}\t#{pane_tty}\t#{session_name}\t#{pane_id}"


def attach_command(session: str) -> str:
    """`tmux attach -t '=<session>'`, after $TUI_ATTACH_PREFIX when not blank."""
    prefix = os.environ.get("TUI_ATTACH_PREFIX", "").strip()
    return f"{prefix} tmux attach -t '={session}'" if prefix else f"tmux attach -t '={session}'"


def session_name(session: str) -> str:
    if not NAME.fullmatch(session):
        raise TuiError(f"invalid session name {session!r}: want [A-Za-z0-9_-]+")
    return session


def address(target: str, suffix: str = ":") -> str:
    if PANE.fullmatch(target):
        return target
    if not NAME.fullmatch(target):
        raise TuiError(
            f"invalid target {target!r}: want a session name [A-Za-z0-9_-]+ or a pane id %N"
        )
    return f"={target}{suffix}"


def error(res: subprocess.CompletedProcess) -> str:
    return (res.stderr or "").strip()


def absent(res: subprocess.CompletedProcess) -> bool:
    return bool(ABSENT.fullmatch(error(res)))


def display_error(value) -> str | None:
    """Why value is no display (None: it is), keeping a tab-separated listing row one row."""
    if value is None or (
        isinstance(value, str) and value.isprintable() and not value.endswith(";")
    ):
        return None
    return f"display {value!r:.80}: want printable text not ending in ;"


def _out_of_mode(target: str, *command: str) -> list[str]:
    """copy-mode -q first: a pane in a mode (scrolled back) would swallow the keys."""
    return ["copy-mode", "-q", "-t", target, ";", *command]


def _process_id(found: str) -> str | None:
    """found: as tmux printed it, less its newline (unstripped); None: its pane is going away."""
    if PROCESS_ID.fullmatch(found):
        return found
    if PROCESS_GONE.fullmatch(found):
        return None
    raise TuiError(f"bad tmux process {found!r:.80}")


def status(session: str, *, on: bool) -> list[str]:
    target = f"={session}:"
    command = ["set-option", "-t", target, "status", "on" if on else "off"]
    if on:
        command += [
            ";",
            "set-option",
            "-t",
            target,
            "status-left-length",
            str(len(f"[{session}] ")),
        ]
    return command


class TmuxCli:
    def __init__(self, runner=subprocess.run) -> None:
        self.runner = runner

    def checked(self, args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
        res = self.run(args, stdin)
        if res.returncode:
            raise TuiError(f"tmux: {error(res)}")
        return res

    def run(self, args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
        try:
            if stdin is not None:
                return self.runner(["tmux", *args], capture_output=True, text=True, input=stdin)
            return self.runner(
                ["tmux", *args], capture_output=True, text=True, stdin=subprocess.DEVNULL
            )
        except OSError as e:
            raise TuiError(f"tmux: {e}") from e

    def read(self, target: str, lines: int | None = None) -> str:
        """The visible text, or the last `lines` with history; trailing blank lines dropped."""
        args = ["capture-pane", "-p", "-J", "-t", address(target)]
        if lines is not None:
            if isinstance(lines, bool) or not isinstance(lines, int) or lines < 1:
                raise TuiError(f"lines must be a positive int, not {lines!r}")
            args += ["-S", f"-{lines}"]
        text = self.checked(args).stdout.splitlines()
        while text and not text[-1].strip():
            text.pop()
        return "\n".join(text if lines is None else text[-lines:])

    def status(self, target: str):
        """RUNNING, None (target or server absent), UNKNOWN, or a dead pane's status (signal n: -n)."""
        target = address(target)
        try:
            res = self.run(["display-message", "-p", "-t", target, DEAD])
        except TuiError:
            return PaneStatus.UNKNOWN
        if res.returncode:
            return None if absent(res) else PaneStatus.UNKNOWN
        return self._dead_status(res.stdout.rstrip("\n"))

    def _dead_status(self, text: str):
        fields = text.split(" ")
        if fields == ["", "", ""]:  # tmux (3.7) exits 0 with empty fields for a missing session
            return None
        if len(fields) != 3 or fields[0] not in ("0", "1"):
            return PaneStatus.UNKNOWN
        dead, code, sig = fields
        if dead == "0":
            return PaneStatus.RUNNING
        if not (code or sig):
            with contextlib.suppress(TuiError):
                self.run(["run-shell", "-b", "true"])
            return PaneStatus.RUNNING
        if sig:  # libc's sys_signame (macOS: "kill"), else the number
            found = (
                int(sig) if sig.isdecimal() else signal.Signals.__members__.get(f"SIG{sig.upper()}")
            )
            return PaneStatus.UNKNOWN if found is None else -found
        return int(code) if code.isdecimal() else PaneStatus.UNKNOWN

    def kill(self, target: str) -> None:
        if PANE.fullmatch(target):
            self.checked(["kill-pane", "-t", target])
        else:
            self.checked(["kill-session", "-t", address(target, "")])

    def unview(
        self, session: str, size: tuple[int, int] = HIDDEN_SIZE, *, border: bool = False
    ) -> None:
        target = address(session)
        self.checked(
            ["resize-window", "-t", target, *sized(size, border=border), ";"]
            + ["set-option", "-w", "-u", "-t", target, "window-size"]
        )

    def keys(self, target: str, *names: str) -> None:
        target = address(target)
        for name in names:
            if name.startswith("-") or name.endswith(";"):
                raise TuiError(f"tmux would misread {name!r}")
        self.checked(_out_of_mode(target, "send-keys", "-t", target, *names))

    def press(self, target: str, *names: str) -> None:
        """Key names after `--`, so a `-` key is no flag; `;` must come escaped (`\\;`)."""
        target = address(target)
        for name in names:
            if not name or name.endswith(";") and not name.endswith("\\;"):
                raise TuiError(f"tmux would misread {name!r}")
        self.checked(_out_of_mode(target, "send-keys", "-t", target, "--", *names))

    def option(self, target: str, name: str) -> str | None:
        """None when unset or the target absent."""
        target = address(target)
        if not USER_OPTION.fullmatch(name):
            raise TuiError(f"option {name!r}: want {USER_OPTION.pattern}")
        res = self.run(["show-options", "-v", "-t", target, name])
        if res.returncode:
            if error(res).startswith("invalid option:") or absent(res):
                return None
            raise TuiError(f"tmux: {error(res)}")
        return res.stdout.removesuffix("\n")

    def process(self, target: str) -> str | None:
        """PROCESS of target's pane (a session's active one); None when absent."""
        res = self.run(["display-message", "-p", "-t", address(target), PROCESS])
        if res.returncode:
            if absent(res):
                return None
            raise TuiError(f"tmux: {error(res)}")
        return _process_id(res.stdout.removesuffix("\n"))

    def sessions(self) -> list[str] | None:
        """Names: [] without a server, None when tmux fails."""
        try:
            found = self._table(["list-sessions", *VIEWLESS, "-F", "#{session_name}"], 1)
        except TuiError:
            return None
        return [name for (name,) in found if name]

    def listing(self) -> list[dict]:
        """Viewer sessions and their placement; worker identity belongs to the guard."""
        found = self._table(["list-sessions", *VIEWS, "-F", LISTING], 2)
        rows = sorted(f for f in found if NAME.fullmatch(f[0]))
        if not rows:
            return []
        clients = self._table(["list-clients", "-F", SHOWN], 3)
        recent = {}
        for _, tty, session in sorted(
            (c for c in clients if c[0].isdigit() and c[1]), key=lambda c: int(c[0])
        ):
            recent[session] = tty
        hosts = {}
        if any(name in recent for name, _ in rows):
            panes = self._table(["list-panes", "-a", "-F", LISTED_PANES], 4)
            hosts = {
                tty: session if NAME.fullmatch(session) else ""
                for dead, tty, session, _ in panes
                if dead != "1"
            }
        return [
            {
                "name": name,
                "opener": parent if NAME.fullmatch(parent) else None,
                "shown": None if name not in recent else hosts.get(recent[name], ""),
            }
            for name, parent in rows
        ]

    def _table(self, args: list[str], width: int) -> list[list[str]]:
        res = self.run(["-u", *args])
        if res.returncode:
            if absent(res):
                return []
            why = " ".join(error(res).split())
            raise TuiError(f"tmux {args[0]} exited {res.returncode}" + (f": {why}" if why else ""))
        return [f for f in (line.split("\t") for line in res.stdout.split("\n")) if len(f) == width]
