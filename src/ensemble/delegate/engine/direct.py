"""Direct named-environment workers; the session CLI owns their state and lifecycle."""

import json
import os
import subprocess
import sys

from ensemble.delegate.engine import config, environment
from ensemble.delegate.engine.types import ConfigError
from ensemble.session import Host
from ensemble.utils import EVENTS_ENV, SID_ENV, source_path


def open_worker(
    name: str,
    selected: str,
    *,
    client: str | None = None,
    account: str | None = None,
    cwd: str | None = None,
    display: str | None = None,
    sid: str | None = None,
    events: str | None = None,
) -> int:
    cfg = config.read_config()
    hosts = Host.parse_hosts(cfg.get("hosts"))
    host = hosts.default
    if not config.docker_kind(hosts.tables[host]):
        raise ConfigError(f"open: Host {host} is not a Docker-kind Host")
    resolved = environment.resolve_fields(
        cfg,
        host=host,
        name=selected,
        overrides={},
        packaged=config.packaged_environments(),
        by=f"open {name}",
    )
    if resolved is not None and resolved.prefetch:
        raise ConfigError("open: prefetch is not supported for direct use")
    argv = [
        sys.executable,
        "-P",
        "-s",
        "-m",
        "ensemble.session.cli",
        "start",
        name,
        "--host",
        host,
        "--client",
        client or cfg.get("client", ""),
        "--cwd",
        os.path.abspath(cwd or os.getcwd()),
    ]
    if resolved is not None:
        argv += ["--environment-spec", json.dumps(resolved.spec.persisted())]
    for flag, value in (("--account", account), ("--display", display)):
        if value is not None:
            argv += [flag, value]
    child_env = {**os.environ, **source_path(config.module_roots())}
    for key, value in ((SID_ENV, sid), (EVENTS_ENV, events)):
        child_env.pop(key, None)
        if value:
            child_env[key] = value
    result = subprocess.run(argv, env=child_env, check=False)
    return result.returncode if result.returncode >= 0 else 1
