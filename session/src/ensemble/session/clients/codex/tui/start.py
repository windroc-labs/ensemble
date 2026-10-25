"""Codex startup, run as `python -P -s -m` this module with one bounded JSON object argument
(tui.FIELDS).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import signal
import stat
import sys
import time
from collections.abc import Mapping

from ensemble.session.clients.codex import common, tui
from ensemble.session.clients.codex.common import app_server
from ensemble.session.clients.codex.tui import HOOKS

ANSWER_MAX = 64 << 20


class StartError(Exception):
    pass


def _decode_launch_data(argv):
    if len(argv) != 1:
        raise StartError("Codex tui launch: expected bounded launch data")
    try:
        size = len(argv[0].encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise StartError(str(exc)) from exc
    if size > ANSWER_MAX:
        raise StartError("Codex tui launch: expected bounded launch data")
    try:
        data = json.loads(argv[0])
    except (ValueError, RecursionError) as exc:
        raise StartError("Codex tui launch: malformed launch data") from exc
    if not isinstance(data, dict):
        raise StartError("Codex tui launch: invalid launch fields")
    return data


HASH = re.compile(r"sha256:[0-9a-f]{64}")

LOCK_WAIT = 30

ANSWER_WAIT = 20

RETRY = 1

POLL = 0.1


class Stopped(BaseException):
    def __init__(self, signum):
        self.signum = signum


class _Termination(app_server.Termination):
    def __init__(self):
        self.signum = 0
        self.previous = {}

    def __enter__(self):
        for signum in (signal.SIGHUP, signal.SIGTERM):
            self.previous[signum] = signal.signal(signum, self.receive)
        return self

    def __exit__(self, *args):
        for signum, handler in self.previous.items():
            signal.signal(signum, handler)

    def receive(self, signum, _frame):
        # Popen must return an owned process before interruption can unwind.
        self.signum = self.signum or signum

    def check(self):
        if self.signum:
            raise Stopped(self.signum)

    def before_exec(self):
        for signum in self.previous:
            signal.signal(signum, signal.SIG_DFL)
        self.check()


def hook_trust(
    exe: str,
    args: list[str],
    cwd: str,
    env: dict[str, str],
    *,
    commands: Mapping[str, str],
    root=None,
    termination=None,
    listing=None,
) -> str:
    listing = hooks_list if listing is None else listing
    root = common.SessionStore(root).directory(create=False)
    if root is None:
        raise StartError("codex hooks/list: missing session store")
    lock = os.path.join(
        root, hashlib.sha256(env["CODEX_HOME"].encode()).hexdigest() + ".start.lock"
    )
    try:
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as e:
        raise StartError(f"codex hooks/list: {lock}: {e.strerror}") from e
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise StartError("codex hooks/list: startup lock is not a regular file")
        _acquire_lock(fd, lock, termination)
        try:
            result = listing(exe, args, cwd, env, termination=termination)
        except app_server.Exited:
            time.sleep(RETRY)
            if termination is not None:
                termination.check()
            result = listing(exe, args, cwd, env, termination=termination)
    except app_server.Exited as e:
        raise StartError(f"codex hooks/list: app-server exited {e} before answering") from None
    finally:
        os.close(fd)
    return trusted(result, cwd, commands)


def hooks_list(exe: str, args: list[str], cwd: str, env: dict[str, str], *, termination=None):
    try:
        return app_server.AppServer.call(
            exe,
            args,
            "hooks/list",
            {"cwds": [cwd]},
            env=env,
            wait=ANSWER_WAIT,
            limit=ANSWER_MAX,
            cwd=cwd,
            termination=termination,
        )
    except app_server.Error as e:
        raise StartError(f"codex hooks/list: {e}") from None


def trusted(result, cwd: str, commands: Mapping[str, str]) -> str:
    hooks = _listed_hooks(result, cwd)
    ours = [h for h in hooks if h.get("source") == "sessionFlags"]
    trusted_hooks = []
    for event, name, _ in HOOKS:
        command = commands[event]
        rows = [h for h in ours if h.get("eventName") == name]
        if len(rows) != 1:
            count = len(rows)
            raise StartError(
                f"codex hooks/list: {count or 'no'} session-flags {name} hook{('s' if count > 1 else '')}"
            )
        h = rows[0]
        why = (
            "is disabled"
            if h.get("enabled") is not True
            else "is not our command"
            if h.get("handlerType") != "command" or h.get("command") != command
            else "has no key"
            if not isinstance(h.get("key"), str)
            else "has no sha256 hash"
            if not isinstance(h.get("currentHash"), str) or not HASH.fullmatch(h["currentHash"])
            else ""
        )
        if why:
            raise StartError(f"codex hooks/list: the session-flags {name} hook {why}")
        trusted_hooks.append(h)
    for h in ours:
        if not any(h is t for t in trusted_hooks):
            raise StartError(f"codex hooks/list: a session-flags hook not ours: {h.get('key')}")
    for h in hooks:
        if (
            h.get("source") != "sessionFlags"
            and h.get("enabled") is not False
            and h.get("isManaged") is not True
            and h.get("trustStatus") in ("untrusted", "modified")
        ):
            key = h.get("key")
            status = h["trustStatus"]
            raise StartError(
                f"codex hooks/list: your hook {key} is {status}: review it in your own Codex session (/hooks) first"
            )
    return (
        "hooks.state={"
        + ",".join(
            f"{json.dumps(h['key'], ensure_ascii=False)}="
            f"{{trusted_hash={json.dumps(h['currentHash'])}}}"
            for h in trusted_hooks
        )
        + "}"
    )


def main(data):
    try:
        launch(data)
    except Stopped as exc:
        return 128 + exc.signum
    except (StartError, common.ConfigError, common.RecoveryError, OSError, ValueError) as exc:
        print(f"codex tui: {exc}", file=sys.stderr)
        return 1
    return 0


def launch(data, *, trust=hook_trust):
    with _Termination() as termination:
        return _start(tui.decode(data), termination, trust)


def _start(plan: tui.LaunchPlan, termination, trust):
    commands = tui.commands(plan.hook)
    common.absolute_path(plan.store, "store")
    state = common.SessionStore(plan.store).load(plan.workdir, plan.sid)
    if state is None:
        raise StartError("Codex tui launch: missing recovery state")
    env = {
        key: value
        for key, value in {**os.environ, "CODEX_HOME": state.snapshot.codex_home}.items()
        if key not in common.LOCAL_ENV_VARS and key not in ("PYTHONPATH", tui.CLIENT_PYTHONPATH)
    }
    if (pythonpath := os.environ.get(tui.CLIENT_PYTHONPATH)) is not None:
        env["PYTHONPATH"] = pythonpath
    cwd = state.snapshot.cwd
    termination.check()
    trusted_state = trust(
        exe=plan.executable,
        args=tui.trust_args(plan, state.snapshot),
        cwd=cwd,
        env=env,
        root=plan.store,
        termination=termination,
        commands=commands,
    )
    command = tui.execution_argv(plan, state.snapshot, trusted_state)
    os.chdir(cwd)
    termination.before_exec()
    os.execve(command[0], command, env)


def _acquire_lock(fd, lock, termination):
    deadline = time.monotonic() + LOCK_WAIT
    while True:
        if termination is not None:
            termination.check()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() > deadline:
                raise StartError(
                    f"codex hooks/list: another Codex worker's start held {lock} for {LOCK_WAIT} s"
                ) from None
            time.sleep(POLL)


def _listed_hooks(result, cwd):
    data = result.get("data") if isinstance(result, dict) else None
    data = data if isinstance(data, list) else []
    if len(data) != 1:
        raise StartError(f"codex hooks/list: want one answer for {cwd}, got {len(data)}")
    entry = data[0] if isinstance(data[0], dict) else {}
    if entry.get("errors"):
        raise StartError(
            f"codex hooks/list: errors for {cwd}: {json.dumps(entry['errors'], ensure_ascii=False)}"
        )
    hooks = entry.get("hooks")
    if not isinstance(hooks, list) or not all(isinstance(h, dict) for h in hooks):
        raise StartError(f"codex hooks/list: malformed hooks for {cwd}")
    if not hooks:
        raise StartError(
            "codex hooks/list: Codex hooks are off (features.hooks); a Codex worker needs them"
        )
    return hooks


if __name__ == "__main__":
    try:
        launch_data = _decode_launch_data(sys.argv[1:])
    except StartError as exc:
        print(f"codex tui: {exc}", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(launch_data))
