"""The Composer's shared types: ConfigError (the whole delegate's), a role's run config, one run's
params, its Team and a run's access intent.
"""

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ensemble.session import Effort
from ensemble.utils import freeze

TMP = "tmp"  # the agent dir's temp files
DELIVERABLE = os.path.join(TMP, "deliverable.md")  # under the agent dir, without `out`


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class DelegateConfig:
    values: Mapping

    def __post_init__(self):
        object.__setattr__(self, "values", freeze(self.values))


@dataclass(frozen=True, kw_only=True)
class RunConfig:
    role: str
    task: str = ""
    tier: int
    effort: Effort
    client: str = ""  # opaque: never compared with a Client's name
    output: dict
    read: list[str] = field(default_factory=list)
    write: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    templates: list[str] = field(default_factory=list)
    gate: str = ""
    language: str = ""
    cwd: str = ""
    role_title: str = ""
    idle_timeout: float
    show: str

    def __post_init__(self):
        try:
            seconds = (
                not isinstance(self.idle_timeout, bool)
                and isinstance(self.idle_timeout, (int, float))
                and math.isfinite(self.idle_timeout)
                and self.idle_timeout > 0
            )
        except OverflowError:
            seconds = False
        if not seconds:
            raise ConfigError(
                f"idle_timeout must be a positive finite number of seconds, got {self.idle_timeout!r}"
            )
        if type(self.tier) is not int or not 1 <= self.tier <= 4:
            raise ConfigError(f"tier must be an integer 1–4, got {self.tier!r}")
        try:
            object.__setattr__(self, "effort", Effort(self.effort))
        except ValueError:
            raise ConfigError(
                f"effort must be one of {', '.join(Effort)}, got {self.effort!r}"
            ) from None
        if (
            not isinstance(self.client, str)
            or not self.client.strip()
            or not self.client.isprintable()
        ):
            raise ConfigError(f"client must be a non-empty one-line name, got {self.client!r:.80}")
        if "type" not in self.output:
            raise ConfigError("output.type is not set")
        if not isinstance(self.gate, str) or "\n" in self.gate or "`" in self.gate:
            raise ConfigError("gate must be one line of shell command without backticks")
        if not isinstance(self.show, str) or not self.show.isprintable():
            raise ConfigError(f"show must be one line of shell command, got {self.show!r:.80}")
        if not isinstance(self.language, str) or "\n" in self.language:
            raise ConfigError("language must be one line of text")
        if self.cwd and (
            not isinstance(self.cwd, str)
            or not self.cwd.isprintable()
            or not os.path.isabs(os.path.expanduser(self.cwd))
        ):
            raise ConfigError(f"cwd must be a printable absolute or ~ path, got {self.cwd!r:.80}")
        for key in ("read", "write", "commands", "templates"):
            value = getattr(self, key)
            if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
                raise ConfigError(f"{key} must be a list of strings, got {value!r:.80}")


@dataclass(frozen=True, kw_only=True)
class RunParams:
    input: str
    out: str
    workdir: str
    resume: bool = False
    docker: bool = False

    @property
    def deliverable(self) -> str:
        out = os.path.abspath(self.out)
        if Path(os.path.realpath(out)).is_relative_to(os.path.realpath(self.workdir)):
            return out
        return os.path.join(os.path.abspath(self.workdir), DELIVERABLE)


@dataclass(frozen=True)
class Team:
    """The roles, tasks and templates a run can name: the team root's and the team dirs' files
    (teams.team).
    """

    root: str
    dirs: tuple[str, ...] = ()
    files: Mapping[tuple[str, str], tuple[str, ...]] = field(default_factory=dict)
    misnamed: tuple[str, ...] = ()

    def custom(self, path: str) -> bool:
        return not Path(path).is_relative_to(self.root)

    def label(self, kind: str, name: str, path: str) -> str:
        return path if self.custom(path) else f"{kind}/{name}.md"

    def source(self, path: str) -> str:
        return path if self.custom(path) else "built-in"


@dataclass(frozen=True)
class ApprovedCommand:
    """A requested command as the team text writes it (text: its names, no trailing ` *`) and as it
    is approved (rule: each word holding a placeholder filled and shell-quoted); quoted when the
    quoting changed a word, the prompt then writing the rule in place of the text.
    """

    text: str
    rule: str
    quoted: bool


@dataclass(frozen=True)
class Intent:
    dirs: tuple[str, ...]
    reads: tuple[str, ...]
    writes: tuple[str, ...]
    commands: tuple[str, ...]
    gate: str
    env: Mapping[str, str]
    cwd: str
    project: bool
