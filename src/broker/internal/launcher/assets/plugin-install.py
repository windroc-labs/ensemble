"""Install image plugins with each client's native marketplace commands."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
COMMIT = re.compile(r"[a-f0-9]{40}")
REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}")


def command(argv, env):
    try:
        return subprocess.run(
            argv,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise ValueError(f"plugin-install: command failed: {' '.join(argv)}") from error


def validate(plugins):
    if not isinstance(plugins, list):
        raise ValueError("plugin-install: sources must be a list")
    names, repos = set(), {}
    for plugin in plugins:
        if not isinstance(plugin, dict) or any(
            not isinstance(plugin.get(key), str) or pattern.fullmatch(plugin[key]) is None
            for key, pattern in (("name", NAME), ("repo", REPO), ("commit", COMMIT))
        ):
            raise ValueError("plugin-install: each source needs name, owner/repo and full commit")
        if any(key in plugin for key in ("plugin_repo", "plugin_commit")) and any(
            not isinstance(plugin.get(key), str) or pattern.fullmatch(plugin[key]) is None
            for key, pattern in (("plugin_repo", REPO), ("plugin_commit", COMMIT))
        ):
            raise ValueError("plugin-install: remote plugin needs owner/repo and full commit")
        name, repo, commit = plugin["name"], plugin["repo"], plugin["commit"]
        if name in names:
            raise ValueError(f"plugin-install: duplicate plugin name {name}")
        if repo in repos and repos[repo] != commit:
            raise ValueError(f"plugin-install: conflicting commits for {repo}")
        names.add(name)
        repos[repo] = commit


def verify(client, repo, expected, checkout, env):
    actual = command(["git", "-C", str(checkout), "rev-parse", "HEAD"], env)
    if actual != expected:
        raise ValueError(f"plugin-install: {client} {repo}: expected {expected}, actual {actual!r}")
    return actual


def marketplace(checkout, names, plugins=()):
    manifest_path = checkout / ".claude-plugin/marketplace.json"
    manifest = json.loads(manifest_path.read_text())
    market = manifest.get("name")
    if not isinstance(market, str) or NAME.fullmatch(market) is None:
        raise ValueError("plugin-install: invalid marketplace name")
    entries = manifest.get("plugins", [])
    for name in names:
        selected = [
            entry for entry in entries if isinstance(entry, dict) and entry.get("name") == name
        ]
        source = selected[0].get("source") if len(selected) == 1 else None
        pinned = next((p for p in plugins if p["name"] == name and "plugin_repo" in p), None)
        if pinned is not None:
            repo = pinned["plugin_repo"]
            url = f"https://github.com/{repo}.git"
            if not isinstance(source, dict) or not (
                source.get("source") == "github"
                and source.get("repo") == repo
                or source.get("source") == "url"
                and source.get("url") == url
            ):
                raise ValueError(f"plugin-install: {name}: source must name {repo}")
            selected[0]["source"] = {"source": "url", "url": url, "sha": pinned["plugin_commit"]}
            continue
        if not isinstance(source, str) or not source.startswith("./"):
            raise ValueError(f"plugin-install: {name}: source must be a local ./ directory")
        path = (checkout / source).resolve()
        if not path.is_relative_to(checkout.resolve()) or not path.is_dir():
            raise ValueError(f"plugin-install: {name}: source must stay inside its checkout")
    if any("plugin_repo" in plugin for plugin in plugins):
        manifest_path.write_text(json.dumps(manifest) + "\n")
    return market


def prepare(repo, commit, names, homes, *, markets, env, plugins=()):
    sources, market = {}, None
    for client, home in homes.items():
        checkout = home / "sources" / repo
        checkout.parent.mkdir(parents=True, exist_ok=True)
        try:
            command(["git", "init", str(checkout)], env)
            command(
                [
                    "git",
                    "-C",
                    str(checkout),
                    "fetch",
                    "--depth=1",
                    f"https://github.com/{repo}.git",
                    commit,
                ],
                env,
            )
            command(["git", "-C", str(checkout), "checkout", "--detach", "FETCH_HEAD"], env)
            verify(client, repo, commit, checkout, env)
            actual_market = marketplace(checkout, names, plugins)
        except (ValueError, OSError) as error:
            raise ValueError(
                f"plugin-install: {client} {repo} expected {commit}: {error}"
            ) from error
        if market is not None and market != actual_market:
            raise ValueError(f"plugin-install: {repo}: client marketplace names differ")
        market = actual_market
        if market in markets and markets[market] != repo:
            raise ValueError(f"plugin-install: duplicate marketplace name {market} from {repo}")
        sources[client] = checkout
    markets[market] = repo
    for client, checkout in sources.items():
        command([client, "plugin", "marketplace", "add", str(checkout)], env)
    return market, sources


def installed(home, market, name):
    versions = list((home / "plugins/cache" / market / name).iterdir())
    if len(versions) != 1 or not versions[0].is_dir():
        raise ValueError(f"plugin-install: expected one installed version of {name}@{market}")
    root = versions[0]
    paths = [root / ".codex-plugin/plugin.json", root / ".claude-plugin/plugin.json"]
    path = next((path for path in paths if path.is_file()), None)
    if path is None:
        raise ValueError(f"plugin-install: missing installed manifest for {name}@{market}")
    manifest = json.loads(path.read_text())
    version = manifest.get("version")
    if (
        manifest.get("name") != name
        or not isinstance(version, str)
        or not version.isprintable()
        or not version
    ):
        raise ValueError(f"plugin-install: invalid installed manifest for {name}@{market}")
    return root, version


def verify_plugin(client, plugin, home, market, location, *, env):
    repo, expected = plugin["plugin_repo"], plugin["plugin_commit"]
    if client == "codex":
        return verify(client, repo, expected, location, env)
    record = json.loads((home / "plugins/installed_plugins.json").read_text())
    entries = record.get("plugins", {}).get(f"{plugin['name']}@{market}", [])
    actual = [
        entry.get("gitCommitSha")
        for entry in entries
        if isinstance(entry, dict) and entry.get("installPath") == str(location)
    ]
    if actual != [expected]:
        raise ValueError(f"plugin-install: {client} {repo}: expected {expected}, actual {actual!r}")
    return expected


def install(plugins, root):
    validate(plugins)
    claude, codex = root / "claude-plugins", root / "codex-plugins"
    shared = root / "plugins"
    for home in (claude, codex):
        (home / "plugins/cache").mkdir(parents=True, exist_ok=True)
    (codex / "config.toml").write_text("")
    shared.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(root),
        "CLAUDE_CONFIG_DIR": str(claude),
        "CODEX_HOME": str(codex),
        "GIT_TERMINAL_PROMPT": "0",
    }
    homes = {"claude": claude, "codex": codex}
    markets, prepared = {}, {}
    for plugin in plugins:
        name, repo, expected = plugin["name"], plugin["repo"], plugin["commit"]
        if repo not in prepared:
            names = [item["name"] for item in plugins if item["repo"] == repo]
            selected = [item for item in plugins if item["repo"] == repo]
            prepared[repo] = prepare(
                repo, expected, names, homes, markets=markets, env=env, plugins=selected
            )
        market, sources = prepared[repo]
        identifier = f"{name}@{market}"
        for client, verb, home in (("claude", "install", claude), ("codex", "add", codex)):
            command([client, "plugin", verb, identifier], env)
            location, version = installed(home, market, name)
            commit = verify(client, repo, expected, sources[client], env)
            if "plugin_repo" in plugin:
                commit = verify_plugin(client, plugin, home, market, location, env=env)
            print(
                f"plugin-install: {client} {identifier} version={version} commit={commit}",
                flush=True,
            )
            if client == "claude":
                (shared / name).symlink_to(location)


def main(argv):
    if len(argv) != 2 or not Path(argv[1]).is_absolute():
        sys.exit("usage: plugin-install.py SOURCES ABSOLUTE_ROOT")
    try:
        install(json.loads(Path(argv[0]).read_text()), Path(argv[1]))
    except (ValueError, OSError) as error:
        sys.exit(f"plugin-install: {error}")


if __name__ == "__main__":
    main(sys.argv[1:])
