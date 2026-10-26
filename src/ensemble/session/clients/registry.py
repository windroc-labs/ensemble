"""Caller-owned client configuration, copied read-only once: client_class loads a table's
`implementation` (module.path:Class); client builds one per call (per session) from its own copy.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from ensemble.session.clients.base import Client
from ensemble.session.engine.types import ConfigError, Hosts
from ensemble.session.hosts.base import Host
from ensemble.utils import CONFIG_NAME as NAME
from ensemble.utils import freeze, thaw
from ensemble.utils import load_class as load


@dataclass(frozen=True)
class Settings:
    clients: Mapping[str, Mapping]
    hosts: Mapping[str, Mapping]


PACKAGE = "ensemble.session"


def settings(config: Mapping, *, hosts: Hosts | None = None) -> dict:
    """A read session config's [clients] and [viewer], and its [hosts] parsed once (`hosts`: parsed
    already).
    """
    if not isinstance(config, Mapping):
        raise ConfigError("session config: want a table")
    if hosts is None:
        hosts = Host.parse_hosts(config.get("hosts"))
    return {
        "clients": config.get("clients", {}),
        "hosts": hosts,
        "viewer": config.get("viewer", {}),
    }


class Registry:
    def __init__(self, *, clients: Mapping, hosts: Hosts, class_loader=load) -> None:
        if not isinstance(clients, Mapping) or not all(
            isinstance(k, str) and isinstance(t, Mapping) for k, t in clients.items()
        ):
            raise ConfigError("clients: want a mapping of [clients.<name>] tables")
        for name, table in clients.items():
            limit = table.get("max_runs", 0)
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
                raise ConfigError(
                    f"[clients.{name}].max_runs: want a whole number >= 0, got {limit!r}"
                )
        self.settings = Settings(freeze(clients), hosts.tables)
        self.class_loader = class_loader

    def configured(self) -> list[str]:
        return sorted(self.settings.clients)

    def _table(self, name: str) -> Mapping:
        if not isinstance(name, str) or not NAME.match(name):
            raise ConfigError(f"unsafe client name {name!r}: want a lowercase identifier")
        table = self.settings.clients.get(name)
        if table is None:
            raise ConfigError(f"no [clients.{name}] table in the configured clients")
        return table

    def client_class(self, name: str) -> type[Client]:
        path = self._table(name).get("implementation")
        where = f"[clients.{name}].implementation"
        if isinstance(path, str) and path.startswith("clients."):
            raise ConfigError(f"{where}: {path} is relative: want {PACKAGE}.{path}")
        try:
            return self.class_loader(path, Client)
        except ValueError as e:
            raise ConfigError(f"{where}: {e}") from None

    def client(self, name: str) -> Client:
        cls = self.client_class(name)
        config = thaw(self._table(name))
        config.pop("implementation", None)
        config.pop("max_runs", None)
        return cls(config)

    def max_runs(self, name: str) -> int:
        return self._table(name).get("max_runs", 0)
