"""Subprocess runs with a timeout, their stderr shown with URL userinfo masked; process-group
kills (kill_group), piped starts (piped_popen), timed output probes (probe) and shell resume commands.
"""

import contextlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
from collections.abc import Callable, Mapping, Sequence

from ensemble.utils.environment import environment_ok

SHORT, LONG = 60, 600
STOP_WAIT = 5
PROMPT_ARGV_MAX = 100_000  # JSON bytes; exec takes 128 KiB per arg
USERINFO = re.compile(r"//[^@/\s]+@")

Runner = Callable[[list[str], int], subprocess.CompletedProcess]


class Stopped(BaseException):
    def __init__(self, code: int):
        super().__init__(code)
        self.code = code


def kill_group(proc: subprocess.Popen, *, ended: bool = False, wait: float = STOP_WAIT) -> None:
    # Closed pipes and a reaped process mean the group is gone and its id may be reused.
    if not (ended and proc.returncode is not None):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=wait)


def over_argv(text: str) -> bool:
    return len(json.dumps(text, ensure_ascii=False).encode("utf-8")) > PROMPT_ARGV_MAX


def piped_popen(
    argv, *, env: Mapping[str, str], cwd: str | None = None, stderr: int = subprocess.PIPE
) -> subprocess.Popen:
    return subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=stderr,
        text=True,
        errors="replace",
        process_group=0,
    )


def probe(argv: Sequence[str], *, cwd: str, env: Mapping[str, str], timeout: float) -> str | None:
    try:
        proc = piped_popen(argv, cwd=cwd, env=env, stderr=subprocess.DEVNULL)
    except (OSError, ValueError):
        return None
    try:
        return proc.communicate(timeout=timeout)[0]
    except subprocess.TimeoutExpired:
        kill_group(proc)
        return None
    finally:
        assert proc.stdout is not None
        proc.stdout.close()


def sh(
    argv: list[str], timeout: int, *, env: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, env=env
    )


def redact(text: str) -> str:
    return USERINFO.sub("//***@", text)


def err_text(res: subprocess.CompletedProcess) -> str:
    return redact((res.stderr or "").strip())[:200]


def command_path(name: str) -> str | None:
    """`name` where PATH finds it, absolute; None when PATH has none."""
    found = shutil.which(name)
    return None if found is None else os.path.abspath(found)


def resume_command(cwd: str, env: Mapping[str, str], argv) -> str:
    if not environment_ok(env):
        raise ValueError("resume environment must map variable names to strings without NUL")
    if all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in env):
        assign = "".join(f"{name}={shlex.quote(value)} " for name, value in env.items())
    else:
        assign = "env -- " + "".join(
            shlex.quote(f"{name}={value}") + " " for name, value in env.items()
        )
    return f"cd {shlex.quote(cwd)} && {assign}{shlex.join(argv)}"
