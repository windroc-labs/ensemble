"""The session layer: Session (engine/base.py), whose one concrete class is HostedSession
(engine/hosted_session.py, modes/), runs agent sessions through each Client (clients/)
on a Host (hosts/); the Viewer (viewer/) shows them. `__all__` is its API for Session users.
"""

from ensemble.session.engine.base import Session, hosted_viewer
from ensemble.session.engine.config import CONFIG_RESOURCES
from ensemble.session.engine.records import RunLog
from ensemble.session.engine.status_log import SessionStatusLog
from ensemble.session.engine.types import (
    SESSION_NAME,
    SESSION_NAME_RULE,
    Access,
    ConfigError,
    Effort,
    Exited,
    IdleTimeout,
    Launch,
    LimitHit,
    Mode,
    NoRecordedSession,
    NotShowable,
    Reply,
    Resumable,
    SessionError,
    SessionStatus,
    Spec,
    Stderr,
    TurnEnd,
    TurnFailed,
    Unanswered,
    Usage,
    Window,
)
from ensemble.session.hosts.base import SIGNALS, Host, Process
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
    "Resumable",
    "RunLog",
    "Host",
    "Process",
    "Viewer",
    "get_viewer",
    "NotShowable",
    "NoRecordedSession",
    "SessionStatusLog",
    "Usage",
    "Window",
    "SessionStatus",
    "Exited",
    "IdleTimeout",
    "LimitHit",
    "Reply",
    "Stderr",
    "TurnEnd",
    "TurnFailed",
    "SessionError",
    "Unanswered",
    "ConfigError",
]


def get_viewer() -> Viewer:
    """The Viewer Session.configure_from built from [viewer]."""
    return hosted_viewer()
