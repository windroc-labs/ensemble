"""Codex shared client behavior, recovery storage and validated snapshots."""

from __future__ import annotations

import fcntl
import fnmatch
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
import uuid
from collections.abc import Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import replace
from datetime import datetime
from functools import cached_property, wraps
from types import MappingProxyType
from typing import Literal, overload
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ensemble.session.clients.codex.common import app_server
from ensemble.session.clients.codex.common.types import (
    NativeMetadata,
    SessionFile,
    SessionState,
    Snapshot,
)
from ensemble.session.clients.common import (
    CHECKS,
    DOCKER_CONFIG_MAX,
    FLAGS,
    MODES,
    check_config,
    docker_config,
    docker_config_path,
    mode_flags,
    scan,
    split_flags,
)
from ensemble.session.engine.types import (
    COMMAND,
    ClientHome,
    ConfigError,
    Launch,
    Permission,
    Resumable,
    SessionInfo,
    SessionStatus,
    Spec,
    Usage,
    Window,
)
from ensemble.utils import (
    DOCKER_STATE,
    LOCAL_ENV_VARS,
    STATE,
    drop_stale,
    environment_ok,
    load_config,
    load_config_text,
    number,
    open_beneath,
    read_beneath,
    resume_command,
)

TABLE = "[clients.codex]"
LIMIT_PREFIXES = (
    "You've hit your usage limit.",
    "You've hit your usage limit for ",
    "Quota exceeded. Check your plan and billing details.",
    "Your workspace is out of credits.",
    "You hit your spend cap set in your workspace.",
    "You hit your spend cap set by the owner of your workspace.",
)


def limit_message(text: str) -> bool:
    """Codex's usage-limit message, a curly apostrophe read as straight."""
    return text.replace("’", "'").startswith(LIMIT_PREFIXES)


MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
RESET = re.compile(
    r"(?:Try|try) again at (?:(?P<month>[A-Z][a-z]{2}) (?P<day>\d{1,2})"
    r"(?P<suffix>st|nd|rd|th), (?P<year>\d{4}) )?"
    r"(?P<hour>[1-9]|1[0-2]):(?P<minute>[0-5]\d) (?P<period>AM|PM)\.$"
)
RESERVED = ("-C", "-p", "--dangerously-bypass-hook-trust")
RESERVED_KEYS = ("hooks",)

VALUED = {
    "-c": "-c",
    "--config": "-c",
    "-C": "-C",
    "--cd": "-C",
    "-p": "-p",
    "--profile": "-p",
    "--enable": "--enable",
    "--disable": "--disable",
    "--add-dir": "--add-dir",
}

MCP_OFF = "mcp_servers={}"
SYSTEM_CONFIG = "/etc/codex/config.toml"
DOCKER_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docker-config.toml")
WORKER_CONFIG = "config/config.toml"  # CODEX_HOME/config.toml links here, in a bind-mounted dir
_SERVER_NAME = re.compile(r"[A-Za-z0-9_-]+")

_SESSION_VERSION = 1

_SESSION_KEYS = frozenset({"version", "workdir", "sid", "snapshot", "native_id", "writable_roots"})

_MAX_SESSION_BYTES = 64 * 1024

STORE = f"{STATE}/sessions/codex"  # under HOME, resolved per operation


class RecoveryError(Exception):
    pass


def added_dirs(argv, cwd: str) -> list[str]:
    return [
        os.path.join(cwd, flag.value)
        for flag in scan(argv, VALUED)
        if flag.word == "--add-dir" and flag.value is not None
    ]


def absolute_path(path: object, label: str) -> str:
    if not isinstance(path, str) or not path or not os.path.isabs(path) or "\x00" in path:
        raise RecoveryError(f"invalid Codex recovery {label}: expected an absolute path")
    return os.path.realpath(path)


def _snapshot(value: object) -> Snapshot:
    if not isinstance(value, dict):
        raise RecoveryError("invalid Codex recovery snapshot fields")
    if set(value) != {
        "cwd",
        "project",
        "dirs",
        "commands",
        "model",
        "effort",
        "codex_home",
        "flags",
        "env",
    }:
        raise RecoveryError("invalid Codex recovery snapshot fields")
    env = value["env"]
    if not environment_ok(env):
        raise RecoveryError("invalid Codex recovery snapshot env")
    flags = value["flags"]
    if not isinstance(flags, list) or any(not isinstance(v, str) or "\0" in v for v in flags):
        raise RecoveryError("invalid Codex recovery snapshot flags")
    if type(value["project"]) is not bool:
        raise RecoveryError("invalid Codex recovery snapshot project")
    for key in ("dirs", "commands"):
        if not isinstance(value[key], list):
            raise RecoveryError(f"invalid Codex recovery snapshot {key}")
    commands = value["commands"]
    if any(not isinstance(item, str) or not item or not item.isprintable() for item in commands):
        raise RecoveryError("invalid Codex recovery snapshot commands")
    for key in ("model", "effort"):
        if value[key] is not None and (
            not isinstance(value[key], str) or not value[key] or not value[key].isprintable()
        ):
            raise RecoveryError(f"invalid Codex recovery snapshot {key}")
    return Snapshot(
        value["cwd"],
        value["project"],
        tuple(value["dirs"]),
        tuple(commands),
        value["model"],
        value["effort"],
        value["codex_home"],
        tuple(flags),
        env,
    )


