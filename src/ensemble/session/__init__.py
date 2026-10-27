"""The session layer: Session (engine/base.py), whose concrete classes are HostedSession
(engine/hosted_session.py, modes/) and OwnedSession (harness/view.py), the control view of a
worker its session harness owns, runs agent sessions through each Client (clients/) on a Host
(hosts/); the Viewer (viewer/) shows them. `__all__` is its API for Session users.
"""

from ensemble.session.engine.base import Session, hosted_viewer
from ensemble.session.engine.config import CONFIG_RESOURCES
from ensemble.session.engine.records import RunLog
from ensemble.session.engine.status_log import SessionStatusLog
from ensemble.session.engine.types import (
    SESSION_NAME,
    SESSION_NAME_RULE,
    Access,
    Activity,
    Blocked,
    ConfigError,
    Effort,
    EnvironmentSpec,
    Exited,
    HookCallback,
    IdleTimeout,
    Launch,
    LimitHit,
    Mode,
    NoRecordedSession,
    NotShowable,
    Reply,
    Resumable,
    SessionBusy,
    SessionError,
    SessionEvent,
    SessionStatus,
    Spec,
    Stderr,
    TurnEnd,
    TurnFailed,
    TurnStarted,
    Unanswered,
    Usage,
    Window,
)
from ensemble.session.harness.types import EventCall
from ensemble.session.hosts.base import SIGNALS, Host
from ensemble.session.viewer.viewer import Viewer

__all__ = [
    "CONFIG_RESOURCES",
    "SESSION_NAME",
    "SESSION_NAME_RULE",
    "SIGNALS",
    "Session",
    "Spec",
    "Access",
    "Mode",
    "Launch",
    "Effort",
    "EnvironmentSpec",
    "HookCallback",
    "EventCall",
    "Resumable",
    "RunLog",
    "Host",
    "Viewer",
    "get_viewer",
    "NotShowable",
    "NoRecordedSession",
    "SessionStatusLog",
    "Usage",
    "Window",
    "SessionStatus",
    "SessionEvent",
    "TurnStarted",
    "Activity",
    "Blocked",
    "Exited",
    "IdleTimeout",
    "LimitHit",
    "Reply",
    "Stderr",
    "TurnEnd",
    "TurnFailed",
    "SessionError",
    "SessionBusy",
    "Unanswered",
    "ConfigError",
]


def get_viewer() -> Viewer:
    """The Viewer Session.configure_from built from [viewer]."""
    return hosted_viewer()
