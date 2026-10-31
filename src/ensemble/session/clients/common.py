"""What the Claude, Codex and OpenCode clients share."""

import os
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from ensemble.session.engine.types import ConfigError, Effort, Permission, Tier
from ensemble.utils import DOCKER_CONFIG, environment_ok, read_regular

DOCKER_CONFIG_MAX = 2 * 1024 * 1024


def _names(value, keys) -> bool:
    return isinstance(value, Mapping) and all(
        isinstance(k, str) and k in keys and isinstance(v, str) and v.strip() and v.isprintable()
        for k, v in value.items()
    )


def strings(value) -> bool:
    return isinstance(value, list) and all(isinstance(s, str) for s in value)


def question(titles) -> str:
    """A question dialog's blocked reason: `question: ` and its titles, the empty ones left out."""
    found = [t for t in titles if isinstance(t, str) and t] if isinstance(titles, list) else []
    return f"question: {'; '.join(found)}" if found else "question"


def docker_config(client: str, name: str, default: str) -> tuple[str, str]:
    """(path, text) of the user's Docker config DOCKER_CONFIG/`client`/`name`, else of the packaged
    `default` file; ConfigError: not a regular UTF-8 file of at most DOCKER_CONFIG_MAX bytes.
    """
    path = os.path.expanduser(f"{DOCKER_CONFIG}/{client}/{name}")
    if not os.path.lexists(path):
        path = default
    data = read_regular(path, limit=DOCKER_CONFIG_MAX + 1)
    try:
        if data is None or len(data) > DOCKER_CONFIG_MAX:
            raise ValueError
        return path, data.decode()
    except ValueError:
        raise ConfigError(
            f"Docker config {path}: want a regular UTF-8 file of at most 2 MiB"
        ) from None


FLAGS = ("flags", strings, "a list of strings")
MODES = (
    "modes",
    lambda v: (
        isinstance(v, Mapping) and all(k in tuple(Permission) and strings(f) for k, f in v.items())
    ),
    "a table from auto and yolo to a list of strings",
)

CHECKS = (
    ("tiers", lambda v: _names(v, tuple(Tier)), 'a table from tier "1"–"4" to a model name'),
    (
        "efforts",
        lambda v: _names(v, tuple(Effort)),
        f"a table from effort ({', '.join(Effort)}) to a printable value",
    ),
    ("env", environment_ok, "a table of environment names to strings without NUL"),
)


def mode_flags(config: Mapping, permission: Permission) -> tuple[str, ...]:
    """[clients.<name>.modes]' flags for `permission`; none when absent."""
    return tuple(config.get("modes", {}).get(permission, ()))


def check_config(config: Mapping, table: str, checks=CHECKS) -> None:
    for key, ok, what in checks:
        if key in config and not ok(config[key]):
            raise ConfigError(f"{table}.{key} must be {what}")


@dataclass(frozen=True)
class Flag:
    arg: str
    word: str
    value: str | None
    start: int
    end: int

    @property
    def text(self) -> str:
        return f"{self.arg} {self.value}" if self.end - self.start > 1 else self.arg


def split_flags(flags: Sequence[str]) -> tuple[list[str], list[str]]:
    index = max((flag.end for flag in scan(flags)), default=0)
    return list(flags[:index]), list(flags[index:])


def scan(flags: Sequence[str], valued: Mapping[str, str] = MappingProxyType({})) -> Iterator[Flag]:
    """Scan before '--'; a valued option may consume that separator as its next value."""
    i = 0
    while i < len(flags) and flags[i] != "--":
        start, arg = i, flags[i]
        if arg.startswith("--"):
            word, attached, value = arg.partition("=")
        else:
            word, attached = arg[:2], arg[2:]
            value = attached.removeprefix("=")
        name = valued.get(word)
        if name is None:
            value = None
        elif not attached:
            i += 1
            value = flags[i] if i < len(flags) else ""
        yield Flag(arg, name or word, value, start, i + 1)
        i += 1


UUID = re.compile(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}")


def select(
    native: str,
    syntax: tuple[tuple[str, ...], tuple[str, ...]],
    *,
    resume: bool,
    uuid: bool = False,
) -> tuple[str, ...]:
    if uuid and not UUID.fullmatch(native):
        raise ConfigError(f"session ids are UUIDs, got {native!r:.80}")
    prefix = syntax[int(resume)]
    return (*prefix, native) if prefix else ()


def check_selection(
    flags: tuple[str, ...], forbidden: tuple[str, ...], valued: tuple[str, ...] = ()
) -> None:
    for flag in scan(flags, {word: word for word in valued}):
        if flag.arg in forbidden or flag.word in forbidden:
            raise ConfigError(f"{flag.arg}: picks the session; use sid= or resume=")
