"""A role's run config (resolve: the core config, then the caller's overlays) and accounts table, one
run's params and its client-neutral access intent (access), whose env is the session env its
commands need.
An overlay can widen read, write and commands, so a caller passes only operator-controlled
config as one. A runtime overlay may also supply the caller's gate command; config files cannot.
"""

import os
import shlex
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace

import ensemble.session as session
import ensemble.utils as utils
from ensemble.delegate import config as delegate_config
from ensemble.delegate.composer.base import fill, title_of
from ensemble.delegate.composer.teams import (
    charter,
    config_of,
    index,
    misnamed_error,
    no_charter,
    role_tables,
    role_team,
    tasks_path,
    team_or_default,
)
from ensemble.delegate.composer.types import ApprovedCommand, ConfigError, Intent, RunConfig, Team
from ensemble.delegate.config import CONFIG
from ensemble.utils import LOCAL

RUN_KEYS = frozenset(
    {
        "tier",
        "effort",
        "client",
        "read",
        "write",
        "commands",
        "templates",
        "output",
        "language",
        "cwd",
        "idle_timeout",
        "show",
    }
)
OVERLAY_KEYS = RUN_KEYS | {"gate"}
ACCOUNTS_KEY = "accounts"
ENVIRONMENT_KEY = "environment"  # a config role table's; engine/config.py checks it
OUTPUT_KEYS = frozenset({"type", "repo", "branch", "dir", "host"})
OLD_KEYS = frozenset({"default_task", "tasks"})
NAMES = {"workdir": "<Workdir>", "team": "<team>", "role_team": "<role team>"}
IMAGE_TEAM = "/opt/ensemble/team"


def run_key(layer: Mapping, role: str, key: str):
    for table in (layer.get("roles", {}).get(role, {}), layer):
        if key in table:
            return table[key]
    return None


def check_old_keys(table: Mapping, role: str, prefix: str = "") -> None:
    if old := sorted(OLD_KEYS & set(table)):
        raise ConfigError(
            f"[{prefix}roles.{role}] has {old[0]!r}: task-level config and default_task are gone; set run keys in [{prefix}roles.{role}] (a run without a task picks one)"
        )


def resolve(
    role: str,
    task: str | None = None,
    *,
    overlays: Sequence[Mapping] = (),
    label: tuple[str, str] = ("run overlay", "overlay"),
    team: Team | None = None,
    config: str = CONFIG,
    local: str = LOCAL,
    cfg: Mapping | None = None,
) -> RunConfig:
    overlays, team = tuple(overlays), team_or_default(team)
    known = set(role_tables(config, local, cfg)) | {n for kind, n in team.files if kind == "roles"}
    if role not in known:
        raise no_charter(role, {}, team)
    source, where = label
    cfg = config_of(config, local, cfg)
    for overlay in overlays:
        try:
            _check_overlay(overlay, known, where)
        except ConfigError as e:
            raise ConfigError(f"{source}: {e}") from None
    _check(cfg)
    values = _run_keys(cfg, role)
    run = RunConfig(
        role=role,
        task=task or "",
        tier=values.pop("tier", None),
        effort=values.pop("effort", None),
        idle_timeout=values.pop("idle_timeout", None),
        show=values.pop("show", None),
        output=values.pop("output", {}),
        **values,
    )
    for overlay in overlays:
        values = _run_keys(overlay, role, OVERLAY_KEYS)
        run = replace(run, **values)
    if not (found := charter(team, role)):
        raise no_charter(role, cfg, team)
    tasks = index(role, team, config, local, cfg=cfg)
    if task and task not in tasks:
        raise misnamed_error(team, "tasks", task) or ConfigError(
            f"task {task!r} is not one of {role}'s tasks ({', '.join(tasks)})"
        )
    return replace(run, role_title=title_of(*found))


