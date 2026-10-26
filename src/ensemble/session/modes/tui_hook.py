"""A tui client's hook: drains stdin, checks callback sources, binds identity and journals
hooks.jsonl before notifying the optional Host. No stdout; failures log a traceback and stderr line.
The CLI defaults to exit 0; --strict reports failure; an --event retry of the last journaled event
reruns only the Host. session.json declares client_hook and host_hook via HookCallback.fields().
Resolve modules over hook roots, all callbacks' roots, then sys.path.
Call on_hook(HookCall); load external modules from declared roots without changing sys.path.
"""

from __future__ import annotations

import fcntl
import importlib
import importlib.machinery
import importlib.util
import json
import os
import re
import stat
import sys
import traceback
from collections.abc import Sequence
from contextlib import suppress
from datetime import datetime

from ensemble.session.engine.types import MARKS, HookCall, HookError, StatusState
from ensemble.session.hosts.base import TUI_STATE
from ensemble.utils import read_regular

PAYLOAD_MAX = 64 << 20  # bytes of stdin parsed

STATE_MAX = 1 << 20  # bytes of session.json read

BLOCK = 1 << 16

CUT = 4096

LABEL = re.compile(r"[A-Za-z0-9_.-]{1,64}")


HOOKS = "hooks.jsonl"

ROTATED = HOOKS + ".1"

HOOKS_MAX = 1 << 20  # bytes


def _abs(value) -> bool:
    return isinstance(value, str) and value.isprintable() and os.path.isabs(value)


def _arguments(argv: Sequence[str]) -> tuple[str, str]:
    if len(argv) != 2 or not _abs(argv[0]) or not LABEL.fullmatch(argv[1]):
        raise HookError(
            "usage: ensemble-session hook <absolute session dir> <label [A-Za-z0-9_.-]{1,64}>"
        )
    return argv[0], argv[1]


def say(text: str) -> None:
    """One stderr line; none when stderr is closed (print would fall back to stdout)."""
    if sys.stderr is not None:
        print("tui_hook: " + " ".join(text.split()), file=sys.stderr)


SESSION_STATE = {  # what this hook or a client_hook reads
    "words": lambda v: (
        isinstance(v, dict) and all(LABEL.fullmatch(k) and w in MARKS for k, w in v.items())
    ),
    "events": lambda v: v is None or _abs(v),
    "sid": lambda v: isinstance(v, str) and v != "" and v.isprintable() and "/" not in v,
    "workdir": _abs,
}


def _prepare(argv: Sequence[str], roots: Sequence[str]):
    """The session dir, label and state, and each callback's module specs, found over `roots`,
    then every callback's roots, then sys.path.
    """
    folder, label = _arguments(argv)
    path = os.path.join(folder, TUI_STATE)
    state = _read_state(path, HookError)
    _check_callbacks(state, path, HookError)
    hooks = [
        (kind, state[kind + "_hook"]) for kind in ("client", "host") if kind + "_hook" in state
    ]
    for kind, hook in hooks:
        if missing := [root for root in hook["roots"] if not os.path.isdir(root)]:
            raise HookError(f"{kind} hook {hook['module']}: root {missing[0]} is no directory")
    search = list(
        dict.fromkeys([*roots, *(root for _, hook in hooks for root in hook["roots"]), *sys.path])
    )
    specs = {
        hook["module"]: _check_module(hook["module"], search, kind + " hook", HookError)
        for kind, hook in hooks
    }
    return folder, label, state, specs


def main(argv: Sequence[str], stdin, *, roots: Sequence[str], event: str | None = None) -> None:
    payload = read_payload(stdin)
    if event is not None and not LABEL.fullmatch(event):
        raise HookError("usage: --event <relay event [A-Za-z0-9_.-]{1,64}>")
    folder, label, state, specs = _prepare(argv, roots)
    _dispatch(folder, label, payload, state, specs, event=event)


