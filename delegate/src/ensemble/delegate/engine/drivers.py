"""Driver delivers an instruction or starts a Session through its injected class."""

import os
from abc import ABC, abstractmethod
from collections.abc import Mapping

from ensemble.delegate import config
from ensemble.delegate.engine.config import read_config
from ensemble.delegate.engine.container import on_docker
from ensemble.delegate.engine.types import ConfigError, DriverSpec, Instruction
from ensemble.session import ConfigError as SessionConfigError
from ensemble.session import Mode, Session, Spec
from ensemble.utils import Display, save


class Driver(ABC):
    session_based = True
    docker = False
    session: Session | None = None
    display: Display = None

    def policy(self, client: str) -> tuple[tuple[str, ...], dict[str, str]]:
        return (), {}

    def writable_dirs(self, spec: Spec) -> tuple[str, ...]:
        return ()

    def preflight(self, spec: Spec) -> None:
        return None

    @abstractmethod
    def start(
        self,
        spec: DriverSpec,
        *,
        display: Display = None,
        host: str | None = None,
        idle_timeout: float | None = None,
    ) -> Session | Instruction: ...


class AgentDriver(Driver):
    def __init__(
        self,
        session_cls: type[Session],
        *,
        settings: Mapping | None = None,
        display: Display = None,
        host: str | None = None,
    ):
        self.session_cls = session_cls
        self.settings = settings
        self.display, self.host = display, host
        self.docker = settings is not None and on_docker(settings, host)

    def policy(self, client: str) -> tuple[tuple[str, ...], dict[str, str]]:
        try:
            flags, env = config.policy(
                client, read_config() if self.settings is None else self.settings
            )
            self.session_cls.validate_policy(flags=tuple(flags), env=env)
            return tuple(flags), dict(env)
        except (OSError, ValueError, SessionConfigError) as error:
            raise ConfigError(f"policy.{client}: {error}") from None

    def preflight(self, spec: Spec) -> None:
        self.session_cls.preflight(
            spec, display=self.display if spec.mode == Mode.TUI else None, host=self.host
        )

    def start(
        self,
        spec: DriverSpec,
        *,
        display: Display = None,
        host: str | None = None,
        idle_timeout: float | None = None,
    ) -> Session:
        placement = self.display if spec.session.mode == Mode.TUI else None
        self.session = self.session_cls.start(
            spec.session,
            display=placement if display is None else display,
            host=self.host if host is None else host,
            idle_timeout=idle_timeout,
        )
        return self.session

    def writable_dirs(self, spec: Spec) -> tuple[str, ...]:
        return self.session_cls.writable_dirs(spec)


class InstructionDriver(Driver):
    session_based = False

    def start(
        self,
        spec: DriverSpec,
        *,
        display: Display = None,
        host: str | None = None,
        idle_timeout: float | None = None,
    ) -> Instruction:
        session = spec.session
        if session.resume:
            raise ConfigError("instruction has no session to resume")
        if host is not None or display is not None:
            raise ConfigError("instruction has no host or display")
        if idle_timeout is not None:
            raise ConfigError("instruction has no idle timeout")
        path = os.path.abspath(spec.out)
        save(path, session.prompt)
        return Instruction(path)
