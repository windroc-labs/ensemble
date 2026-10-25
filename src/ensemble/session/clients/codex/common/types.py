"""Codex native session metadata and validated recovery state."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class Snapshot:
    cwd: str
    project: bool
    dirs: tuple[str, ...]
    commands: tuple[str, ...]
    model: str | None
    effort: str | None
    codex_home: str
    flags: tuple[str, ...]  # the caller's Spec.flags; the config's are read at each launch
    env: Mapping[str, str]

    def __post_init__(self):
        object.__setattr__(self, "dirs", tuple(self.dirs))
        object.__setattr__(self, "commands", tuple(self.commands))
        object.__setattr__(self, "flags", tuple(self.flags))
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))


@dataclass(frozen=True)
class NativeMetadata:
    id: str
    cwd: str
    parent_thread_id: str | None


@dataclass(frozen=True)
class SessionFile:
    path: str
    mtime: float
    metadata: NativeMetadata


@dataclass(frozen=True)
class SessionState:
    workdir: str
    sid: str
    snapshot: Snapshot
    writable_roots: tuple[str, ...]
    native_id: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "writable_roots", tuple(self.writable_roots))
