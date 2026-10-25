"""The Team: the roles, tasks and templates a run can name, from the team root and the team dirs;
each role's task index; the checks that keep a team dir out of reach of the agent run.
"""

import os
import re
from collections.abc import Mapping, Sequence
from enum import StrEnum

from ensemble.delegate.composer.base import METHOD, PLACEHOLDER, TEAM, read_text, title_of
from ensemble.delegate.composer.types import ConfigError, Team
from ensemble.delegate.config import CONFIG, validate_team_dirs
from ensemble.delegate.engine.config import read_config
from ensemble.delegate.event import PROGRESS
from ensemble.utils import LOCAL, under, unfenced
from ensemble.utils import load_config as load

START = re.compile(rf"^\s*\[{PROGRESS}:start\] \S", re.M)
TEAM_NAME = re.compile(r"[a-z0-9-]+")
INDEX = re.compile(r"^## Tasks\n(.*?)(?=^#|\Z)", re.M | re.S)
INDEX_LINE = re.compile(r"^- `([\w-]+)`(?: \(`([^`]*)`\))?: (.+)$", re.M)
INDEX_DEFAULT = re.compile(r"unsure → `([\w-]+)` \(`([^`]*)`\)")


class TeamKind(StrEnum):
    ROLES = "roles"
    TASKS = "tasks"
    TEMPLATES = "templates"


def lookup(team: Team, kind: str, name: str) -> str | None:
    paths = team.files.get((kind, name), ())
    if len(paths) > 1:
        raise ConfigError(
            f"{paths[1]}: {kind[:-1]} {name!r} is also defined by {paths[0]}; team dir files only add"
        )
    if paths and team.custom(paths[0]):
        check_file(team, kind, paths[0])
    return paths[0] if paths else None


def path_for(team: Team, kind: str, name: str) -> str:
    if path := lookup(team, kind, name):
        return path
    raise misnamed_error(team, kind, name) or ConfigError(f"missing file {kind}/{name}.md")


def read(team: Team, kind: str, name: str) -> tuple[str, str]:
    path = path_for(team, kind, name)
    return read_text(path), team.label(kind, name, path)


def check_file(team: Team, kind: str, path: str) -> None:
    text = read_text(path)
    if text.split("\n", 1)[0].strip() == "---":
        raise ConfigError(
            f"{path}: front matter (---) is not read: team dir files hold text only; run keys go in {LOCAL}"
        )
    if m := PLACEHOLDER.search(text):
        raise ConfigError(f"{path}: unfilled placeholder {m[0]}")
    for m in METHOD.findall(text):
        if not os.path.isfile(os.path.join(team.root, "methods", f"{m}.md")):
            raise ConfigError(f"{path}: names <team>/methods/{m}.md, which core has no file for")
    if kind == "roles":
        title_of(text, path)
    elif kind == "tasks":
        if not text.startswith("# "):
            raise ConfigError(f"{path}: must start with a '# ' heading")
        if (n := len(START.findall(text))) != 1:
            raise ConfigError(f"{path}: has {n} [{PROGRESS}:start] lines, not 1")
        if not any(line.rstrip() == "## Resume" for _, line in unfenced(text)):
            raise ConfigError(f"{path}: has no ## Resume section")


def charter(team: Team, role: str) -> tuple[str, str] | None:
    if not (path := lookup(team, "roles", role)):
        return None
    return read_text(path), team.label("roles", role, path)


def tasks_dir(team: Team, role: str) -> str | None:
    path = lookup(team, "roles", role)
    return (
        os.path.join(os.path.dirname(os.path.dirname(path)), "tasks")
        if path and team.custom(path)
        else None
    )


def role_team(team: Team, role: str) -> str:
    """The team root holding the role's charter: its team dir, else the team root."""
    path = lookup(team, "roles", role)
    return os.path.dirname(os.path.dirname(path)) if path and team.custom(path) else team.root


def used(team: Team, role: str, templates: Sequence[str] = ()) -> list[str]:
    paths = [lookup(team, "roles", role)]
    paths += [lookup(team, "tasks", t) for t in index(role, team)]
    paths += [lookup(team, "templates", name) for name in templates]
    return list(dict.fromkeys(p for p in paths if p and team.custom(p)))


