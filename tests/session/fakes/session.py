"""FakeSession, a Session in the caller's process with no Host (it cannot show). Its sessions
yield the class's script (FakeSession's is empty; scripted(*events) makes a subclass with those),
then Exited(0) unless it ends with Exited. FakeSession and each subclass have their own started
(start's Spec and keywords), live (name → session; stop and close remove it) and configured.
"""

from __future__ import annotations

import re
import uuid

from ensemble.session import NotShowable, Session
from ensemble.session.engine.types import (
    ConfigError,
    Exited,
    Liveness,
    Reply,
    Resumable,
    SessionError,
    SessionInfo,
    SessionRow,
    SessionStatus,
    TurnEnd,
)
from ensemble.utils import Status, StatusState
from tests.session.fakes.host import FakeHost


class FakeSession(Session):
    script: tuple = ()
    started: list[dict] = []
    live: dict[str, FakeSession] = {}
    configured: dict | None = None
    usage_result = None

    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)
        cls.started, cls.live, cls.configured = [], {}, None

    @classmethod
    def scripted(cls, *events) -> type[FakeSession]:
        return type(cls.__name__, (cls,), {"script": events})

    def __init__(self, spec, sid, name, *, display) -> None:
        self.spec, self.sid, self.name = spec, sid, name
        self.client, self.mode = spec.client, spec.mode
        self.display_value = display
        self.sent: list[str] = []
        self.liveness, self._reply = Liveness.LIVE, None
        ended = self.script and isinstance(self.script[-1], Exited)
        self._pending = iter(self.script if ended else (*self.script, Exited(0)))

    def events(self):
        for event in self._pending:
            if isinstance(event, Reply):
                self._reply = event.text
            elif isinstance(event, TurnEnd) and event.reply:
                self._reply = event.reply
            yield event

    def send(self, text: str) -> None:
        self.sent.append(text)

    def press(self, keys) -> None:
        self.sent.append(tuple(keys))

    def alive(self) -> bool:
        return self.liveness == Liveness.LIVE

    def status(self) -> Status:
        return Status(
            name=self.name,
            alive=self.alive(),
            state={Liveness.LIVE: StatusState.WORKING, Liveness.UNKNOWN: StatusState.UNKNOWN}.get(
                self.liveness, StatusState.GONE
            ),
            outcome=None,
            attach=None,
            hidden=False,
        )

    def stop(self) -> None:
        self.liveness = Liveness.ABSENT
        if self.live.get(self.name) is self:
            del self.live[self.name]

    def close(self) -> None:
        self.stop()

    def info(self) -> SessionInfo:
        return SessionInfo(
            sid=self.sid,
            cwd=self.spec.access.cwd,
            project=False,
            transcript="",
            resume="",
        )

    @property
    def reply(self) -> str | None:
        return self._reply

    def session_status(self) -> SessionStatus:
        return SessionStatus(Resumable.YES, None, "")

    def record(self) -> None:
        return None

    @property
    def showable(self) -> bool:
        return False

    def show(self, *, display=None, command=None):
        raise NotShowable("host none cannot show")

    def hide(self) -> None:
        raise NotShowable("host none cannot show")

    def attach_command(self):
        raise NotShowable("host none cannot show")

    def content(self):
        return self.screen()

    def screen(self, lines=None):
        raise NotShowable("host none cannot show")

    @classmethod
    def configure(cls, *, clients, hosts) -> None:
        cls.configured = {"clients": clients, "hosts": hosts}

    @classmethod
    def start(cls, spec, *, display=None, host=None, idle_timeout=None) -> FakeSession:
        for field in ("client", "mode"):
            if getattr(spec, field) is None:
                raise ConfigError(f"spec.{field}: required to start")
        cls.started.append(
            {"spec": spec, "display": display, "host": host, "idle_timeout": idle_timeout}
        )
        sid = spec.sid or str(uuid.uuid4())
        name = spec.name or f"{spec.client}-{sid[:8]}"
        if name in cls.live:
            raise SessionError(f"session {name} is live")
        cls.live[name] = cls(spec, sid, name, display=display)
        return cls.live[name]

    @classmethod
    def attach(cls, name, *, sid=None, host=None, idle_timeout=None) -> FakeSession:
        if name not in cls.live:
            raise SessionError(f"no session {name}")
        session = cls.live[name]
        if sid is not None and sid != session.sid:
            raise SessionError(f"session {name}: expected sid {sid!r}, found {session.sid!r}")
        return session

    @classmethod
    def retire(cls, name, *, sid=None, host=None) -> None:
        cls.attach(name, sid=sid, host=host).close()

    @classmethod
    def list(cls, match=None, *, display=None) -> list[SessionRow]:
        return [
            SessionRow(
                name=name,
                sid=s.sid,
                client=s.client,
                state="working",
                display=None,
                shown=None,
                last_lines=None,
                own=False,
            )
            for name, s in sorted(cls.live.items())
            if (match is None or re.fullmatch(match, name))
            and (display is None or display != "" and s.display_value == display)
        ]

    @classmethod
    def usage(cls, client):
        return cls.usage_result

    @classmethod
    def layout(cls, *, display=None) -> None:
        return None

    @classmethod
    def writable_dirs(cls, spec) -> tuple[str, ...]:
        return ()

    @classmethod
    def display(cls, value=None, *, host=None):
        return value

    @classmethod
    def host(cls, name=None) -> FakeHost:
        return FakeHost(name or "fake")
