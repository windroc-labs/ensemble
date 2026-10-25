"""Per-account usage readings, cached on disk, and the account a start selecting none runs on."""

import contextlib
import json
import math
import os
import sys
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor

from ensemble.session.engine.types import DEFAULT_ACCOUNT, Launch, Usage, Window
from ensemble.utils import STATE, exhausted, file_lock, read_regular, save, summary

TTL = 300  # seconds
UNKNOWN_TTL = 30
CACHE_DIR = f"{STATE}/usage"
CACHE_MAX = 64 << 10


def readings(
    client: str,
    probes: Mapping[str, tuple[str, Launch]],
    read: Callable[[Launch], Usage | None],
) -> dict[str, Usage | None]:
    """Each account's reading: its cached one when it reads the same identity and is under TTL
    (UNKNOWN_TTL when unknown) old, else `read` of its launch, once per identity, all
    concurrently, then cached; one process at a time.
    """
    path = os.path.join(os.path.expanduser(CACHE_DIR), f"{client}.json")
    with contextlib.ExitStack() as held:
        with contextlib.suppress(OSError):
            os.makedirs(os.path.dirname(path), 0o700, exist_ok=True)
            held.enter_context(file_lock(path))
        return _readings(client, path, probes, read)


def _readings(
    client: str,
    path: str,
    probes: Mapping[str, tuple[str, Launch]],
    read: Callable[[Launch], Usage | None],
) -> dict[str, Usage | None]:
    cached = _load(path)
    now = time.time()
    found: dict[str, Usage | None] = {}
    stale: dict[str, tuple[Launch, list[str]]] = {}
    for name, (identity, launch) in probes.items():
        entry = cached.get(name)
        if (
            entry is not None
            and entry[1] == identity
            and 0 <= now - entry[0] < (UNKNOWN_TTL if entry[2] is None else TTL)
        ):
            found[name] = entry[2]
        else:
            stale.setdefault(identity, (launch, []))[1].append(name)
    if stale:
        with ThreadPoolExecutor(len(stale)) as pool:
            read_all = pool.map(read, (launch for launch, _ in stale.values()))
            results = dict(zip(stale, read_all, strict=True))
        at = time.time()
        for identity, (_, names) in stale.items():
            for name in names:
                found[name] = results[identity]
                cached[name] = (at, identity, results[identity])
                print(f"{client} usage: account {name}: {summary(found[name])}", file=sys.stderr)
        _store(path, {name: cached[name] for name in probes})
    return {name: found[name] for name in probes}


def pick(found: Mapping[str, Usage | None]) -> str | None:
    """The account under every threshold with the most usage left; a tie: default, else by name."""
    return _most_left({n: u for n, u in found.items() if u is not None and not exhausted(u)})


def best(found: Mapping[str, Usage | None]) -> str | None:
    """pick, else the known account with the most usage left; None: no reading known."""
    return pick(found) or _most_left({n: u for n, u in found.items() if u is not None})


def _most_left(found: Mapping[str, Usage]) -> str | None:
    return min(found, key=lambda n: (-_left(found[n]), n != DEFAULT_ACCOUNT, n), default=None)


def _left(usage: Usage) -> float:
    windows = (usage.five_hour, usage.weekly)
    return 100 - max((w.used_percent for w in windows if w is not None), default=0)


def _load(path: str) -> dict[str, tuple[float, str, Usage | None]]:
    """The file's well-formed entries; anything else reads as no entry."""
    raw = read_regular(path, limit=CACHE_MAX + 1)
    if raw is None or len(raw) > CACHE_MAX:
        return {}
    try:
        table = json.loads(raw)
    except (ValueError, RecursionError):
        return {}
    if not isinstance(table, dict):
        return {}
    found = {}
    for name, entry in table.items():
        try:
            found[name] = (_number(entry["at"]), _text(entry["identity"]), _usage(entry["usage"]))
        except (KeyError, TypeError, ValueError):
            continue
    return found


def _store(path: str, entries: Mapping[str, tuple[float, str, Usage | None]]) -> None:
    table = {
        name: {"at": at, "identity": identity, "usage": _plain(usage)}
        for name, (at, identity, usage) in entries.items()
    }
    with contextlib.suppress(OSError):
        save(path, json.dumps(table, sort_keys=True))


def _plain(usage: Usage | None):
    if usage is None:
        return None
    return {
        key: None if w is None else [w.used_percent, w.resets_at]
        for key, w in (("five_hour", usage.five_hour), ("weekly", usage.weekly))
    }


def _usage(value) -> Usage | None:
    if value is None:
        return None
    return Usage(_window(value["five_hour"]), _window(value["weekly"]))


def _window(value) -> Window | None:
    if value is None:
        return None
    used, resets_at = value
    return Window(_number(used), None if resets_at is None else _number(resets_at))


def _number(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(value)
    return value


def _text(value) -> str:
    if not isinstance(value, str):
        raise TypeError(value)
    return value
