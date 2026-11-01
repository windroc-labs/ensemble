"""The Client interface: a client factory from [clients.<name>] minus `implementation`;
headless(), tui() and in_process() each give a fresh mode object, None if unsupported, implementing
its Mode's contract in modes/client_adapters/. Each mode reads what to run from the Spec alone. Starts no
process.
"""

import re
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import ClassVar

from ensemble.session.clients.common import select
from ensemble.session.engine.types import (
    DEFAULT_MAX_USAGE,
    ConfigError,
    Launch,
    ProviderProbe,
    Resumable,
    SessionInfo,
    SessionStatus,
    Spec,
    Usage,
)
from ensemble.session.modes.client_adapters.headless import Headless
from ensemble.session.modes.client_adapters.in_process import InProcess
from ensemble.session.modes.client_adapters.tui import Tui
from ensemble.utils import thaw

NATIVE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*")


class Client:
    selection: tuple[tuple[str, ...], tuple[str, ...]] = ((), ())
    forbidden_selection: tuple[str, ...] = ()
    valued_flags: tuple[str, ...] = ()
    selection_uuid: bool = False
    keys: frozenset[str] = frozenset()
    client_note: str = ""
    session_env: ClassVar[str] = ""
    identity_env: ClassVar[tuple[str, ...]] = ()
    child_defaults: ClassVar[Mapping[str, str]] = MappingProxyType({})

    def __init__(self, config: Mapping):
        if extra := sorted(set(config) - self.keys):
            key = extra[0]
            client = type(self).__name__
            raise ConfigError(f"unknown key {key!r} in {client}'s config")
        self.config = thaw(config)

    def select_session(self, native: str, *, resume: bool) -> tuple[str, ...]:
        return select(native, self.selection, uuid=self.selection_uuid, resume=resume)

    def resume_argv(self, native: str) -> tuple[str, ...]:
        if not NATIVE.fullmatch(native):
            raise ConfigError(f"session id {native!r:.80}: want {NATIVE.pattern}")
        executable, selection = self._executable(), self.select_session(native, resume=True)
        if not executable or not selection:
            raise ConfigError("this client has no interactive resume")
        return (executable, *selection)

    def headless(self) -> Headless | None:
        return None

    def tui(self) -> Tui | None:
        return None

    def in_process(self) -> InProcess | None:
        return None

    def usage_probe(self) -> Launch | None:
        return None

    def _executable(self, *, container: bool = False) -> str:
        return ""

    def _provider_probe(self) -> ProviderProbe | None:
        return None

    def _accounts(self) -> frozenset[str] | None:
        """The registered account names a session may select; None: the client has no accounts."""
        return None

    def _account(self) -> str:
        return ""

    def _account_env(self, account: str) -> Mapping[str, str]:
        """The env a run on registered `account` takes over every other source."""
        return {}

    def _account_of(self, transcript: str) -> str:
        """The registered account whose dir holds `transcript`; "": none."""
        return ""

    def _max_usage(self, account: str) -> float:
        return DEFAULT_MAX_USAGE

    def _usage_probes(self) -> dict[str, Launch] | None:
        return None

    def usage(self, output: str) -> Usage | None:
        """None only when the probe failed."""
        return None

    def session_metadata(self, info: SessionInfo) -> SessionInfo:
        return info

    def resume_main(self, argv: Sequence[str]) -> int:
        """Runs its public resume command's arguments (SessionInfo.resume); returns on failure."""
        raise ConfigError("this client has no resume command")

    def session_status(
        self, info: SessionInfo, *, env: Mapping[str, str] = MappingProxyType({})
    ) -> SessionStatus:
        return SessionStatus(Resumable.UNKNOWN, None, "")

    def writable_dirs(self, spec: Spec) -> tuple[str, ...]:
        """Own write dirs (spec.resume: spec.sid's)."""
        return ()