def accounts(
    role: str,
    *,
    overlays: Sequence[Mapping] = (),
    label: tuple[str, str] = ("run overlay", "overlay"),
    config: str = CONFIG,
    local: str = LOCAL,
    cfg: Mapping | None = None,
) -> dict[str, str]:
    """`role`'s accounts (client name: account name) from the layers resolve takes, entry by entry,
    each layer's global table then its role's; an entry "" is unset. Run after resolve accepted the
    same layers.
    """
    source, where = label
    out: dict[str, str] = {}
    for layer, prefix in (
        (config_of(config, local, cfg), ""),
        *((o, f"{where}.") for o in overlays),
    ):
        try:
            out |= _accounts(layer, prefix)
            out |= _accounts(layer.get("roles", {}).get(role, {}), f"{prefix}roles.{role}.")
        except ConfigError as e:
            raise ConfigError(f"{source}: {e}" if prefix else str(e)) from None
    return {client: account for client, account in out.items() if account}


def trusted_dirs(
    config: str = CONFIG, local: str = LOCAL, *, cfg: Mapping | None = None
) -> frozenset[str]:
    try:
        return delegate_config.trusted_dirs(config_of(config, local, cfg))
    except ValueError as e:
        raise ConfigError(str(e)) from None


def _in_image(path: str) -> bool:
    return os.path.commonpath([path, IMAGE_TEAM]) == IMAGE_TEAM


def bind(entry: str, repo_dir: str | None) -> str | None:
    if entry == "repo":
        return os.path.abspath(repo_dir) if repo_dir else None
    return os.path.abspath(os.path.expanduser(entry))


def team_paths(run: RunConfig, team: Team, *, docker: bool = False) -> dict[str, str]:
    """{team, role_team, tasks}: on the host, else in the image, which holds only the core team."""
    if docker:
        return {"team": IMAGE_TEAM, "role_team": IMAGE_TEAM, "tasks": f"{IMAGE_TEAM}/tasks"}
    return {
        "team": team.root,
        "role_team": role_team(team, run.role),
        "tasks": tasks_path(run.role, team),
    }


def access(
    run: RunConfig,
    workdir: str,
    *,
    cwd: str,
    project: bool,
    repo_dir: str | None = None,
    team: Team | None = None,
    docker: bool = False,
) -> Intent:
    """`docker`: the image's team paths, no env, and any other read or a write refused."""
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        raise ConfigError(f"cwd must be an absolute path, got {cwd!r:.80}")
    team, workdir = team_or_default(team), os.path.abspath(workdir)
    roots = team_paths(run, team, docker=docker)
    paths = {"read": [], "write": []}
    for key, entries in (("read", run.read), ("write", run.write)):
        for entry in entries:
            p = bind(fill(entry, roots, key), repo_dir)
            if docker and (key == "write" or not p or not _in_image(p)):
                raise ConfigError(
                    f"{key} {entry}: a Docker run reads only the image's team and writes only its agent dir"
                )
            if p and p not in paths[key]:
                paths[key].append(p)
    dirs = [workdir] if os.path.realpath(cwd) != os.path.realpath(workdir) else []
    dirs += [p for p in paths["write"] if p not in dirs]
    seen, reads = set(map(os.path.realpath, dirs)), []
    for p in (roots["tasks"], *paths["read"]):
        if (real := os.path.realpath(p)) not in seen:
            seen.add(real)
            reads.append(p)
    commands = tuple(c.rule for c in approved(run, workdir, team, docker=docker))
    needs_env = (commands or run.gate) and not docker
    return Intent(
        dirs=tuple(dirs),
        reads=tuple(reads),
        writes=tuple(paths["write"]),
        commands=commands,
        gate=run.gate,
        env=utils.import_env(delegate_config, session, utils) if needs_env else {},
        cwd=cwd,
        project=project,
    )


def approved(
    run: RunConfig, workdir: str, team: Team, *, docker: bool = False
) -> tuple[ApprovedCommand, ...]:
    """Each requested command, its words holding a placeholder shell-quoted once filled, so a path
    with a space stays one argument; the prompt writes a quoted one as approved.
    """
    values = {"workdir": os.path.abspath(workdir)} | team_paths(run, team, docker=docker)
    out = []
    for command in run.commands:
        words = [(word, fill(word, values, "commands")) for word in command.split(" ")]
        quoted = [shlex.quote(new) if new != word else word for word, new in words]
        out.append(
            ApprovedCommand(
                text=fill(command, NAMES, "commands").removesuffix(" *"),
                rule=" ".join(quoted),
                quoted=quoted != [new for _, new in words],
            )
        )
    return tuple(out)


