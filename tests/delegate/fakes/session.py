"""Delegate tests' Session class; preflight refuses handlers unless `owns`. With spec.handlers,
its first alive() (not one inside a handler) sends each scripted SessionEvent to each handler's
on_event; ABSENT after Exited. `lingers`: no appended Exited(0), it stays live.
"""

import importlib
import uuid

from ensemble.session import ConfigError, EventCall, Exited, Host, Session, SessionEvent, Spec
from ensemble.session.engine.types import Liveness
from tests.session.fakes.session import FakeSession


class SessionClass(FakeSession):
    preflights: list[dict] = []
    preflight_error: Exception | None = None
    owns = True
    lingers = False

    def __init_subclass__(cls, **keywords):
        super().__init_subclass__(**keywords)
        cls.preflights, cls.preflight_error, cls.owns, cls.lingers = [], None, True, False

    def __init__(self, spec, sid, name, *, display) -> None:
        super().__init__(spec, sid, name, display=display)
        self._undelivered = bool(spec.handlers)

    def alive(self) -> bool:
        if self._undelivered:
            self._undelivered = False
            self._deliver()
        return super().alive()

    def _deliver(self) -> None:
        modules = [(importlib.import_module(h.module), h.state) for h in self.spec.handlers]
        generation = uuid.uuid4().hex
        for n, event in enumerate(self.events(), 1):
            if self.lingers and n > len(self.script):
                break
            if not isinstance(event, SessionEvent):
                continue
            for module, state in modules:
                module.on_event(
                    EventCall(
                        session=self,
                        event=event,
                        sid=self.sid,
                        generation=generation,
                        event_id=f"{generation}:{n}",
                        turn=None,
                        state=dict(state),
                    )
                )
            if isinstance(event, Exited):
                self.liveness = Liveness.ABSENT

    @classmethod
    def fresh(cls):
        return cls.scripted()

    @classmethod
    def configure_from(cls, config, *, hosts=None):
        cls.configure(
            clients=config.get("clients", {}),
            hosts=Host.parse_hosts(config.get("hosts")) if hosts is None else hosts,
        )

    @classmethod
    def class_roots(cls, client):
        return ()

    @classmethod
    def preflight(cls, spec, *, display=None, host=None, idle_timeout=None):
        if spec.handlers and not cls.owns:
            raise ConfigError(f"handlers: {spec.client} {spec.mode} has no session harness yet")
        cls.preflights.append(
            {"spec": spec, "display": display, "host": host, "idle_timeout": idle_timeout}
        )
        if cls.preflight_error is not None:
            raise cls.preflight_error

    @classmethod
    def start(cls, spec: Spec, *, display=None, host=None, idle_timeout=None) -> Session:
        return super().start(spec, display=display, host=host, idle_timeout=idle_timeout)
