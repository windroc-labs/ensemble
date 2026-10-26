"""Session, the ABC of an agent session on a Host. Its class methods on Session itself are
HostedSession's (loaded when called); a subclass's are its own.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any

from ensemble.session.engine.types import (
    ACCOUNT_SET_BY,
    ConfigError,
    Hosts,
    IdleTimeout,
    KeyPress,
    Mode,
    ProcessOptions,
    SessionEvent,
    SessionInfo,
    SessionRow,
    SessionStatus,
    Spec,
    Stderr,
    Usage,
)
from ensemble.session.hosts.base import Host
from ensemble.utils import ACCOUNT_ENV, Display, Record, Status, load_class

HOSTED = "ensemble.session.engine.hosted_session:HostedSession"


class Session(ABC):
    """show, hide, attach_command, screen: NotShowable unless showable (its Host is
    attachable). alive: False while its host cannot tell (status: unknown).
    """

    sid: str
    client: str
    mode: Mode
    name: str | None

    @abstractmethod
    def events(self) -> Iterator[SessionEvent | Stderr | IdleTimeout]:
        """FR-27 target: observes recorded events; iterator exit never ends supervision."""

    @abstractmethod
    def send(self, text: str) -> None: ...

    @abstractmethod
    def press(self, keys: Sequence[KeyPress]) -> None:
        """SessionError without a terminal; ConfigError, nothing sent, if one is untranslatable."""

    @abstractmethod
    def alive(self) -> bool: ...

    @abstractmethod
    def status(self) -> Status: ...

    @abstractmethod
    def content(self) -> str: ...

    @abstractmethod
    def stop(self) -> None:
        """FR-27 target: request owner shutdown; return only after confirmed worker exit."""

    @abstractmethod
    def info(self) -> SessionInfo: ...

    @property
    @abstractmethod
    def reply(self) -> str | None: ...

    @abstractmethod
    def close(self) -> None:
        """FR-27 target: idempotent owner shutdown, SessionError/Unanswered if unconfirmed."""

    @abstractmethod
    def session_status(self) -> SessionStatus: ...

    @abstractmethod
    def record(self) -> Record | None: ...

    @property
    @abstractmethod
    def showable(self) -> bool: ...

    @abstractmethod
    def show(self, *, display: Display = None, command: str | None = None) -> str | None: ...

    @abstractmethod
    def hide(self) -> None: ...

    @abstractmethod
    def attach_command(self) -> str: ...

    @abstractmethod
    def screen(self, lines: int | None = None) -> str: ...

    @classmethod
    def _hosted(cls, method: str) -> Callable[..., Any]:
        if cls is not Session:
            raise NotImplementedError(f"{cls.__name__}.{method}: not implemented")
        return getattr(load_class(HOSTED, Session), method)

    @staticmethod
    def validate_policy(*, flags, env) -> None:
        if ACCOUNT_ENV in ProcessOptions(flags=flags, env=env).env:
            raise ConfigError(ACCOUNT_SET_BY)

    @classmethod
    def configure(cls, *, clients: Mapping, hosts: Hosts) -> None:
        cls._hosted("configure")(clients=clients, hosts=hosts)

    @classmethod
    def configure_from(cls, config: Mapping, *, hosts: Hosts | None = None) -> None:
        cls._hosted("configure_from")(config, hosts=hosts)

    @classmethod
    def preflight(
        cls,
        spec: Spec,
        *,
        display: Display = None,
        host: str | None = None,
        idle_timeout: float | None = None,
    ) -> None:
        """Validate a start without writing state or running the client."""
        cls._hosted("preflight")(spec, display=display, host=host, idle_timeout=idle_timeout)

    @classmethod
    def start(
        cls,
        spec: Spec,
        *,
        display: Display = None,
        host: str | None = None,
        idle_timeout: float | None = None,
    ) -> Session:
        """FR-27 target: create the sole harness owner, restoring handlers on resume."""
        return cls._hosted("start")(spec, display=display, host=host, idle_timeout=idle_timeout)

    @classmethod
    def attach(
        cls,
        name: str,
        *,
        sid: str | None = None,
        host: str | None = None,
        idle_timeout: float | None = None,
    ) -> Session:
        """FR-27 target: a control view of the existing owner, never a second owner."""
        return cls._hosted("attach")(name, sid=sid, host=host, idle_timeout=idle_timeout)

    @classmethod
    def retire(cls, name: str, *, sid: str | None = None, host: str | None = None) -> None:
        """FR-27 target: sid-bound owner close; unconfirmed exit raises, retaining its record."""
        cls._hosted("retire")(name, sid=sid, host=host)

    @classmethod
    def inspect(cls, spec: Spec) -> SessionStatus:
        """Historical status using spec.workdir, client and sid, without starting or attaching.
        A missing record raises SessionError.
        """
        return cls._hosted("inspect")(spec)

    @classmethod
    def list(cls, match: str | None = None, *, display: Display = None) -> list[SessionRow]:
        return cls._hosted("list")(match, display=display)

    @classmethod
    def usage(cls, client: str) -> Usage | None:
        return cls._hosted("usage")(client)

    @classmethod
    def client_resume(cls, client: str, argv: Sequence[str]) -> int:
        return cls._hosted("client_resume")(client, argv)

    @classmethod
    def layout(cls, *, display: Display = None) -> str | None:
        return cls._hosted("layout")(display=display)

    @classmethod
    def writable_dirs(cls, spec: Spec) -> tuple[str, ...]:
        return cls._hosted("writable_dirs")(spec)

    @classmethod
    def display(cls, value: Display = None, *, host: str | None = None) -> Display:
        return cls._hosted("display")(value, host=host)

    @classmethod
    def host(cls, name: str | None = None) -> Host:
        return cls._hosted("host")(name)

    @classmethod
    def class_roots(cls, client: str) -> tuple[str, ...]:
        """The import roots of the classes a `client` session loads by name: its Client's, then
        each configured Host's, without repeats.
        """
        return cls._hosted("class_roots")(client)


def hosted_viewer():
    return load_class(HOSTED, Session).viewer()
