"""Orchestrator quota decisions over Delegate per-account headroom, for scheduling and recovery:
a start is allowed while any account has points left under its max_usage (inf: no usage panel).
"""

import functools
from collections.abc import Callable

from ensemble import delegate
from ensemble.orchestrator.engine.log import log
from ensemble.orchestrator.engine.types import ConfigError


def _probe(role, cfg, provider, client):
    """Each account's headroom, else [], and the configuration or runtime error that left it so."""
    if provider is None:
        return [], None
    overlays = (cfg.core,)
    if client is not None:
        overlays += ({"roles": {role: {"client": client}}},)
    try:
        return provider.usage(role, overlays=overlays), None
    except delegate.ConfigError as error:
        return [], error
    except delegate.SessionError as error:
        return [], error


def _points(left) -> str:
    return "unknown" if left is None else f"{left:g}"


def _shown(found) -> str:
    return " ".join(f"{h.account}={_points(h.left)}" for h in found) or "none"


class Gate:
    def __init__(self, say=None, *, cfg, dry):
        self.cfg = cfg
        self.dry, self.seen = dry, {}
        self.say: Callable[..., None] = say or functools.partial(log, "router", dry=dry)

    def blocked(self, role, ident, *, key, reject_config=False):
        kind, client = key
        if not self.cfg.usage_limit.get(client, True):
            return False
        if key not in self.seen:
            self.seen[key] = self._check(role, client, kind)
        reason, fields, error = self.seen[key]
        if reject_config and error is not None:
            raise error
        if reason:
            self.say("usage-skip", ident, once=True, client=client, reason=reason, **fields)
        return reason is not None

    def _check(self, role, client, kind):
        found, error = _probe(role, self.cfg, self.cfg.delegates.get(kind), client)
        if isinstance(error, delegate.SessionError):
            raise ConfigError(f"host usage probe failed for client {client}: {error}") from None
        shown = {"usage": _shown(found)}
        if any(h.left is not None and h.left > 0 for h in found):
            reason = None
        elif all(h.left is None for h in found):
            reason = "usage unknown"
        else:
            reason = "usage"
        fields = shown if reason == "usage" else {}
        if self.dry:
            self.say("usage", client=client, **shown, allowed=reason is None)
        return reason, fields, error
