"""Session public exports."""

import inspect
import typing
from unittest import mock

from ensemble import session
from ensemble.session import (
    Access,
    Activity,
    Blocked,
    Effort,
    EnvironmentSpec,
    EventCall,
    Exited,
    HookCallback,
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
    TurnStarted,
    Usage,
    Viewer,
    Window,
    extension,
)
from ensemble.session.engine import types
from ensemble.session.engine.base import Session
from ensemble.session.engine.hosted_session import HostedSession
from tests.session.case import HOSTS, Case

EVENTS = {
    Activity,
    Blocked,
    Exited,
    IdleTimeout,
    LimitHit,
    Reply,
    Stderr,
    TurnEnd,
    TurnFailed,
    TurnStarted,
}

ALLOWED_CONCRETE = {
    SessionStatusLog,
    Usage,
    Window,
    Spec,
    EnvironmentSpec,
    Access,
    SessionStatus,
    Mode,
    Launch,
    Effort,
    Resumable,
    RunLog,
    Viewer,
    HookCallback,
    EventCall,
    *EVENTS,
}


class Exports(Case):
    def test_event_call_types_the_session_it_controls_without_a_reverse_import(self):
        self.assertEqual(EventCall.__module__, "ensemble.session.harness.types")
        self.assertIs(typing.get_type_hints(EventCall)["session"], Session)
        self.assertFalse(hasattr(types, "EventCall"))

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

    def test_no_usage_query_or_headroom_list_is_public(self):
        for cls in (session.Session, HostedSession):
            with self.subTest(cls=cls.__name__):
                self.assertFalse(hasattr(cls, "usage"))
        for module in (session, extension):
            with self.subTest(module=module.__name__):
                self.assertNotIn("Headroom", module.__all__)
                self.assertFalse(hasattr(module, "Headroom"))

    def test_the_only_concrete_classes_are_the_helpers_and_exceptions(self):
        concrete = {
            value
            for value in (getattr(session, n) for n in session.__all__)
            if inspect.isclass(value)
            and not issubclass(value, Exception)
            and not inspect.isabstract(value)
        }
        self.assertEqual(concrete, ALLOWED_CONCRETE)
