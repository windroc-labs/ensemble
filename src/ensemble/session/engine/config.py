"""The session layer's configuration entry point."""

from ensemble.utils import LOCAL, read_resources

CONFIG_RESOURCES = (("ensemble.session", "config.toml"),)


def read_config(*, local: str = LOCAL) -> dict:
    return read_resources(CONFIG_RESOURCES, local=local)
