"""Session public exports."""

import inspect
from unittest import mock

from ensemble import session
from ensemble.session import (
    Access,
    Effort,
    Exited,
    Host,
    IdleTimeout,
    Launch,
    LimitHit,
    Mode,
    Reply,
    Resumable,
    RunLog,
    SessionStatus,
    SessionStatusLog,
    Spec,
    Stderr,
    TurnEnd,
    TurnFailed,
    Usage,
    Viewer,
    Window,
)
from ensemble.session.engine import types
from ensemble.session.engine.hosted_session import HostedSession
from tests.session.case import HOSTS, Case

EVENTS = {Exited, IdleTimeout, LimitHit, Reply, Stderr, TurnEnd, TurnFailed}

ALLOWED_CONCRETE = {
    SessionStatusLog,
    Usage,
    Window,
    Spec,
    Access,
    SessionStatus,
    Mode,
    Launch,
    Effort,
    Resumable,
    RunLog,
    Viewer,
    *EVENTS,
}


class Exports(Case):
    def test_the_types_are_engine_types(self):
        for name in session.__all__:
            if hasattr(types, name):
                with self.subTest(name=name):
                    self.assertIs(getattr(session, name), getattr(types, name))

    def test_get_viewer_is_the_one_viewer_session_configure_built(self):
        self.addCleanup(setattr, HostedSession, "_setup", None)
        HostedSession.configure(
            clients={},
            hosts=Host.parse_hosts({**HOSTS, "default": "tmux"}),
            viewer_factory=lambda _settings: mock.sentinel.viewer,
        )
        self.assertIs(session.get_viewer(), mock.sentinel.viewer)

    def test_the_only_concrete_classes_are_the_helpers_and_exceptions(self):
        concrete = {
            value
            for value in (getattr(session, n) for n in session.__all__)
            if inspect.isclass(value)
            and not issubclass(value, Exception)
            and not inspect.isabstract(value)
        }
        self.assertEqual(concrete, ALLOWED_CONCRETE)
