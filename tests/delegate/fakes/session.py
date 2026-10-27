"""A caller-owned fake Session class for Delegate driver contract tests; like an unowned start,
its preflight refuses handlers unless `owns`.
"""

from ensemble.session import ConfigError, Host, Session, Spec
from tests.session.fakes.session import FakeSession


class SessionClass(FakeSession):
    preflights: list[dict] = []
    preflight_error: Exception | None = None
    owns = True

    def __init_subclass__(cls, **keywords):
        super().__init_subclass__(**keywords)
        cls.preflights, cls.preflight_error, cls.owns = [], None, True

    @classmethod
    def fresh(cls):
        return cls.scripted()

    @classmethod
    def configure_from(cls, config, *, hosts=None):
        cls.configure(
            clients=config.get("clients", {}),
            hosts=Host.parse_hosts(config.get("hosts")) if hosts is None else hosts,
        )

    @classmethod
    def class_roots(cls, client):
        return ()

    @classmethod
    def preflight(cls, spec, *, display=None, host=None, idle_timeout=None):
        if spec.handlers and not cls.owns:
            raise ConfigError(f"handlers: {spec.client} {spec.mode} has no session harness yet")
        cls.preflights.append(
            {"spec": spec, "display": display, "host": host, "idle_timeout": idle_timeout}
        )
        if cls.preflight_error is not None:
            raise cls.preflight_error

    @classmethod
    def start(cls, spec: Spec, *, display=None, host=None, idle_timeout=None) -> Session:
        return super().start(spec, display=display, host=host, idle_timeout=idle_timeout)
