"""Docker Host runs (docs/architecture.md): Host test, Reference repo, users."""

import itertools
import json
import os
from collections.abc import Mapping

from ensemble.delegate.engine.config import docker_kind
from ensemble.delegate.engine.types import ConfigError
from ensemble.session import ConfigError as SessionConfigError
from ensemble.session import Host
from ensemble.utils import GUARD, GitError, Invalid, Repo, Runner, origin, run_git, sh


def on_docker(settings: Mapping, host: str | None) -> bool:
    """Whether `host` (None: default) is the Docker Host."""
    try:
        hosts = Host.parse_hosts(settings.get("hosts"))
    except SessionConfigError as error:
        raise ConfigError(str(error)) from None
    return docker_kind(hosts.tables.get(host or hosts.default))


def reference_lines(text: str) -> tuple[list[str], int]:
    """(text's lines, kept ends; the length of its Reference block, led by Reference: or Repo:)."""
    lines = text.lstrip().splitlines(keepends=True)
    if not lines or not lines[0].startswith(("Reference:", "Repo:")):
        return lines, 0
    return lines, len(list(itertools.takewhile(str.strip, lines)))


def repo_lines(text: str) -> list[str]:
    """The distinct `Repo:` values in the leading Reference block."""
    lines, n = reference_lines(text)
    return sorted(
        {line.removeprefix("Repo:").strip() for line in lines[:n] if line.startswith("Repo:")}
    )


def with_repo(text: str, repo: str) -> str:
    """`text`, its Reference `Repo:` lines naming `repo`."""
    lines, n = reference_lines(text)
    for i in range(n):
        if lines[i].startswith("Repo:"):
            lines[i] = f"Repo: {repo}" + lines[i][len(lines[i].rstrip("\r\n")) :]
    return "".join(lines)


def reference_repo(text: str, *, run: Runner | None = None) -> str | None:
    """The github.com owner/name of the Reference block's one `Repo:` (a clone's from its origin,
    read with GUARD), else None."""
    found = repo_lines(text)
    if len(found) > 1:
        raise ConfigError(f"the Reference block names more than one Repo: {', '.join(found)}")
    return github_repo(found[0], run or sh) if found else None


def github_repo(spec: str, run: Runner) -> str | None:
    if spec.startswith(("/", "~")):
        clone = os.path.realpath(os.path.expanduser(spec))
        try:
            spec = origin(run_git(clone, "remote", "get-url", "origin", run=run, pre=GUARD)) or ""
        except GitError:
            return None
    try:
        repo = Repo.parse(spec)
    except Invalid:
        return None
    return f"{repo.owner}/{repo.name}" if repo.host == "github.com" else None


def users_env(users) -> dict[str, str]:
    """ENSEMBLE_USERS: the config's users without `[bot]` logins."""
    if not isinstance(users, list | tuple) or not all(isinstance(u, str) for u in users):
        raise ConfigError("users must be a list of GitHub logins")
    return {"ENSEMBLE_USERS": json.dumps([u for u in users if not u.endswith("[bot]")])}