def misnamed_error(
    team: Team, kind: str | None = None, name: str | None = None
) -> ConfigError | None:
    for path in team.misnamed:
        stem = os.path.basename(path).removesuffix(".md")
        if kind is None or (os.path.basename(os.path.dirname(path)), stem) == (kind, name):
            return ConfigError(f"{path}: name {stem!r}: want {TEAM_NAME.pattern}")
    return None


def check_all(team: Team, cfg: Mapping) -> None:
    if e := misnamed_error(team):
        raise e
    for kind, name in team.files:
        lookup(team, kind, name)
    roles(team, cfg)


def team_dirs(config: str = CONFIG, local: str = LOCAL) -> tuple[str, ...]:
    try:
        own = load(config)
    except (OSError, ValueError) as error:
        raise ConfigError(f"config: {error}") from None
    try:
        validate_team_dirs(own, config, local=local)
    except ValueError as error:
        raise ConfigError(str(error)) from None
    try:
        value = read_config(path=config, local=local).get("team_dirs")
    except (OSError, ValueError) as error:
        raise ConfigError(f"config: {error}") from None
    return configured_dirs(value)


def configured_dirs(value) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError(f"team_dirs: want a list of absolute or ~ paths, got {value!r:.80}")
    dirs = []
    for p in value:
        if (
            not isinstance(p, str)
            or not p.startswith(("/", "~"))
            or not os.path.isabs(os.path.expanduser(p))
        ):
            raise ConfigError(f"team_dirs: {p!r:.80} is not an absolute or ~ path")
        real = os.path.realpath(os.path.expanduser(p))
        if not os.path.isdir(real):
            raise ConfigError(f"team_dirs: {p} is not a directory")
        if real in dirs:
            raise ConfigError(f"team_dirs: {p} is listed twice")
        dirs.append(real)
    return tuple(dirs)


def team(root: str = TEAM, dirs: Sequence[str] = ()) -> Team:
    files, misnamed = {}, []
    for base in (root, *dirs):
        for kind in TeamKind:
            sub = os.path.join(base, kind)
            try:
                names = sorted(os.listdir(sub)) if os.path.isdir(sub) else []
            except OSError as e:
                raise ConfigError(f"{sub}: {e.strerror}") from None
            for name in names:
                path, stem = os.path.join(sub, name), name.removesuffix(".md")
                if not name.endswith(".md") or not os.path.isfile(path):
                    continue
                if base != root and not TEAM_NAME.fullmatch(stem):
                    misnamed.append(path)
                else:
                    files.setdefault((kind, stem), []).append(path)
    return Team(
        root=root,
        dirs=tuple(dirs),
        files={k: tuple(v) for k, v in files.items()},
        misnamed=tuple(misnamed),
    )


def tasks_path(role: str, team: Team) -> str:
    return tasks_dir(team, role) or os.path.join(team.root, "tasks")


def roles(team: Team, cfg: Mapping) -> list[str]:
    for role in cfg.get("roles", {}):
        if not lookup(team, "roles", role):
            raise no_charter(role)
    charters = {name: paths for (kind, name), paths in team.files.items() if kind == "roles"}
    core = [name for name, paths in charters.items() if not team.custom(paths[0])]
    out = [r for r in cfg.get("roles", {}) if r in core] + sorted(
        r for r in core if r not in cfg.get("roles", {})
    )
    for d in team.dirs:
        out += sorted(
            name
            for name, paths in charters.items()
            if name not in out and any(os.path.dirname(os.path.dirname(p)) == d for p in paths)
        )
    return out


