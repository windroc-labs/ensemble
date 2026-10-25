"""Delegate configuration resources and package locations."""

from collections.abc import Mapping

import ensemble.session as session
import ensemble.utils as utils
from ensemble.delegate import config


def read_config(*, local: str = utils.LOCAL, path: str | None = None) -> dict:
    def validate(package: str, source: str, table: Mapping) -> None:
        if package == "ensemble.delegate":
            config.validate_team_dirs(table, source, local=local)

    return utils.read_resources(
        (*session.CONFIG_RESOURCES, ("ensemble.delegate", "config.toml")),
        local=local,
        paths=None if path is None else {"ensemble.delegate": path},
        validate=validate,
    )


def module_roots() -> tuple[str, ...]:
    return utils.module_roots(config, session, utils)
