"""Names Vulture cannot see used, one entry each with its reason above it. An interface waiting for
its caller names the ticket that calls it; that ticket deletes the entry.
"""

import urllib.request

from ensemble.delegate.composer.teams import TeamKind
from ensemble.delegate.event.types import UpdraftStatus
from ensemble.orchestrator.engine.flow.tracker import Connect
from ensemble.session.clients.claude import ClaudeClient
from ensemble.session.clients.codex import CodexClient
from ensemble.session.clients.opencode import OpenCodeClient
from ensemble.session.engine.base import Session
from ensemble.session.engine.types import Effort, HookKind, NamedKey, SessionRow, Tier, WorkerHook
from ensemble.session.harness.types import EventCall
from ensemble.session.viewer.types import Split
from ensemble.utils import LayerCommand, OutcomeStatus

# Enum members reached only by iteration or by value:
# teams.py reads each TeamKind dir.
_ = TeamKind.ROLES, TeamKind.TEMPLATES
# outcome.py parses OutcomeStatus(status).
_ = OutcomeStatus.NEEDS_INPUT, OutcomeStatus.FAILED
# outcome.py parses UpdraftStatus(status).
_ = UpdraftStatus.CAPPED, UpdraftStatus.STOPPED
# Record checks ctl[0] against LayerCommand's values.
_ = LayerCommand.ORCHESTRATOR, LayerCommand.SESSION
# Effort(value) parses a run's effort; clients/common.py lists the values.
_ = Effort.LOW, Effort.MEDIUM, Effort.HIGH, Effort.XHIGH, Effort.MAX
# Spec checks its tier, and clients/common.py a tiers table's keys, against Tier's values.
_ = Tier.ONE, Tier.TWO, Tier.THREE, Tier.FOUR
# Key matches a `send --key` name by value.
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
# The tmux Host checks a split by value.
_ = Split.RIGHT, Split.BELOW

# session/config.toml loads each Client by its `implementation` path.
_ = ClaudeClient, CodexClient, OpenCodeClient
# The harness sets its PYTHONPATH from Session.class_roots (D16).
_ = Session.class_roots
# Sid-checked CLI closes are to route through Session.retire; delete this entry then.
_ = Session.retire
# The router calls each Connect: Vulture reads a __call__ whose self is positional-only as a
# function (this file is never run).
_ = Connect.__call__
# Only tests read it, but the session interface is frozen (tests/guard_checks/exports.py
# session_interface_snapshot).
_ = SessionRow.last_lines
# A handler's EventCall field; WDRC-204's session harness sets it for each dispatched event.
_ = EventCall.event_id
# A Tui's accepted-prompt hook kind (WDRC-226); WDRC-204's Claude adapter maps a label to it.
_ = HookKind.TURN_STARTED
# WDRC-204/207 consume the guard's retry-stable hook identity during channel deduplication.
_ = WorkerHook.delivery_id
# urllib calls the redirect hook src/broker/internal/launcher/assets/opencode-install.py's NoRedirect overrides.
_ = urllib.request.HTTPRedirectHandler.redirect_request