def index(
    role: str,
    team: Team | None = None,
    config: str = CONFIG,
    local: str = LOCAL,
    *,
    cfg: Mapping | None = None,
) -> dict[str, str]:
    team = team_or_default(team)
    if not (found := charter(team, role)):
        raise no_charter(role, config_of(config, local, cfg), team)
    text, rel = found
    m = INDEX.search(text)
    section = m.group(1).strip() if m else ""
    if not (lines := INDEX_LINE.findall(section)):
        raise ConfigError(
            f"{rel}: no task index (a ## Tasks section of - `<task>` (`<tasks>/<task>.md`): … lines)"
        )
    tasks = {t: what for t, _, what in lines}
    if not (default := INDEX_DEFAULT.search(section.split("\n", 1)[0])):
        raise ConfigError(
            f"{rel}: ## Tasks opens without its default (unsure → `<task>` (`<tasks>/<task>.md`))"
        )
    if default.group(1) not in tasks:
        raise ConfigError(f"{rel}: the default {default.group(1)!r} is not a listed task")
    for t, path in [default.groups(), *((t, path) for t, path, _ in lines)]:
        if path != f"<tasks>/{t}.md":
            raise ConfigError(f"{rel}: {t!r} has path {path!r}, not '<tasks>/{t}.md'")
    own = tasks_path(role, team)
    for t in tasks:
        if not (path := lookup(team, "tasks", t)):
            raise misnamed_error(team, "tasks", t) or ConfigError(
                f"{rel}: lists {t!r} without tasks/{t}.md"
            )
        if os.path.dirname(path) != own:
            raise ConfigError(
                f"{rel}: lists {t!r}, whose file {team.label('tasks', t, path)} is not in {tasks_dir(team, role) or 'core team/tasks'}: a role runs only its own tasks"
            )
    return tasks


def config_of(config: str, local: str, cfg: Mapping | None = None) -> Mapping:
    """`cfg`, the caller's already-read delegate config, else `config` with `local` read."""
    if cfg is not None:
        return cfg
    try:
        return read_config(path=config, local=local)
    except (OSError, ValueError) as e:
        raise ConfigError(f"config: {e}") from None


def role_tables(config: str, local: str, cfg: Mapping | None = None) -> Mapping:
    if cfg is None and not os.path.isfile(config):
        raise ConfigError(f"no core config {config}")
    roles = config_of(config, local, cfg).get("roles")
    if not isinstance(roles, Mapping):
        raise ConfigError(f"malformed [roles] table in {config}")
    for role, table in roles.items():
        if not isinstance(role, str) or not isinstance(table, Mapping):
            raise ConfigError(f"malformed [roles.{role}] table in {config}")
    return roles


def team_or_default(given: Team | None) -> Team:
    return given if given is not None else team()


def no_charter(role: str, cfg: Mapping | None = None, team: Team | None = None) -> ConfigError:
    if team is not None and (e := misnamed_error(team, "roles", role)):
        return e
    if cfg is not None and role not in cfg.get("roles", {}):
        return ConfigError(f"unknown role {role!r}")
    return ConfigError(f"roles.{role}: no charter roles/{role}.md in core or team_dirs")


def check_team_dirs(dirs: Sequence[str], *, writes: Sequence[tuple[str, str]]) -> None:
    for d in map(os.path.realpath, dirs):
        for path in (*team_paths(d), *tool_paths(d)):
            real = os.path.realpath(path)
            check_overlap(path if real == path else f"{path} ({real})", real, writes)


def tool_paths(d: str) -> list[str]:
    """Each path of d's tools/ tree, a linked dir followed once by its real path."""
    paths, seen, todo = [], set(), [os.path.join(d, "tools")]
    while todo:
        path = todo.pop()
        if not os.path.lexists(path) or (real := os.path.realpath(path)) in seen:
            continue
        seen.add(real)
        paths.append(path)
        if os.path.isdir(path):
            try:
                names = sorted(os.listdir(path), reverse=True)
            except OSError as e:
                raise ConfigError(f"{path}: {e.strerror}") from None
            todo += [os.path.join(path, n) for n in names]
    return paths


def team_paths(d: str) -> list[str]:
    paths = [d]
    for kind in TeamKind:
        if os.path.isdir(sub := os.path.join(d, kind)):
            try:
                names = sorted(os.listdir(sub))
            except OSError as e:
                raise ConfigError(f"{sub}: {e.strerror}") from None
            paths += [sub, *(os.path.join(sub, n) for n in names if n.endswith(".md"))]
    return paths


def check_overlap(shown: str, real: str, writes: Sequence[tuple[str, str]]) -> None:
    for what, p in writes:
        p = os.path.realpath(p)
        if under(real, [p]) or under(p, [real]):
            raise ConfigError(
                f"team_dirs: {shown} overlaps {what} {p}: the agent run can write there"
            )
