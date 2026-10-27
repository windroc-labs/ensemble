"""Delegate configuration location and independent setting parsers."""

import os
from collections.abc import Mapping

from ensemble import utils
from ensemble.utils import real_dirs

CONFIG: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.toml")


def validate_team_dirs(table: Mapping, source: str, *, local: str = utils.LOCAL) -> None:
    if table.get("team_dirs"):
        raise ValueError(f"team_dirs: set it in {local}, not {os.path.basename(source)}")


def delegates(config: Mapping) -> None:
    """[delegates] takes no keys: the delegate spawns its drive itself."""
    table = config.get("delegates", {})
    if not isinstance(table, Mapping):
        raise ValueError("[delegates]: the delegate takes no keys")
    for key in table:
        raise ValueError(f"[delegates] {key}: the delegate takes no keys")


def policy(client: str, cfg: Mapping) -> tuple[list, dict]:
    """[policy.<client>]'s flags and env, unchecked."""
    tables = cfg.get("policy", {})
    if not isinstance(tables, Mapping):
        raise ValueError("policy must be a table")
    value = tables.get(client, {})
    if not isinstance(value, Mapping) or set(value) - {"flags", "env"}:
        raise ValueError(f"policy.{client} must contain only flags/env")
    flags, env = value.get("flags", []), value.get("env", {})
    if not isinstance(flags, list):
        raise ValueError(f"policy.{client}.flags must be a list of strings without NUL")
    return flags, env


def trusted_dirs(cfg: Mapping) -> frozenset[str]:
    return real_dirs(cfg.get("trusted_dirs", []), "trusted_dirs")
