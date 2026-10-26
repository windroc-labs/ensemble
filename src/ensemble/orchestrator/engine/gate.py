"""Orchestrator quota decisions over Delegate usage readings, for scheduling and recovery."""

import functools
from collections.abc import Callable

from ensemble import delegate
from ensemble.orchestrator.engine.log import log
from ensemble.orchestrator.engine.types import ConfigError
from ensemble.utils import exhausted, summary

BRAKE_5H = 80


def read_usage(role, cfg, *, client=None):
    return _probe(role, cfg, cfg.delegate_cls, client)[0]


def _probe(role, cfg, provider, client):
    """Usage, else None, and the configuration or runtime error that left it unknown."""
    if provider is None:
        return None, None
    overlays = (cfg.core,)
    if client is not None:
        overlays += ({"roles": {role: {"client": client}}},)
    try:
        return provider.usage(role, overlays=overlays), None
    except delegate.ConfigError as error:
        return None, error
    except delegate.SessionError as error:
        return None, error


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
        usage, error = _probe(role, self.cfg, self.cfg.delegates.get(kind), client)
        if isinstance(error, delegate.SessionError):
            raise ConfigError(f"host usage probe failed for client {client}: {error}") from None
        shown = {"usage": summary(usage)}
        reason = "usage unknown" if usage is None else "usage" if exhausted(usage) else None
        fields = shown if reason == "usage" else {}
        if self.dry:
            self.say("usage", client=client, **shown, allowed=reason is None)
        return reason, fields, error