def _check(cfg: Mapping) -> None:
    if "gate" in cfg:
        raise ConfigError("unknown key 'gate'")
    for key in cfg:
        if hint := utils.renamed_key(key):
            raise ConfigError(f"unknown key {key!r}{hint}")
    _check_output(cfg, "output", str)
    _accounts(cfg, "")
    for r, table in cfg.get("roles", {}).items():
        _check_role(table, r, "", str, RUN_KEYS | {ENVIRONMENT_KEY})


def _check_overlay(overlay: Mapping, roles: set[str], where: str) -> None:
    """An overlay's errors name its tables in brackets."""
    if not isinstance(overlay, Mapping):
        raise ConfigError(f"[{where}] must be a table")
    for key in (ENVIRONMENT_KEY, "environments"):
        if key in overlay:
            raise _untrusted(f"{where}.{key}")
    _check_keys(overlay, OVERLAY_KEYS | {"roles", ACCOUNTS_KEY}, f"[{where}]")
    _check_output(overlay, f"{where}.output", _boxed)
    _accounts(overlay, f"{where}.")
    if "roles" not in overlay:
        return
    if not isinstance(overlay["roles"], Mapping):
        raise ConfigError(f"[{where}.roles] must be a table")
    for role, table in overlay["roles"].items():
        if role not in roles:
            raise ConfigError(f"unknown role {role!r} in [{where}.roles]")
        if not isinstance(table, Mapping):
            raise ConfigError(f"[{where}.roles.{role}] must be a table")
        if ENVIRONMENT_KEY in table:
            raise _untrusted(f"{where}.roles.{role}.{ENVIRONMENT_KEY}")
        _check_role(table, role, f"{where}.", _boxed, OVERLAY_KEYS)


def _untrusted(where: str) -> ConfigError:
    return ConfigError(
        f"[{where}]: environments come only from the delegate config files "
        "(config.toml, config.local.toml)"
    )


def _boxed(where: str) -> str:
    return f"[{where}]"


def _check_role(
    table: Mapping, role: str, prefix: str, shown: Callable[[str], str], keys: frozenset = RUN_KEYS
) -> None:
    check_old_keys(table, role, prefix)
    _check_keys(table, keys | {ACCOUNTS_KEY}, shown(f"{prefix}roles.{role}"))
    _check_output(table, f"{prefix}roles.{role}.output", shown)
    _accounts(table, f"{prefix}roles.{role}.")


def _accounts(table: Mapping, prefix: str) -> Mapping[str, str]:
    """`table`'s accounts table, `[<prefix>accounts]` in its errors; absent: empty."""
    found = table.get(ACCOUNTS_KEY, {})
    where = f"[{prefix}{ACCOUNTS_KEY}]"
    if not isinstance(found, Mapping):
        raise ConfigError(f"{where} must be a table")
    if bad := next((k for k, v in found.items() if not isinstance(v, str)), None):
        raise ConfigError(f"{where} {bad} must be text")
    return found


def _check_output(table: Mapping, where: str, shown: Callable[[str], str]) -> None:
    if "output" not in table:
        return
    if not isinstance(table["output"], Mapping):
        raise ConfigError(f"{shown(where)} must be a table")
    _check_keys(table["output"], OUTPUT_KEYS, shown(where))
    if bad := next((k for k, v in table["output"].items() if not isinstance(v, str)), None):
        raise ConfigError(f"{shown(f'{where}.{bad}')} must be text")


def _check_keys(table: Mapping, allowed: frozenset, where: str) -> None:
    if extra := sorted(set(table) - allowed, key=str):
        raise ConfigError(f"unknown key {extra[0]!r} in {where}{utils.renamed_key(extra[0])}")


def _run_keys(layer: Mapping, role: str, keys: frozenset = RUN_KEYS) -> dict:
    """A layer's run keys for `role`; an [output] key "" is unset."""
    values = {k: v for k in keys if (v := run_key(layer, role, k)) is not None}
    if isinstance(output := values.get("output"), Mapping):
        values["output"] = {k: v for k, v in output.items() if v != ""}
    return values
