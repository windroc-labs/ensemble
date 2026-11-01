"""Each start's live account readings and the account it runs on."""

import math
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor

from ensemble.session.engine.types import Launch, Usage
from ensemble.utils import exhausted


def live(
    probes: Mapping[str, Launch], read: Callable[[Launch], Usage | None]
) -> dict[str, Usage | None]:
    """Each account's reading, all read concurrently."""
    if not probes:
        return {}
    with ThreadPoolExecutor(len(probes)) as pool:
        return dict(zip(probes, pool.map(read, probes.values()), strict=True))


def left(usage: Usage | None, limit: float) -> float | None:
    """Points left under `limit` by the most-used window; inf: no window; None: no reading."""
    if usage is None:
        return None
    used = [w.used_percent for w in (usage.five_hour, usage.weekly) if w is not None]
    return limit - max(used) if used else math.inf


def pick(found: Mapping[str, Usage | None]) -> str | None:
    """The account under every threshold with the most usage left; a tie: by name."""
    return _most_left({n: u for n, u in found.items() if u is not None and not exhausted(u)})


def best(found: Mapping[str, Usage | None]) -> str | None:
    """pick, else the known account with the most usage left; None: no reading known."""
    return pick(found) or _most_left({n: u for n, u in found.items() if u is not None})


def _most_left(found: Mapping[str, Usage]) -> str | None:
    return min(found, key=lambda n: (-_left(found[n]), n), default=None)


def _left(usage: Usage) -> float:
    windows = (usage.five_hour, usage.weekly)
    return 100 - max((w.used_percent for w in windows if w is not None), default=0)
