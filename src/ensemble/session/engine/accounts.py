"""Each start's live account readings and the account it runs on."""

import math
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor

from ensemble.session.engine.types import Launch, Usage


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


def choose(left: Mapping[str, float | None], wanted: str = "") -> str | None:
    """`left`: each candidate account's points left. `wanted` while usable (more than 0 left);
    none wanted: the usable one with the most left, a tie by name; None: none usable.
    """
    usable = {name: points for name, points in left.items() if points is not None and points > 0}
    if wanted:
        return wanted if wanted in usable else None
    return min(usable, key=lambda name: (-usable[name], name), default=None)
