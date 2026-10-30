"""Tmux through an injected subprocess runner."""

from __future__ import annotations

import re
import subprocess


class TuiError(Exception):
    pass


NAME = re.compile(r"[A-Za-z0-9_-]+")


PANE = re.compile(r"%[0-9]+")


ABSENT = re.compile(
    r"can't find (session|pane|window).*|no such session: .*|no server running.*|"
    r"error connecting to .* \(No such file or directory\)",
    re.S,
)


CLIENTS = "#{client_activity}\t#{pane_id}"


def session_name(session: str) -> str:
    if not NAME.fullmatch(session):
        raise TuiError(f"invalid session name {session!r}: want [A-Za-z0-9_-]+")
    return session


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

    def has_session(self, name: str) -> bool:
        """Whether tmux session `name` exists; False without a server."""
        res = self.run(["has-session", "-t", f"={session_name(name)}"])
        if res.returncode == 0:
            return True
        if absent(res):
            return False
        raise TuiError(f"tmux: {error(res)}")
