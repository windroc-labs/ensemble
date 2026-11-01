"""Session's own class methods are HostedSession's."""

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
)
from ensemble.session.hosts.base import Host
from ensemble.utils import ACCOUNT_ENV, Display, Record, Status, load_class

HOSTED = "ensemble.session.engine.hosted_session:HostedSession"


class Session(ABC):
    """show, hide, attach_command, screen: NotShowable unless its Host is attachable.
    alive: False while unknown.
    """

    sid: str
    client: str
    mode: Mode
    name: str | None

    @abstractmethod
    def events(self) -> Iterator[SessionEvent | Stderr | IdleTimeout]:
        """Leaving the iterator never ends supervision."""

    @abstractmethod
    def send(self, text: str) -> None: ...

    @abstractmethod
    def press(self, keys: Sequence[KeyPress]) -> None:
        """SessionError: no terminal; ConfigError, nothing sent: an untranslatable key."""

    @abstractmethod
    def alive(self) -> bool: ...

    @abstractmethod
    def status(self) -> Status: ...

    @abstractmethod
    def content(self) -> str: ...

    @abstractmethod
    def stop(self) -> None:
        """Returns after the worker's confirmed exit."""

    @abstractmethod
    def info(self) -> SessionInfo: ...

    @property
    @abstractmethod
    def reply(self) -> str | None: ...

    @abstractmethod
    def close(self) -> None:
        """Idempotent; SessionError/Unanswered if unconfirmed."""

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
        """Writes no state, runs no client."""
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
        """The sole harness owner; resume restores handlers."""
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
        """A control view, never a second owner."""
        return cls._hosted("attach")(name, sid=sid, host=host, idle_timeout=idle_timeout)

    @classmethod
    def retire(cls, name: str, *, sid: str | None = None, host: str | None = None) -> None:
        """Sid-bound; an unconfirmed exit raises, keeping its record."""
        cls._hosted("retire")(name, sid=sid, host=host)

    @classmethod
    def inspect(cls, spec: Spec) -> SessionStatus:
        """Recorded status of spec's workdir, client, sid; SessionError: no record."""
        return cls._hosted("inspect")(spec)

    @classmethod
    def list(cls, match: str | None = None, *, display: Display = None) -> list[SessionRow]:
        return cls._hosted("list")(match, display=display)

    @classmethod
    def client_resume(cls, client: str, argv: Sequence[str]) -> int:
        return cls._hosted("client_resume")(client, argv)

    @classmethod
    def resume_argv(cls, client: str, native: str) -> tuple[str, ...]:
        return cls._hosted("resume_argv")(client, native)

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
        """Its Client's, then each Host's import roots, once each."""
        return cls._hosted("class_roots")(client)


def hosted_viewer():
    return load_class(HOSTED, Session).viewer()