def validate_snapshot(value: object) -> Snapshot:
    snapshot = _snapshot(value)
    cwd = absolute_path(snapshot.cwd, "cwd")
    home = absolute_path(snapshot.codex_home, "codex_home")
    dirs = tuple(absolute_path(item, "dir") for item in snapshot.dirs)
    if cwd != snapshot.cwd or home != snapshot.codex_home or dirs != snapshot.dirs:
        raise RecoveryError("invalid Codex recovery snapshot path")
    return snapshot


def snapshot_data(snapshot: Snapshot) -> dict:
    return {
        "cwd": snapshot.cwd,
        "codex_home": snapshot.codex_home,
        "project": snapshot.project,
        "dirs": list(snapshot.dirs),
        "commands": list(snapshot.commands),
        "model": snapshot.model,
        "effort": snapshot.effort,
        "flags": list(snapshot.flags),
        "env": dict(snapshot.env),
    }


def inside(path: str, root: str) -> bool:
    return os.path.commonpath((path, root)) == root


def read_metadata(path: str) -> SessionFile:
    try:
        fd = open_beneath("/", path)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise RecoveryError(f"cannot read Codex session file {path}: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise RecoveryError(f"not a regular Codex session file: {path}")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = -1
            try:
                metadata = json.loads(stream.readline(128 * 1024))
            except (ValueError, UnicodeError, RecursionError) as exc:
                raise RecoveryError(f"malformed Codex session metadata: {path}") from exc
        if (
            not isinstance(metadata, dict)
            or metadata.get("type") != "session_meta"
            or not isinstance(metadata.get("payload"), dict)
        ):
            raise RecoveryError(f"malformed Codex session metadata: {path}")
        payload = metadata["payload"]
        if (
            not native_id(payload.get("id"))
            or not path.endswith("-" + payload["id"] + ".jsonl")
            or absolute_path(payload.get("cwd"), "session cwd") != payload["cwd"]
        ):
            raise RecoveryError(f"mismatched Codex session metadata: {path}")
        return SessionFile(
            path,
            info.st_mtime,
            NativeMetadata(payload["id"], payload["cwd"], _parent_identifier(payload)),
        )
    finally:
        if fd >= 0:
            os.close(fd)


def _parent_identifier(payload) -> str | None:
    parent, source = payload.get("parent_thread_id"), payload.get("source")
    if parent is None and isinstance(source, dict):
        agent = source.get("subagent")
        spawn = agent.get("thread_spawn") if isinstance(agent, dict) else None
        parent = spawn.get("parent_thread_id") if isinstance(spawn, dict) else None
    return parent if native_id(parent) else None


def session_paths(snapshot: Snapshot, identifier: str = "*") -> list[str]:
    root = os.path.join(snapshot.codex_home, "sessions")
    if not os.path.exists(root):
        return []
    directories = [root]
    for length in (4, 2, 2):
        children = []
        for directory in directories:
            fd = open_beneath("/", directory, directory=True)
            try:
                entries = os.scandir(fd)
                with entries:
                    for entry in entries:
                        if (
                            len(entry.name) == length
                            and entry.name.isascii()
                            and entry.name.isdigit()
                            and entry.is_dir(follow_symlinks=False)
                        ):
                            children.append(os.path.join(directory, entry.name))
            finally:
                os.close(fd)
        directories = children
    matches = []
    for directory in directories:
        fd = open_beneath("/", directory, directory=True)
        try:
            with os.scandir(fd) as entries:
                matches.extend(
                    os.path.join(directory, entry.name)
                    for entry in entries
                    if fnmatch.fnmatchcase(entry.name, f"rollout-*-{identifier}.jsonl")
                )
        finally:
            os.close(fd)
    return matches


def session_file(snapshot: Snapshot, identifier: str, *, recorded: str = "") -> SessionFile | None:
    if not native_id(identifier):
        raise RecoveryError("invalid Codex recovery native ID")
    if recorded:
        absolute_path(recorded, "recorded transcript")
    matches = [recorded] if recorded else session_paths(snapshot, identifier)
    if len(matches) > 1:
        raise RecoveryError(f"duplicate Codex session files for native ID {identifier}")
    if not matches:
        return None
    try:
        found = read_metadata(matches[0])
    except FileNotFoundError:
        return None
    if found.metadata.id != identifier or found.metadata.cwd != snapshot.cwd:
        raise RecoveryError(
            f"mismatched Codex session metadata for native ID {identifier}: {found.path}"
        )
    return found


def native_id(value) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def session_identity(workdir: object, sid: object) -> tuple[str, str]:
    workdir = absolute_path(workdir, "workdir")
    if not isinstance(sid, str) or not sid or len(sid) > 1024 or "\x00" in sid:
        raise RecoveryError("invalid recovery run ID")
    return workdir, sid


def _state(data: object, workdir: str, sid: str) -> SessionState:
    if (
        not isinstance(data, dict)
        or frozenset(data) != _SESSION_KEYS
        or type(data["version"]) is not int
        or data["version"] != _SESSION_VERSION
    ):
        raise RecoveryError("malformed recovery state")
    if data["workdir"] != workdir or data["sid"] != sid:
        raise RecoveryError("invalid recovery state identity")
    native = data["native_id"]
    if native is not None and not native_id(native):
        raise RecoveryError("invalid recovery native ID")
    roots = data["writable_roots"]
    if not isinstance(roots, list) or any(
        absolute_path(path, "writable root") != path for path in roots
    ):
        raise RecoveryError("invalid recovery writable roots")
    return SessionState(workdir, sid, validate_snapshot(data["snapshot"]), tuple(roots), native)


def _private_dir(path: str, *, create: bool) -> bool:
    if not os.path.exists(path):
        if not create:
            return False
        with suppress(FileExistsError):
            os.mkdir(path, 0o700)
        fd = os.open(os.path.dirname(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    if not os.path.isdir(path):
        raise RecoveryError(f"recovery state path is not a directory: {path}")
    return True


def implicit_writable_roots(environment: Mapping[str, str] | None = None) -> list[str]:
    environment = os.environ if environment is None else environment
    return [
        "/tmp",
        "/private/tmp",
        "/var/tmp",
        tempfile.gettempdir(),
        *([os.path.abspath(environment["TMPDIR"])] if environment.get("TMPDIR") else []),
    ]


def check_writable_roots(root: str, paths: Sequence[str]) -> None:
    actual = os.path.realpath(root)
    for path in paths:
        writable = absolute_path(path, "writable root")
        if os.path.commonpath((actual, writable)) == writable:
            raise RecoveryError(f"recovery state directory {root} is inside writable root {path}")


def _state_data(fd: int, path: str):
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise RecoveryError(f"not a regular recovery state file: {path}")
        if info.st_size > _MAX_SESSION_BYTES:
            raise RecoveryError(f"malformed recovery state: file too large: {path}")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = -1
            text = stream.read(_MAX_SESSION_BYTES + 1)
            if len(text.encode("utf-8")) > _MAX_SESSION_BYTES:
                raise RecoveryError(f"malformed recovery state: file too large: {path}")
            data = json.loads(text)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise RecoveryError(f"malformed recovery state: {path}") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    return data


class _StoreOperation:
    def __init__(self, root: str):
        self.root = absolute_path(root, "store")

    def directory(self, *, create: bool) -> str | None:
        root = self.root
        parent = os.path.dirname(root)
        base = os.path.dirname(parent)
        check_writable_roots(root, implicit_writable_roots())
        if not _private_dir(base, create=create):
            return None
        if not _private_dir(parent, create=create):
            return None
        return root if _private_dir(root, create=create) else None

    def path(self, workdir: str, sid: str) -> str:
        digest = hashlib.sha256((workdir + "\x00" + sid).encode("utf-8")).hexdigest()
        return os.path.join(self.root, digest + ".json")

    def _validate_recovery(self, state: SessionState) -> None:
        snapshot = state.snapshot
        paths = [
            snapshot.cwd,
            snapshot.codex_home,
            *snapshot.dirs,
            *(absolute_path(path, "dir") for path in added_dirs(snapshot.flags, snapshot.cwd)),
        ]
        check_writable_roots(self.root, paths)
        for path in paths:
            if not any(inside(path, grant) for grant in state.writable_roots):
                raise RecoveryError(
                    f"invalid Codex recovery writable grants: no recorded root covers {path}"
                )

    def _read(self, path: str) -> dict | None:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise RecoveryError(f"cannot read recovery state {path}: {exc}") from exc
        data = _state_data(fd, path)
        if not isinstance(data, dict):
            raise RecoveryError("malformed recovery state")
        return data

    def _validated_state(self, path: str, data: dict, workdir: str, sid: str) -> SessionState:
        state = _state(data, workdir, sid)
        if path != self.path(workdir, sid):
            raise RecoveryError("invalid recovery state identity")
        self._validate_recovery(state)
        check_writable_roots(self.root, state.writable_roots)
        return state

    def load(self, workdir: str, sid: str) -> SessionState | None:
        path = self.path(workdir, sid)
        data = self._read(path)
        if data is None:
            return None
        state = self._validated_state(path, data, workdir, sid)
        try:
            os.utime(path, follow_symlinks=False)
        except OSError as error:
            print(f"codex recovery: cannot renew {path}: {error}", file=sys.stderr)
        return state

    def read(self, path: str) -> SessionState | None:
        data = self._read(path)
        if data is None:
            return None
        workdir, sid = session_identity(data.get("workdir"), data.get("sid"))
        return self._validated_state(path, data, workdir, sid)

    @contextmanager
    def locked(self, timeout: float | None = None):
        fd = os.open(self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            deadline = None if timeout is None else time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | (fcntl.LOCK_NB if deadline is not None else 0))
                    break
                except BlockingIOError:
                    if deadline is not None and time.monotonic() >= deadline:
                        raise RecoveryError("Codex recovery state lock timed out") from None
                    time.sleep(0.05)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @staticmethod
    def encoded(state: SessionState) -> bytes:
        payload = json.dumps(
            {
                "version": _SESSION_VERSION,
                "workdir": state.workdir,
                "sid": state.sid,
                "snapshot": snapshot_data(state.snapshot),
                "native_id": state.native_id,
                "writable_roots": list(state.writable_roots),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        if len(payload) > _MAX_SESSION_BYTES:
            raise RecoveryError("recovery state is too large")
        return payload

    def write(self, state: SessionState, *, exclusive: bool) -> None:
        payload = self.encoded(state)
        path = self.path(state.workdir, state.sid)
        fd, tmp = tempfile.mkstemp(prefix=".state-", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as stream:
                fd = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if exclusive:
                try:
                    os.link(tmp, path, follow_symlinks=False)
                except FileExistsError as exc:
                    raise RecoveryError(
                        f"recovery state already exists for run ID {state.sid!r}"
                    ) from exc
            else:
                os.replace(tmp, path)
            directory = os.open(self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if fd >= 0:
                os.close(fd)
            if os.path.exists(tmp):
                os.unlink(tmp)

    def validate_for_save(self, state: SessionState) -> None:
        self._validate_recovery(state)
        check_writable_roots(self.root, [*state.writable_roots, *implicit_writable_roots()])
        self.encoded(state)


class SessionStore:
    """Recovery mappings and locking; default roots follow HOME, explicit roots pin the store."""

    def __init__(self, root: str | None = None):
        self._root = root

    @property
    def root(self) -> str:
        return (
            self._root
            if self._root is not None
            else os.path.join(os.path.realpath(os.path.expanduser("~")), STORE.removeprefix("~/"))
        )

    @overload
    def directory(self, *, create: Literal[True]) -> str: ...

    @overload
    def directory(self, *, create: bool) -> str | None: ...

    def directory(self, *, create: bool) -> str | None:
        return _StoreOperation(self.root).directory(create=create)

    def create(
        self,
        workdir: str,
        sid: str,
        snapshot: Snapshot,
        writable_roots: Sequence[str],
    ) -> SessionState:
        workdir, sid = session_identity(workdir, sid)
        state = SessionState(
            workdir,
            sid,
            snapshot,
            tuple(dict.fromkeys(absolute_path(path, "writable root") for path in writable_roots)),
        )
        _StoreOperation(self.root).validate_for_save(state)
        return state

    def save(self, state: SessionState) -> SessionState:
        operation = _StoreOperation(self.root)
        operation.validate_for_save(state)
        operation.directory(create=True)
        operation.write(state, exclusive=True)
        for error in drop_stale(operation.root):
            print(f"codex recovery: {error}", file=sys.stderr)
        return state

    @staticmethod
    def project(state: SessionState, root: str) -> dict[str, str]:
        operation = _StoreOperation(root)
        return {operation.path(state.workdir, state.sid): operation.encoded(state).decode()}

    def file(self, workdir: str, sid: object) -> str:
        """Its recovery file's path for the run; nothing is read or created."""
        return _StoreOperation(self.root).path(*session_identity(workdir, sid))

    def load(self, workdir: str, sid: object) -> SessionState | None:
        operation = _StoreOperation(self.root)
        workdir, sid = session_identity(workdir, sid)
        if operation.directory(create=False) is None:
            return None
        return operation.load(workdir, sid)

    def bind(
        self,
        workdir: str,
        sid: str,
        identifier: str,
        timeout: float | None = None,
    ) -> SessionState:
        operation = _StoreOperation(self.root)
        workdir, sid = session_identity(workdir, sid)
        if not native_id(identifier):
            raise RecoveryError("invalid Codex recovery native ID")
        if operation.directory(create=False) is None:
            raise RecoveryError(f"missing recovery state for run ID {sid!r}")
        with operation.locked(timeout):
            existing = operation.load(workdir, sid)
            if existing is None:
                raise RecoveryError(f"missing recovery state for run ID {sid!r}")
            validated = replace(existing, native_id=identifier)
            if existing.native_id is not None:
                if existing.native_id != validated.native_id:
                    raise RecoveryError(
                        f"recovery run ID {sid!r} is already bound to a different native ID"
                    )
                return existing
            operation.write(validated, exclusive=False)
            return validated

    def lookup(self, sid: str, cwd: str) -> SessionState | None:
        """An unreadable state file is skipped with a stderr line; with no match it is raised, as
        it may be this run's.
        """
        operation = _StoreOperation(self.root)
        cwd = absolute_path(cwd, "cwd")
        directory = operation.directory(create=False)
        if directory is None:
            return None
        matches, skipped = [], []
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name.endswith(".json"):
                    try:
                        state = operation.read(entry.path)
                    except RecoveryError as exc:
                        named = str(exc) if entry.path in str(exc) else f"{entry.path}: {exc}"
                        print(f"codex recovery: {named}", file=sys.stderr)
                        skipped.append(exc)
                        continue
                    if state is not None and state.sid == sid and state.snapshot.cwd == cwd:
                        matches.append(state)
        if len(matches) > 1 and (
            matches[0].native_id is None or len({state.native_id for state in matches}) > 1
        ):
            raise RecoveryError(f"ambiguous Codex binding for run ID {sid!r} in {cwd}")
        if not matches and skipped:
            raise skipped[0]
        return matches[0] if matches else None


def reset_at(message, environment):
    match = RESET.search(message)
    if match is None:
        return None
    try:
        zone_name = environment.get("TZ", os.environ.get("TZ"))
        zone = ZoneInfo(zone_name.removeprefix(":")) if zone_name else None
        value = match.groupdict()
        today = datetime.now(zone)
        year, month, day = today.year, today.month, today.day
        if value["month"]:
            year, month, day = (
                int(value["year"]),
                MONTHS.index(value["month"]) + 1,
                int(value["day"]),
            )
            suffix = "th" if 11 <= day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
            if value["suffix"] != suffix:
                return None
        hour = int(value["hour"]) % 12 + (12 if value["period"] == "PM" else 0)
        local = datetime(year, month, day, hour, int(value["minute"]))
        stamps = set()
        for fold in (0, 1):
            stamp = local.replace(tzinfo=zone, fold=fold).timestamp()
            if datetime.fromtimestamp(stamp, zone).replace(tzinfo=None, fold=0) == local:
                stamps.add(stamp)
        return stamps.pop() if len(stamps) == 1 else None
    except (ValueError, OverflowError, OSError, ZoneInfoNotFoundError):
        return None


def refuse(flags) -> None:
    for flag in scan(flags, VALUED):
        key = (
            flag.value.partition("=")[0].split(".", 1)[0].strip().strip("\"'")
            if flag.word == "-c" and flag.value is not None
            else ""
        )
        if flag.word in RESERVED or key in RESERVED_KEYS:
            raise ConfigError(f"{flag.text}: the Codex client sets hooks, session and cwd")


def refuse_mode(flags, owner: str = "a permission mode's flags") -> None:
    """Flags read at each launch, a permission mode's or the config's, grant no directory, hooks,
    session or cwd, and pass no literal argument: a resume's grants stay those recorded at its start.
    """
    refuse(flags)
    if "--" in flags:
        raise ConfigError(f"--: {owner} pass no literal argument")
    if any(flag.word == "--add-dir" for flag in scan(flags, VALUED)):
        raise ConfigError(f"--add-dir: {owner} grant no directory")


def _native_id(state: SessionState) -> str:
    if state.native_id is None:
        raise ConfigError(f"missing Codex native ID for run ID {state.sid!r}")
    return state.native_id


def _recover(method):
    @wraps(method)
    def translated(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except RecoveryError as exc:
            raise ConfigError(str(exc)) from exc

    return translated


def _guard_untrusted_project(
    workdir: str, cwd: str, config_home: str | None = None, dirs: Sequence[str] = ()
) -> None:
    real_workdir, real_cwd = os.path.realpath(workdir), os.path.realpath(cwd)
    default_home = os.path.abspath(os.path.expanduser("~/.codex"))
    real_default_home = os.path.realpath(default_home)
    config_home = _config_home(cwd, config_home)
    real_config_home = os.path.realpath(config_home)
    if any(
        inside(home, root) or inside(root, home)
        for home, root in ((config_home, workdir), (real_config_home, real_workdir))
    ):
        raise ConfigError(
            f"CODEX_HOME {config_home} overlaps the untrusted workdir {workdir}; move Codex config outside the workdir"
        )
    for root in (cwd, *dirs):
        if inside(config_home, root) or inside(real_config_home, os.path.realpath(root)):
            raise ConfigError(
                f"CODEX_HOME {config_home} is inside writable root {root}; move Codex config outside the writable roots"
            )

    for start in (cwd, real_cwd):
        ancestor = start
        while True:
            candidate = os.path.join(ancestor, ".codex")
            if os.path.lexists(candidate) and (
                os.path.isdir(candidate) or os.path.islink(candidate)
            ):
                real_candidate = os.path.realpath(candidate)
                genuine_user_home = (
                    candidate in (default_home, real_default_home)
                    and real_candidate == real_default_home
                    and not inside(candidate, workdir)
                    and not inside(real_candidate, real_workdir)
                )
                if not genuine_user_home:
                    raise ConfigError(
                        f"project Codex settings at {candidate} may load in an untrusted run; use a clean workdir or trusted_dirs"
                    )
            parent = os.path.dirname(ancestor)
            if parent == ancestor:
                break
            ancestor = parent


def _config_home(cwd: str, config_home: str | None = None) -> str:
    return os.path.abspath(os.path.join(cwd, os.path.expanduser(config_home or "~/.codex")))


def _docker_files() -> dict[str, str]:
    """The worker's own config copy: the Docker config verbatim, once it parses."""
    path, text = docker_config("codex", "config.toml", DOCKER_DEFAULT)
    try:
        load_config_text(text)
    except (ValueError, RecursionError) as error:
        raise ConfigError(f"Docker config {path}: invalid TOML: {error}") from None
    return {WORKER_CONFIG: text}


def _docker_home(home: str) -> bool:
    """Whether real path `home` is a Docker worker's Codex home, seen from the host."""
    state = os.path.realpath(os.path.expanduser(DOCKER_STATE))
    return os.path.dirname(os.path.dirname(home)) == state and os.path.basename(home) == "codex"


def _config_layers(snapshot: Snapshot, *, resume: bool = False) -> list[str]:
    """The config files Codex may read for `snapshot`, lowest precedence first; a trusted project
    adds each `.codex/config.toml` from / down to the cwd. A Docker home's one user layer is the
    file Codex will load there: the Docker config a new start copies, else the worker's own copy;
    the host's system config never reaches it.
    """
    home = os.path.realpath(snapshot.codex_home)
    if not _docker_home(home):
        layers = [SYSTEM_CONFIG, os.path.join(home, "config.toml")]
    elif resume:
        layers = [os.path.join(home, WORKER_CONFIG)]
    else:
        layers = [docker_config_path("codex", "config.toml", DOCKER_DEFAULT)]
    if snapshot.project:
        ancestors = [snapshot.cwd]
        while (parent := os.path.dirname(ancestors[-1])) != ancestors[-1]:
            ancestors.append(parent)
        layers += [os.path.join(d, ".codex", "config.toml") for d in reversed(ancestors)]
    return layers


def _read_layer(path: str, home: str) -> dict:
    """`path`'s TOML; the worker's own copy, which its agent can write, is read with no link
    followed and at most DOCKER_CONFIG_MAX bytes. FileNotFoundError: no such layer.
    """
    if path != os.path.join(home, WORKER_CONFIG):
        return load_config(path)
    data = read_beneath(home, path, limit=DOCKER_CONFIG_MAX + 1)
    if data is None or len(data) > DOCKER_CONFIG_MAX:
        if not os.path.lexists(path):
            raise FileNotFoundError(path)
        raise ConfigError(f"{path}: want a regular file of at most 2 MiB")
    return load_config_text(data.decode())


def _mcp_servers(snapshot: Snapshot, *, resume: bool = False) -> dict[str, tuple[str, str]]:
    """Each MCP server the layers name: its transport key and value, the later layer's winning."""
    found = {}
    home = os.path.realpath(snapshot.codex_home)
    for path in _config_layers(snapshot, resume=resume):
        if not os.path.lexists(path):
            continue
        try:
            servers = _read_layer(path, home).get("mcp_servers", {})
        except FileNotFoundError:
            continue
        except OSError as error:
            raise ConfigError(f"{path}: {error}") from None
        except ValueError as error:
            raise ConfigError(f"{path}: invalid TOML: {error}") from None
        if not isinstance(servers, dict):
            raise ConfigError(f"{path}: mcp_servers is not a table")
        for name, server in servers.items():
            if not isinstance(server, dict):
                raise ConfigError(f"{path}: mcp_servers.{name} is not a table")
            for key in ("command", "url"):
                if key not in server:
                    continue
                if not isinstance(server[key], str):
                    raise ConfigError(f"{path}: mcp_servers.{name}.{key} is not a string")
                if not _SERVER_NAME.fullmatch(name):
                    raise ConfigError(
                        f"{path}: MCP server {name!r}: a -c flag cannot turn it off; rename it"
                    )
                found[name] = (key, server[key])
    return found


def mcp_off(flags: Sequence[str], snapshot: Snapshot, *, resume: bool = False) -> list[str]:
    """`flags` with each `-c mcp_servers={}` (MCP_OFF) replaced by flags turning off every server
    the config layers name: `-c` merges tables, so MCP_OFF alone removes none. Each also gets its
    transport, so a name no layer Codex loads defines (a Docker launch) is still valid.
    """
    result, servers, end = [], None, 0
    for flag in scan(flags, VALUED):
        end = flag.end
        if flag.word != "-c" or flag.value != MCP_OFF:
            result.extend(flags[flag.start : flag.end])
            continue
        if servers is None:
            servers = _mcp_servers(snapshot, resume=resume)
        for name, (key, value) in sorted(servers.items()):
            result += ["-c", f"mcp_servers.{name}.enabled=false"]
            result += ["-c", f"mcp_servers.{name}.{key}={json.dumps(value, ensure_ascii=False)}"]
    return [*result, *flags[end:]]


def launch_options(snapshot: Snapshot) -> list[str]:
    result = [
        *(("-m", snapshot.model) if snapshot.model is not None else ()),
        *(
            ("-c", f"model_reasoning_effort={snapshot.effort}")
            if snapshot.effort is not None
            else ()
        ),
    ]
    if not snapshot.project:
        result.extend(("-c", "project_doc_max_bytes=0"))
    for directory in snapshot.dirs:
        result.extend(("--add-dir", directory))
    return result


def unattended_flags(cwd: str) -> tuple[str, ...]:
    """What a Docker worker needs whatever its config.toml says: `-c` outranks that file, and
    Codex applies the last `-c` for a key, so these go after every other flag.
    """
    trusted = json.dumps(os.path.realpath(cwd), ensure_ascii=False).replace("\x7f", "\\u007f")
    return (
        "-c",
        'cli_auth_credentials_store="file"',
        "-c",
        'forced_login_method="chatgpt"',
        "-c",
        "check_for_update_on_startup=false",
        "-c",
        f'projects={{{trusted}={{trust_level="trusted"}}}}',
    )


def launch_flags(
    snapshot: Snapshot,
    *,
    interactive: bool,
    mode: Sequence[str],
    configured: Sequence[str],
    unattended: bool = False,
    resume: bool = False,
) -> list[str]:
    """`mode` and `configured`: the launch's permission mode flags and the config's current
    flags, never in the snapshot; `unattended`: a Docker launch, given unattended_flags last.
    """
    supplied, _ = split_flags(snapshot.flags)
    result = [
        *mode,
        *mcp_off([*configured, *supplied], snapshot, resume=resume),
        *launch_options(snapshot),
        *(unattended_flags(snapshot.cwd) if unattended else ()),
    ]
    if not interactive:
        result[:0] = ["--json", "--skip-git-repo-check"]
    return result


def _window(window: dict) -> Window | None:
    percent, reset = number(window.get("usedPercent")), number(window.get("resetsAt"))
    if percent is None or percent < 0:
        return None
    return Window(percent, reset if reset is not None and reset >= 0 else None)


def _usage(snapshot) -> Usage | None:
    if not isinstance(snapshot, dict):
        return None
    windows = {}
    for key in ("primary", "secondary"):
        window = snapshot.get(key)
        if not isinstance(window, dict):
            continue
        duration = window.get("windowDurationMins")
        if type(duration) is int and duration in (300, 10080) and duration not in windows:
            windows[duration] = window
    return Usage(_window(windows.get(300, {})), _window(windows.get(10080, {})))


class CodexCommon:
    UNSET = (
        *sorted(LOCAL_ENV_VARS),
        "CODEX_THREAD_ID",
        "CODEX_SESSION_ID",
        "CODEX_VERSION",
        "CODEX_SANDBOX",
        "CODEX_SANDBOX_NETWORK_DISABLED",
        "CODEX_PERMISSION_PROFILE",
        "CLAUDECODE",
    )
    DEFAULTS = MappingProxyType(
        {
            "NO_COLOR": "1",
            "CODEX_CI": "1",
            "PAGER": "cat",
            "GIT_PAGER": "cat",
            "GH_PAGER": "cat",
        }
    )

    def __init__(self, config, *, selector):
        self.selector = selector
        self.config = dict(config)
        check_config(config, TABLE, (FLAGS, MODES, *CHECKS))
        self.flags = tuple(config.get("flags", ()))
        self.modes = {p: mode_flags(config, p) for p in Permission}
        refuse_mode(self.flags, f"{TABLE} flags")
        for flags in self.modes.values():
            refuse_mode(flags)

    def home(self) -> ClientHome:
        return ClientHome(
            "codex_dir",
            "codex",
            "CODEX_HOME",
            "codex-access-token",
            files=_docker_files,
            persistent=("sessions", "config"),
        )

    @cached_property
    def store(self) -> SessionStore:
        return SessionStore()

    def _mapped(self, name, value):
        if value is None:
            return None
        result = self.config.get(name, {}).get(str(value))
        if result is None:
            raise ConfigError(f"no mapping for {value!r} in {TABLE}.{name}")
        return result

    def _explicit_environment(self, spec: Spec) -> dict[str, str]:
        return {**self.config.get("env", {}), **spec.access.env, **spec.env}

    def _environment(
        self, supplied: Mapping[str, str], *, interactive: bool = False, ambient: bool = False
    ) -> dict[str, str]:
        inherited = (
            {key: value for key, value in os.environ.items() if key not in self.UNSET}
            if ambient
            else {}
        )
        result = {
            key: value
            for key, value in {
                **inherited,
                **({} if interactive else self.DEFAULTS),
                **supplied,
            }.items()
            if key not in LOCAL_ENV_VARS
        }
        if ambient and not result.get("CODEX_HOME"):
            result["CODEX_HOME"] = inherited.get("CODEX_HOME") or "~/.codex"
        return result

    def _resumed(self, spec: Spec) -> SessionState:
        """spec's recovery state. One saved under another workdir (a closed issue run resumed as a
        worker) is copied under spec's, which commit saves.
        """
        workdir, sid = session_identity(spec.workdir, spec.sid)
        state = self.load(workdir, sid)
        if state is None and (found := self.lookup(sid, spec.access.cwd)) is not None:
            state = replace(found, workdir=workdir)
        if state is None:
            raise ConfigError(f"missing Codex recovery state for run ID {spec.sid!r}")
        return state

    @_recover
    def prepare(self, spec: Spec) -> SessionState:
        if spec.resume:
            state = self._resumed(spec)
            if state.native_id is None:
                raise ConfigError(f"missing Codex native ID for run ID {spec.sid!r}")
            snapshot = state.snapshot
            refuse(snapshot.flags)
            environment = self.environment(snapshot, ambient=True)
            check_writable_roots(self.store.root, implicit_writable_roots(environment))
            if session_file(snapshot, state.native_id) is None:
                raise ConfigError(f"missing Codex session file for native ID {state.native_id}")
            if not snapshot.project:
                _guard_untrusted_project(
                    state.workdir,
                    snapshot.cwd,
                    snapshot.codex_home,
                    (*snapshot.dirs, *added_dirs(snapshot.flags, snapshot.cwd)),
                )
            return state
        refuse(spec.flags)
        explicit = self._explicit_environment(spec)
        environment = self._environment(explicit, ambient=True)
        check_writable_roots(self.store.root, implicit_writable_roots(environment))
        roots = self._new_roots(spec, environment)
        workdir, sid = session_identity(spec.workdir, spec.sid)
        cwd, home, *_ = roots
        if not spec.access.project:
            _guard_untrusted_project(workdir, cwd, home, roots[2:])
        snapshot = _snapshot(
            {
                "cwd": cwd,
                "project": spec.access.project,
                "dirs": roots[2 : 2 + len(spec.access.dirs)],
                "commands": list(spec.access.commands),
                "model": self._mapped("tiers", spec.tier),
                "effort": self._mapped("efforts", spec.effort and spec.effort.value),
                "codex_home": home,
                "flags": list(spec.flags),
                "env": explicit,
            }
        )
        return self.store.create(workdir, sid, snapshot, roots)

    def _new_roots(self, spec: Spec, environment: Mapping[str, str]) -> list[str]:
        cwd = absolute_path(spec.access.cwd, "cwd")
        home = absolute_path(
            _config_home(cwd, environment.get("CODEX_HOME")),
            "codex_home",
        )
        added = [absolute_path(path, "dir") for path in added_dirs(spec.flags, cwd)]
        return [
            cwd,
            home,
            *(os.path.realpath(path) for path in spec.access.dirs),
            *added,
        ]

    @_recover
    def writable_dirs(self, spec: Spec) -> tuple[str, ...]:
        if spec.resume:
            state = self._resumed(spec)
            roots = state.writable_roots
            environment = self.environment(state.snapshot, ambient=True)
        else:
            environment = self._environment(self._explicit_environment(spec), ambient=True)
            roots = self._new_roots(spec, environment)
        implicit = map(os.path.realpath, implicit_writable_roots(environment))
        return tuple(dict.fromkeys([*roots, *implicit]))

    def commit(self, state: SessionState | None) -> SessionState:
        if state is None:
            raise ConfigError("Codex launch has not been prepared")
        stored = state.native_id is not None and self.load(state.workdir, state.sid) is not None
        return state if stored else self.save(state)

    @_recover
    def save(self, state: SessionState) -> SessionState:
        return self.store.save(state)

    @_recover
    def load(self, workdir: str, sid: object) -> SessionState | None:
        return self.store.load(workdir, sid)

    @_recover
    def bind(self, workdir: str, sid: str, identifier: str) -> SessionState:
        return self.store.bind(workdir, sid, identifier)

    def environment(
        self, snapshot: Snapshot, *, interactive: bool = False, ambient: bool = False
    ) -> dict[str, str]:
        return self._environment(
            {**snapshot.env, "CODEX_HOME": snapshot.codex_home},
            interactive=interactive,
            ambient=ambient,
        )

    def probe(self) -> Launch:
        script = app_server.__file__
        return Launch(
            (os.path.realpath(sys.executable), "-I", script),
            env=self._environment(self.config.get("env", {})),
            unset=self.UNSET,
        )

    @staticmethod
    def parse_usage(output: str) -> Usage | None:
        """Map account limits to five-hour and weekly usage; malformed data is unknown."""
        try:
            result = json.loads(output)
        except (ValueError, TypeError, RecursionError):
            return None
        if not isinstance(result, dict):
            return None
        limits = result.get("rateLimitsByLimitId")
        snapshot = limits.get("codex") if isinstance(limits, dict) else None
        return _usage(snapshot if isinstance(snapshot, dict) else result.get("rateLimits"))

    def resume_launch(self, state: SessionState, *, mode: Sequence[str] = ()) -> Launch:
        snapshot = state.snapshot
        selection = self.selector(_native_id(state), resume=True)
        _, literal = split_flags(snapshot.flags)
        argv = (
            "codex",
            *selection[:-1],
            *launch_flags(snapshot, interactive=True, mode=mode, configured=self.flags),
            *selection[-1:],
            *literal,
        )
        return Launch(argv, env=self.environment(snapshot, interactive=True), cwd=snapshot.cwd)

    def resume_command(self, state: SessionState) -> str:
        """A reference to the stored launch, which resume_main execs; it never holds env values."""
        argv = (COMMAND, "client-resume", "codex", self.store.root, state.workdir, state.sid)
        return resume_command(state.snapshot.cwd, {}, (*argv, _native_id(state)))

    def resume_main(self, argv) -> int:
        """resume_command's run (STORE WORKDIR SID NATIVE): execs the stored resume in auto mode
        with the environment its recovery state keeps; 1 and one stderr line when it cannot.
        """
        try:
            store, workdir, sid, native = argv
        except ValueError:
            print("codex resume: expected STORE WORKDIR SID NATIVE", file=sys.stderr)
            return 1
        try:
            state = SessionStore(store).load(workdir, sid)
            if state is None:
                raise ConfigError(f"missing Codex recovery state for run ID {sid!r} in {store}")
            if state.native_id != native:
                raise ConfigError(
                    f"recovery state for run ID {sid!r} is bound to native ID "
                    f"{state.native_id!r}, not {native!r}"
                )
            launch = self.resume_launch(state, mode=self.modes[Permission.AUTO])
            os.chdir(launch.cwd)
            os.execvpe(launch.argv[0], launch.argv, {**os.environ, **launch.env})
        except (ConfigError, RecoveryError, OSError) as exc:
            print(f"codex resume: {exc}", file=sys.stderr)
            return 1
        return 0

    @_recover
    def lookup(self, sid: str, cwd: str) -> SessionState | None:
        return self.store.lookup(sid, cwd)

    @_recover
    def last_active(self, state: SessionState, found: SessionFile) -> float:
        candidates = []
        for path in session_paths(state.snapshot):
            try:
                metadata = read_metadata(path)
            except (ConfigError, RecoveryError, OSError):
                continue
            payload = metadata.metadata
            if payload.parent_thread_id is not None:
                candidates.append((payload.id, payload.parent_thread_id, metadata.mtime))
        related, newest = {state.native_id}, found.mtime
        while True:
            children = [
                (identifier, stamp)
                for identifier, parent, stamp in candidates
                if parent in related and identifier not in related
            ]
            added = {identifier for identifier, stamp in children} - related
            if not added:
                return newest
            if len(children) != len(added):
                raise ConfigError("duplicate Codex descendant session files")
            related.update(added)
            newest = max([newest, *(stamp for identifier, stamp in children)])

    def session_metadata(self, info: SessionInfo) -> SessionInfo:
        try:
            state = self.lookup(info.sid, info.cwd)
            if state is None or not state.native_id:
                return info
            found = session_file(state.snapshot, state.native_id)
            return replace(
                info,
                transcript=found.path if found is not None else info.transcript,
                resume=self.resume_command(state),
            )
        except (ConfigError, RecoveryError, OSError):
            return info

    @_recover
    def transcript(self, state: SessionState) -> str:
        found = session_file(state.snapshot, state.native_id) if state.native_id else None
        return found.path if found is not None else ""

    def session_status(self, sid: str, cwd: str, *, transcript: str = "") -> SessionStatus:
        resume = ""
        try:
            state = self.lookup(sid, cwd)
            if state is None or state.native_id is None:
                return SessionStatus(Resumable.NO, None, "")
            resume = self.resume_command(state)
            found = session_file(state.snapshot, state.native_id, recorded=transcript)
            if found is None:
                return SessionStatus(Resumable.NO, None, resume)
            return SessionStatus(Resumable.YES, self.last_active(state, found), resume)
        except (ConfigError, RecoveryError, OSError):
            return SessionStatus(Resumable.UNKNOWN, None, resume)