def _dispatch(folder, label, payload, state, specs, *, event):
    _validate_state(state, os.path.join(folder, TUI_STATE), HookError)

    def call(kind, given) -> HookCall:
        return HookCall(
            label, given, state["sid"], state["workdir"], state["events"], state[kind]["state"]
        )

    journaled = None if event is None else _journaled(os.path.join(folder, HOOKS), event, label)
    if journaled is not None:  # a retry reruns only the Host
        payload = journaled
    elif client_hook := state.get("client_hook"):
        client = _load_callback(client_hook["module"], "client hook", specs)
        payload = client.on_hook(call("client_hook", payload))
        if payload is None:
            return
        if not isinstance(payload, dict):
            raise HookError(
                f"client hook {client_hook['module']}: on_hook returned "
                f"{type(payload).__name__}, not a payload dict or None"
            )
    word = state["words"].get(label)
    host_hook = state.get("host_hook") if word else None
    host = None if host_hook is None else _load_callback(host_hook["module"], "host hook", specs)
    if journaled is None:
        if word != StatusState.DONE:
            payload = cut(payload)
        stamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
        record = {"ts": stamp, "label": label, "payload": payload}
        append(os.path.join(folder, HOOKS), record if event is None else {**record, "event": event})
    if host is not None:
        host.on_hook(word, call("host_hook", payload))


def _journaled(path: str, event: str, label: str) -> dict | None:
    """The payload of the last hooks.jsonl record when it journaled `event` under `label`."""
    data = read_regular(path) or b""
    try:
        record = json.loads(data.rstrip(b"\n").rpartition(b"\n")[2])
    except (ValueError, RecursionError):
        return None
    if (
        isinstance(record, dict)
        and record.get("event") == event
        and record.get("label") == label
        and isinstance(record.get("payload"), dict)
    ):
        return record["payload"]
    return None


def read_payload(stream) -> dict:
    """stdin's JSON object, read to its end (the writer never stalls); {} when over PAYLOAD_MAX bytes
    or not one.
    """
    chunks, size = [], 0
    while True:
        chunk = stream.read(BLOCK)
        if not chunk:
            break
        size += len(chunk)
        if size <= PAYLOAD_MAX:
            chunks.append(chunk)
    if size > PAYLOAD_MAX:
        say(f"payload: over {PAYLOAD_MAX} bytes")
        return {}
    try:
        payload = json.loads(b"".join(chunks))
    except (ValueError, RecursionError):
        payload = None
    if not isinstance(payload, dict):
        say("payload: not a JSON object")
        return {}
    return payload


def read_state(path: str, *, error: type[Exception] | None = None) -> dict:
    """session.json: a regular file, at most STATE_MAX bytes, its fields passing
    SESSION_STATE; else `error`.
    """
    error = HookError if error is None else error
    state = _read_state(path, error)
    _validate_state(state, path, error)
    _check_callbacks(state, path, error)
    return state


def _read_state(path, error):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(
                os.fstat(fd).st_mode
            ):  # first: fdopen raises on a directory, leaving fd open
                raise error(f"{path}: not a regular file")
            with os.fdopen(fd, "rb", closefd=False) as f:
                data = f.read(STATE_MAX + 1)
        finally:
            os.close(fd)
    except OSError as e:
        raise error(f"{path}: {e.strerror}") from None
    if len(data) > STATE_MAX:
        raise error(f"{path}: over {STATE_MAX} bytes")
    try:
        state = json.loads(data)
    except (ValueError, RecursionError):
        state = None
    if not isinstance(state, dict):
        raise error(f"{path}: not a JSON object")
    return state


def _validate_state(state, path, error):
    for field, check in SESSION_STATE.items():
        if field not in state or not check(state[field]):
            raise error(f"{path}: bad {field}")


def _check_callbacks(state, path, error):
    for hook in ("client_hook", "host_hook"):
        value = state.get(hook, {})
        if hook in state and (
            not isinstance(value, dict)
            or set(value) != {"module", "state", "roots"}
            or not isinstance(value["module"], str)
            or not all(part.isidentifier() for part in value["module"].split("."))
            or not isinstance(value["state"], dict)
            or not isinstance(value["roots"], list)
            or not all(map(_abs, value["roots"]))
            or len(set(value["roots"])) != len(value["roots"])
        ):
            raise error(f"{path}: bad {hook}")


