"""Client unit helpers: argv runs and the packaged [clients.<name>] tables."""

import os

from ensemble.session.clients import registry
from ensemble.session.engine.config import read_config


def holds(argv, words) -> bool:
    return any(tuple(argv[i : i + len(words)]) == tuple(words) for i in range(len(argv)))


def packaged_table(client: str, local: str = os.devnull) -> dict:
    """The packaged session config.toml's [clients.<client>], under `local`, without
    implementation and max_runs."""
    table = dict(registry.settings(read_config(local=local))["clients"][client])
    table.pop("implementation")
    table.pop("max_runs")
    return table
