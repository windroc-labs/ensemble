"""Standard-library test isolation, child environments, a tree's project import roots and owned
process cleanup.
"""

import atexit
import contextlib
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

CONTROL_GRACE = 15
_TMUX_DIRECTORIES: dict[int, str] = {}
_TMUX_LOCK = threading.Lock()


def _after_fork():
    global _TMUX_LOCK
    _TMUX_LOCK = threading.Lock()
    _TMUX_DIRECTORIES.clear()


os.register_at_fork(after_in_child=_after_fork)


def _remove_tmux_directory(pid, directory):
    if os.getpid() == pid and os.path.isdir(directory) and not os.path.islink(directory):
        try:
            stop_tmux_servers(directory, {"PATH": os.environ.get("PATH", os.defpath)})
        finally:
            shutil.rmtree(directory, ignore_errors=True)


def _tmux_directory():
    pid = os.getpid()
    with _TMUX_LOCK:
        if pid not in _TMUX_DIRECTORIES or not os.path.isdir(_TMUX_DIRECTORIES[pid]):
            sandbox = os.environ.get("ENSEMBLE_TEST_SANDBOX") or None
            directory = tempfile.mkdtemp(prefix="tmux-", dir=sandbox)
            if sandbox is None:
                atexit.register(_remove_tmux_directory, pid, directory)
            _TMUX_DIRECTORIES[pid] = directory
        return _TMUX_DIRECTORIES[pid]


def project_roots(repo: str) -> tuple[str, ...]:
    """Each <repo>/<dir>/src whose ensemble/ (no __init__.py) holds just package <dir> (- as _)."""
    found = []
    for name in sorted(os.listdir(repo)):
        top = os.path.join(repo, name, "src", "ensemble")
        subs = [s for s in _entries(top) if os.path.isfile(os.path.join(top, s, "__init__.py"))]
        if subs == [name.replace("-", "_")] and not os.path.isfile(
            os.path.join(top, "__init__.py")
        ):
            found.append(os.path.dirname(top))
    return tuple(found)


def _entries(path: str) -> list[str]:
    try:
        return os.listdir(path)
    except OSError:
        return []


def clean_env(base=None, *import_roots):
    source = os.environ if base is None else base
    env = {
        key: value
        for key, value in source.items()
        if not key.startswith(("GIT_", "PYTHON"))
        and key not in {"TMUX", "TMUX_PANE", "TMUX_TMPDIR", "ENSEMBLE_AGENT_SESSION"}
    }
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    env["TMUX_TMPDIR"] = _tmux_directory()
    env["GIT_CEILING_DIRECTORIES"] = os.pathsep.join(
        dict.fromkeys(
            os.path.dirname(os.path.abspath(source[key]))
            for key in ("HOME", "ENSEMBLE_TEST_WORKSPACE")
            if source.get(key)
        )
    )
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if os.environ.get("ENSEMBLE_CONTROL_ROOT") and "ENSEMBLE_CONTROL_ROOT" not in env:
        env["ENSEMBLE_CONTROL_ROOT"] = os.environ["ENSEMBLE_CONTROL_ROOT"]
    if import_roots:
        env["PYTHONPATH"] = os.pathsep.join(map(str, import_roots))
    return env


def stop_tmux_servers(root, env):
    """Kill the tmux servers on sockets under root; the sockets whose server answered."""
    if Path(root).is_symlink():
        return []
    failures, stopped = [], []
    for path, directories, files in os.walk(root):
        directory = Path(path)
        directories[:] = [name for name in directories if not (directory / name).is_symlink()]
        if directory.name != f"tmux-{os.getuid()}":
            continue
        for name in sorted(files):
            socket = directory / name
            if socket.is_symlink() or not socket.is_socket():
                continue
            try:
                result = subprocess.run(
                    ["tmux", "-S", str(socket), "kill-server"],
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                failures.append(error)
            else:
                if result.returncode == 0:
                    stopped.append(str(socket))
    if failures:
        raise ExceptionGroup(f"{root}: tmux kill-server failed", failures)
    return stopped


def _processes():
    listing = subprocess.run(
        ["ps", "-A", "-ww", "-o", "pid=,ppid=,pgid=,uid=,stat=,args="],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": os.defpath, "LC_ALL": "C"},
    )
    rows = []
    for line in listing.stdout.splitlines():
        fields = line.split(None, 5)
        if len(fields) == 6 and int(fields[3]) == os.getuid() and not fields[4].startswith("Z"):
            rows.append((int(fields[0]), int(fields[1]), int(fields[2]), fields[5]))
    return rows


def leaked_processes(paths, grace=CONTROL_GRACE):
    """(pid, ppid, pgid, args) of this user's processes, other than this one and its ancestors,
    whose command line names one of paths and that outlive grace seconds.
    """
    deadline = time.monotonic() + grace
    while True:
        rows = _processes()
        parents = {pid: ppid for pid, ppid, _, _ in rows}
        own, pid = set(), os.getpid()
        while pid and pid not in own:
            own.add(pid)
            pid = parents.get(pid, 0)
        leaked = [row for row in rows if row[0] not in own and any(p in row[3] for p in paths)]
        if not leaked or time.monotonic() >= deadline:
            return leaked
        time.sleep(0.2)


def kill_processes(leaked):
    """SIGKILL each leaked process (its whole group when it leads one) and the groups its children
    lead, such as a controller's anchored program.
    """
    pids = {pid for pid, _, _, _ in leaked}
    groups = {pgid for pid, _, pgid, _ in leaked if pgid == pid}
    groups |= {pgid for pid, ppid, pgid, _ in _processes() if ppid in pids and pgid == pid}
    for group in sorted(groups):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(group, signal.SIGKILL)
    for pid in sorted(pids - groups):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)


def git_run(*argv, cwd=None, env=None, **kwargs):
    return subprocess.run(
        [
            "git",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "init.templateDir=",
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            *argv,
        ],
        cwd=cwd,
        env=clean_env(env),
        **{"capture_output": True, "text": True, **kwargs},
    )


def git(*argv, cwd=None, env=None):
    return git_run(*argv, cwd=cwd, env=env, check=True).stdout.strip()


def reap(proc, timeout=10):
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=timeout)


def owned_directory(case, root, *, prefix="fixture-"):
    root = os.path.abspath(root)
    if os.path.realpath(root) != root:
        case.fail(f"{root}: test fixture root contains a symlink")
    os.makedirs(root, exist_ok=True)
    path = tempfile.mkdtemp(dir=root, prefix=prefix)
    case.addCleanup(shutil.rmtree, path, ignore_errors=True)
    return path


def python_path(bin_dir, tail=None):
    directory = Path(bin_dir)
    directory.mkdir(parents=True, exist_ok=True)
    shim = directory / "python3"
    shim.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -P -S "$@"\n')
    shim.chmod(0o700)
    return os.pathsep.join((str(directory), os.defpath if tail is None else tail))