def _check_module(name: str, search: list[str], kind: str, error):
    """What `name` and each package above it were found as over `search`, outermost first."""
    paths, specs = search, []
    parts = name.split(".")
    for index in range(len(parts)):
        # A segment lookup needs no loaded parent, including for namespace packages.
        spec = importlib.machinery.PathFinder.find_spec(parts[index], paths)
        if spec is None or (spec.origin is None and index == len(parts) - 1):
            raise error(f"{kind} {name}: cannot find Python module")
        if spec.origin is not None:
            try:
                info = os.stat(spec.origin)
            except OSError as failure:
                raise error(f"{kind} {name}: {failure.strerror}") from failure
            if not stat.S_ISREG(info.st_mode):
                raise error(f"{kind} {name}: not a regular file")
        paths = list(spec.submodule_search_locations or ())
        specs.append(spec)
    return specs


def _load_callback(name: str, kind: str, specs):
    """`name` from what _check_module found: each segment not yet loaded, from its file or, for a
    namespace package, its directories.
    """
    parts = name.split(".")
    for index, found in enumerate(specs[name]):
        fullname = ".".join(parts[: index + 1])
        if fullname in sys.modules:
            continue
        locations = found.submodule_search_locations
        if found.origin is None:
            spec = importlib.machinery.ModuleSpec(fullname, None, is_package=True)
            spec.submodule_search_locations = list(locations or ())
        else:
            spec = importlib.util.spec_from_file_location(
                fullname,
                found.origin,
                submodule_search_locations=None if locations is None else list(locations),
            )
            if spec is None:
                raise HookError(f"{kind} {name}: cannot load {found.origin}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[fullname] = module
        try:
            if spec.loader is not None and found.origin is not None:
                spec.loader.exec_module(module)
        except BaseException:
            del sys.modules[fullname]
            raise
        parent, _, child = fullname.rpartition(".")
        if parent:
            setattr(sys.modules[parent], child, module)
    module = sys.modules[name]
    if not callable(getattr(module, "on_hook", None)):
        raise HookError(f"{kind} {name}: missing on_hook callback")
    return module


def cut(value):
    if isinstance(value, str):
        return value[:CUT]
    if isinstance(value, list):
        return [cut(v) for v in value]
    if isinstance(value, dict):
        return {k: cut(v) for k, v in value.items()}
    return value


def append(path: str, record: dict) -> None:
    """One write under an exclusive flock to a regular file, created 0600, still at `path` once
    locked, first rotated if the line takes it past HOOKS_MAX.
    """
    line = (json.dumps(record) + "\n").encode()
    try:
        while True:
            # O_NONBLOCK: a FIFO put there fails instead of hanging the hook
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK, 0o600)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise HookError(f"{path}: not a regular file")
                fcntl.flock(fd, fcntl.LOCK_EX)
                opened = os.fstat(fd)
                try:
                    current = os.path.samestat(opened, os.stat(path))
                except FileNotFoundError:
                    current = False
                if not current:
                    continue
                if opened.st_size and opened.st_size + len(line) > HOOKS_MAX:
                    os.replace(path, os.path.join(os.path.dirname(path), ROTATED))
                    continue
                written = os.write(fd, line)
                break
            finally:
                os.close(fd)
    except OSError as e:
        raise HookError(f"{path}: {e.strerror}") from None
    if written != len(line):
        raise HookError(f"{path}: short write")


def run(argv: Sequence[str], stdin, *, roots: Sequence[str], event: str | None = None) -> bool:
    """One hook call, as `ensemble-session hook [--event ID] DIR LABEL` runs it with its own source
    roots, its payload stdin's bytes (stdin may be None): returns success, never raises or writes
    stdout; a failure keeps its traceback in DIR/hook.log and one stderr line.
    """
    try:
        main(argv, stdin.buffer, roots=roots, event=event)
    except Exception as e:  # noqa: BLE001  # the hook must never fail the client
        if argv and _abs(argv[0]):
            with (
                suppress(OSError),
                open(
                    os.path.join(argv[0], "hook.log"),
                    "a",
                    encoding="utf-8",
                    errors="backslashreplace",
                ) as file,
            ):
                file.write(traceback.format_exc())
        say(str(e) if isinstance(e, HookError) else f"{type(e).__name__}: {e}")
        return False
    else:
        return True
