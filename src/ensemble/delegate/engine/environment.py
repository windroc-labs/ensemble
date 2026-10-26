"""A run's EnvironmentSpec from the config layers (engine/config.py checks them at load), and its
prefetch target from the input's Reference block (docs/docker-host.md › Environment config).
"""

from collections.abc import Mapping

from ensemble.delegate.engine.container import reference_lines
from ensemble.delegate.engine.types import ConfigError, Resolved
from ensemble.session import ConfigError as SessionConfigError
from ensemble.session import EnvironmentSpec
from ensemble.utils import Invalid, Repo, check_ref


def resolve(cfg: Mapping, *, role: str, host: str, packaged: Mapping) -> Resolved | None:
    """`role`'s [roles.<role>.environment] over Host `host`'s default."""
    roles = cfg.get("roles", {})
    table = dict(roles.get(role, {}).get("environment", {}))
    name = table.pop("name", None)
    return resolve_fields(
        cfg, host=host, name=name, overrides=table, packaged=packaged, by=f"role {role}"
    )


def resolve_fields(
    cfg: Mapping,
    *,
    host: str,
    name: str | None,
    overrides: Mapping,
    packaged: Mapping,
    by: str = "the run",
) -> Resolved | None:
    """None: Host `host` has no default and nothing is selected, or the result is its packaged
    default (the run starts as before environments).
    """
    default = cfg.get("environment", {}).get(host)
    if default is None:
        if name is None and not overrides:
            return None
        raise ConfigError(
            f"{by} selects an environment, but Host {host} has no [environment.{host}]"
        )
    named = {}
    if name is not None:
        if name not in (environments := cfg.get("environments", {})):
            raise ConfigError(f"{by}: no [environments.{name}]")
        named = environments[name]
    fields = {**default, **named, **overrides}
    prefetch = fields.pop("prefetch", False)
    spec = _spec(fields, host)
    base = packaged.get(host)
    if not prefetch and base is not None and spec == _spec(base, host):
        return None
    return Resolved(spec, prefetch)


def _spec(fields: Mapping, host: str) -> EnvironmentSpec:
    try:
        return EnvironmentSpec.from_fields({k: v for k, v in fields.items() if k != "prefetch"})
    except SessionConfigError as error:
        raise ConfigError(f"[environment.{host}]: {error}") from None


def prefetch_target(text: str) -> tuple[str, str | None]:
    """The Reference block's one `Repo:` value and optional `Ref:`, checked before any git call."""
    lines, n = reference_lines(text)
    repos = [line.removeprefix("Repo:").strip() for line in lines[:n] if line.startswith("Repo:")]
    refs = [line.removeprefix("Ref:").strip() for line in lines[:n] if line.startswith("Ref:")]
    if len(repos) != 1:
        found = f"{len(repos)} Repo: lines" if repos else "no Repo: line"
        raise ConfigError(f"prefetch: want one Repo: line in the Reference block, found {found}")
    if len(refs) > 1:
        raise ConfigError(f"prefetch: want at most one Ref: line, found {len(refs)}")
    repo = repos[0]
    if not repo.startswith(("/", "~")):
        try:
            Repo.parse(repo)
        except Invalid as error:
            raise ConfigError(f"prefetch: Repo: {error}") from None
    try:
        return repo, check_ref(refs[0]) if refs else None
    except Invalid as error:
        raise ConfigError(f"prefetch: Ref: {error}") from None
