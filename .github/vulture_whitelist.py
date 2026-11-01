"""Names Vulture cannot see used, each under its reason. An entry waiting for its caller names
that caller; the change adding the caller deletes the entry.
"""

import urllib.request

from ensemble.delegate.composer.teams import TeamKind
from ensemble.delegate.event.types import UpdraftStatus
from ensemble.orchestrator.engine.flow.tracker import Connect
from ensemble.session.clients.base import Client
from ensemble.session.clients.claude import ClaudeClient
from ensemble.session.clients.codex import CodexClient
from ensemble.session.clients.opencode import OpenCodeClient
from ensemble.session.engine.base import Session
from ensemble.session.engine.types import Effort, NamedKey, SessionRow, Tier
from ensemble.session.viewer.types import Split
from ensemble.utils import LayerCommand, OutcomeStatus

# Enum members read only by value or iteration.
_ = TeamKind.ROLES, TeamKind.TEMPLATES
_ = OutcomeStatus.NEEDS_INPUT, OutcomeStatus.FAILED
_ = UpdraftStatus.CAPPED, UpdraftStatus.STOPPED
_ = LayerCommand.ORCHESTRATOR, LayerCommand.SESSION
_ = Effort.LOW, Effort.MEDIUM, Effort.HIGH, Effort.XHIGH, Effort.MAX
_ = Tier.ONE, Tier.TWO, Tier.THREE, Tier.FOUR
_ = (
    NamedKey.ENTER,
    NamedKey.ESCAPE,
    NamedKey.TAB,
    NamedKey.BTAB,
    NamedKey.BSPACE,
    NamedKey.UP,
    NamedKey.DOWN,
    NamedKey.LEFT,
    NamedKey.RIGHT,
    NamedKey.HOME,
    NamedKey.PAGE_UP,
    NamedKey.PAGE_DOWN,
    NamedKey.F1,
    NamedKey.F2,
    NamedKey.F3,
    NamedKey.F4,
    NamedKey.F5,
    NamedKey.F6,
    NamedKey.F7,
    NamedKey.F8,
    NamedKey.F9,
    NamedKey.F10,
    NamedKey.F11,
    NamedKey.F12,
)
_ = Split.RIGHT, Split.BELOW

# session/config.toml's `implementation` paths.
_ = ClaudeClient, CodexClient, OpenCodeClient
# The harness builds its PYTHONPATH from it.
_ = Session.class_roots
# Vulture misses a positional-only-self __call__.
_ = Connect.__call__
# Frozen session interface; only tests read it.
_ = SessionRow.last_lines
_ = Client.usage_probe, CodexClient.usage_probe, OpenCodeClient.usage_probe
# opencode-install.py's NoRedirect overrides it.
_ = urllib.request.HTTPRedirectHandler.redirect_request
