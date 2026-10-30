"""Per-account usage readings, cached on disk, and the account a start selecting none runs on."""

import contextlib
import json
import math
import os
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor

from ensemble.session.engine.types import DEFAULT_ACCOUNT, Launch, Usage, Window
from ensemble.utils import STATE, exhausted, file_lock, read_regular, save, summary

TTL = 300  # seconds
UNKNOWN_TTL = 30
CACHE_DIR = f"{STATE}/usage"
CACHE_MAX = 64 << 10
SESSION_SPAN = 5 * 3600
HOLD_MAX = 7 * 24 * 3600


def readings(
    client: str,
    probes: Mapping[str, tuple[str, Launch]],
    read: Callable[[Launch], Usage | None],
) -> dict[str, Usage | None]:
    """Each account's reading: over while held; else its cached one when it reads the same
    identity and is under TTL (UNKNOWN_TTL when unknown) old, else `read` of its launch, once per
    identity, all concurrently, then cached; one process at a time.
    """
    holds_path = _path(client, "holds")
    with _lock(holds_path):
        holds = _holds(holds_path, time.time())
    path = _path(client)
    with _lock(path):
        return _readings(client, path, probes, read, holds)


def hold(client: str, account: str, resets_at: float | None) -> None:
    """A run on `account` hit a usage limit: over until `resets_at` (None: SESSION_SPAN; at most
    HOLD_MAX away), whatever its probe, of its dir, not its token, reads.
    """
    now = time.time()
    until = min(now + SESSION_SPAN if resets_at is None else resets_at, now + HOLD_MAX)
    if until <= now:
        return
    path = _path(client, "holds")
    with _lock(path):
        holds = _holds(path, now)
        holds[account] = max(until, holds.get(account, until))
        with contextlib.suppress(OSError):
            save(path, json.dumps(holds, sort_keys=True))


def _path(client: str, kind: str = "") -> str:
    name = f"{client}-{kind}" if kind else client
    return os.path.join(os.path.expanduser(CACHE_DIR), f"{name}.json")


@contextlib.contextmanager
def _lock(path: str) -> Iterator[None]:
    with contextlib.ExitStack() as held:
        with contextlib.suppress(OSError):
            os.makedirs(os.path.dirname(path), 0o700, exist_ok=True)
            held.enter_context(file_lock(path))
        yield


def _over(until: float, now: float) -> Usage:
    window = Window(100.0, until)
    return Usage(window, None) if until - now <= SESSION_SPAN else Usage(None, window)


def _readings(
    client: str,
    path: str,
    probes: Mapping[str, tuple[str, Launch]],
    read: Callable[[Launch], Usage | None],
    holds: Mapping[str, float],
) -> dict[str, Usage | None]:
    cached = _load(path)
    now = time.time()
    found: dict[str, Usage | None] = {}
    stale: dict[str, tuple[Launch, list[str]]] = {}
    for name, (identity, launch) in probes.items():
        entry = cached.get(name)
        if name in holds:
            found[name] = _over(holds[name], now)
        elif entry is not None and entry[1] == identity and _fresh(entry[0], entry[2], now):
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
        _store(path, {name: cached[name] for name in probes if name in cached})
    return {name: found[name] for name in probes}


def _fresh(at: float, usage: Usage | None, now: float) -> bool:
    windows = () if usage is None else tuple(filter(None, (usage.five_hour, usage.weekly)))
    if any(w.resets_at is not None and at < w.resets_at <= now for w in windows):
        return False
    overdue = (
        usage is not None
        and exhausted(usage)
        and any(w.resets_at is not None and w.resets_at <= now for w in windows)
    )
    return 0 <= now - at < (UNKNOWN_TTL if usage is None or overdue else TTL)


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


def _table(path: str) -> dict:
    raw = read_regular(path, limit=CACHE_MAX + 1)
    if raw is None or len(raw) > CACHE_MAX:
        return {}
    try:
        table = json.loads(raw)
    except (ValueError, RecursionError):
        return {}
    return table if isinstance(table, dict) else {}


def _load(path: str) -> dict[str, tuple[float, str, Usage | None]]:
    """The file's well-formed entries; anything else reads as no entry."""
    found = {}
    for name, entry in _table(path).items():
        try:
            found[name] = (_number(entry["at"]), _text(entry["identity"]), _usage(entry["usage"]))
        except (KeyError, TypeError, ValueError):
            continue
    return found


def _holds(path: str, now: float) -> dict[str, float]:
    found = {}
    for name, until in _table(path).items():
        with contextlib.suppress(ValueError):
            if _number(until) > now:
                found[name] = until
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
