"""Delegate configuration resources and package locations; the environment tables' load checks."""

import re
import tomllib
from collections.abc import Mapping

import ensemble.session as session
import ensemble.utils as utils
from ensemble.delegate import config

DOCKER_HOST = "ensemble.session.hosts.docker.host:DockerHost"
# an environment name follows the lock-name rule (WDRC-109 FR-64)
ENVIRONMENT_NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,62}")
ENVIRONMENT_KEYS = frozenset({"image", "gateway", "plugins", "lock", "prefetch", "credentials"})
# what a named or role table, checked alone, may leave to its default
PLACEHOLDERS = {"image": session.EnvironmentSpec.WORKER, "gateway": "workers"}


def read_config(*, local: str = utils.LOCAL, path: str | None = None) -> dict:
    def validate(package: str, source: str, table: Mapping) -> None:
        if package == "ensemble.delegate":
            config.validate_team_dirs(table, source, local=local)

    found = utils.read_resources(
        (*session.CONFIG_RESOURCES, ("ensemble.delegate", "config.toml")),
        local=local,
        paths=None if path is None else {"ensemble.delegate": path},
        validate=validate,
        opaque=(
            ("environment", "*", "credentials"),
            ("environments", "*", "credentials"),
            ("roles", "*", "environment", "credentials"),
        ),
    )
    check_environments(found)
    return found


def module_roots() -> tuple[str, ...]:
    return utils.module_roots(config, session, utils)


def docker_kind(table) -> bool:
    return isinstance(table, Mapping) and table.get("implementation") == DOCKER_HOST


def packaged_environments() -> Mapping:
    """The packaged config.toml's [environment] tables, without config.local.toml."""
    with open(config.CONFIG, "rb") as stream:
        return tomllib.load(stream).get("environment", {})


def check_environments(cfg: Mapping) -> None:
    """ValueError naming the bad [environment.<host>], [environments.<name>] or
    [roles.<role>.environment] table.
    """
    hosts = cfg.get("hosts", {})
    named = _tables(cfg, "environments")
    for host, table in _tables(cfg, "environment").items():
        where = f"[environment.{host}]"
        if not docker_kind(hosts.get(host) if isinstance(hosts, Mapping) else None):
            raise ValueError(
                f"{where} ([delegate.environment.{host}] in config.local.toml): "
                f"{host} is not a Docker-kind Host"
            )
        _check_table(table, where)
        if missing := [key for key in ("image", "gateway") if key not in table]:
            raise ValueError(f"{where}: missing {', '.join(missing)}")
    for name, table in named.items():
        where = f"[environments.{name}]"
        _check_name(name, where)
        _check_table(table, where)
    roles = cfg.get("roles", {})
    for role, table in roles.items() if isinstance(roles, Mapping) else ():
        if not isinstance(table, Mapping) or "environment" not in table:
            continue
        where = f"[roles.{role}.environment]"
        found = table["environment"]
        if not isinstance(found, Mapping):
            raise ValueError(f"{where} must be a table")
        _check_table({k: v for k, v in found.items() if k != "name"}, where)
        if "name" in found:
            name = found["name"]
            if not isinstance(name, str):
                raise ValueError(f"{where} name must be text")
            _check_name(name, where)
            if name not in named:
                raise ValueError(f"{where} name {name!r}: no [environments.{name}]")


def _tables(cfg: Mapping, key: str) -> Mapping[str, Mapping]:
    found = cfg.get(key, {})
    if not isinstance(found, Mapping):
        raise ValueError(f"[{key}] must be a table")
    if bad := next((name for name, table in found.items() if not isinstance(table, Mapping)), None):
        raise ValueError(f"[{key}.{bad}] must be a table")
    return found


def _check_name(name: str, where: str) -> None:
    if not ENVIRONMENT_NAME.fullmatch(name):
        raise ValueError(
            f"{where}: environment name {name!r:.80} must match {ENVIRONMENT_NAME.pattern}"
        )


def _check_table(table: Mapping, where: str) -> None:
    if "name" in table:
        raise ValueError(
            f"{where}: name selects an environment only in a [roles.<role>.environment]"
        )
    if extra := sorted(set(table) - ENVIRONMENT_KEYS, key=str):
        raise ValueError(f"unknown key {extra[0]!r} in {where}")
    if not isinstance(table.get("prefetch", False), bool):
        raise ValueError(f"{where} prefetch must be true or false")
    try:
        session.EnvironmentSpec.from_fields(
            PLACEHOLDERS
            | {key: value for key, value in table.items() if key not in ("prefetch", "credentials")}
        )
    except session.ConfigError as error:
        raise ValueError(f"{where}: {error}") from None
