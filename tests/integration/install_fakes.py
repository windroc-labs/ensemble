"""Fake macOS tools for install_integration_test: run as `install_fakes.py <tool> <args>`.

Every call is logged to $LOG (argv) and $ENVLOG (environment values). State lives under
$FAKES: brew/<name> (installed), processes (one running command line each; unkillable: those KILL misses), login/<client>, keychain/<service>, claude/ and codex/ (plugins),
src/broker/ (the broker's Codex install and tokens), fail (a call prefix that fails once), tty_name
(what `ps -o tty=` reports), kqueue_refuses (a terminal the Bun-built clients die on, as on /dev/tty),
commit (the commit GitHub reports for every ensemble ref), linear.json (the Linear workspace: keys
{key: email}, teams [{id, key, name, states, members, projects}], labels).
"""

import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import termios
from pathlib import Path

FAKES = Path(os.environ.get("FAKES", "/nonexistent"))
ROOT = Path(os.environ.get("ENSEMBLE_INSTALL_ROOT", "/nonexistent"))
PREFIX = ROOT / "opt/homebrew"
BROKER = ROOT / "usr/local/libexec/ensemble/ensemble-broker"
APPS = {"gh": "gh", "tmux": "tmux", "git": "git", "go": "go", "python": "python3", "node": "npm"}
CASKS = {"claude-code": "claude", "codex": "codex", "docker-desktop": "docker"}
CASK_APPS = {"docker-desktop": "Docker.app", "iterm2": "iTerm.app"}


def out(text, stream=sys.stdout):
    stream.write(text)
    stream.flush()


def mark(path, text=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def load(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def save(path, value):
    mark(path, json.dumps(value))


def link_app(command):
    (PREFIX / "bin").mkdir(parents=True, exist_ok=True)
    shutil.copy(FAKES / "apps" / command, PREFIX / "bin" / command)


def uname(args):
    platform = (
        (FAKES / "platform").read_text() if (FAKES / "platform").exists() else "Darwin arm64\n"
    )
    out(platform.split()[-1] + "\n" if args == ["-m"] else platform)


def id_(args):
    out("tester\n" if args == ["-un"] else "501\n")


def sw_vers(_args):
    out((FAKES / "macos").read_text() if (FAKES / "macos").exists() else "26.7.1\n")


def lipo(args):
    """lipo -archs <binary>: the fake Docker binary's text is its CPU types."""
    out(Path(args[-1]).read_text() + "\n")


def ps(_args):
    out((FAKES / "tty_name").read_text() if (FAKES / "tty_name").exists() else "??\n")


def bun(name):
    """None, or 1 after Bun's error when stdin is the terminal macOS's kqueue refuses."""
    refused = FAKES / "kqueue_refuses"
    if refused.exists() and os.isatty(0) and os.ttyname(0) == refused.read_text():
        out(f"{name}: error: EINVAL: invalid argument, kqueue\n", sys.stderr)
        return 1
    return None


def sudo(args):
    ticket = FAKES / "sudo_ticket"
    password = (FAKES / "sudo_password").read_text()
    if args == ["-k"]:
        ticket.unlink(missing_ok=True)
        return 0
    if args == ["-v"]:
        out("Password:")
        if sys.stdin.readline().rstrip("\n") != password:
            out("Sorry, try again.\n", sys.stderr)
            return 1
        mark(ticket)
        return 0
    if args == ["-S", "-p", "", "-v"]:
        if sys.stdin.readline().rstrip("\n") != password:
            return 1
        mark(ticket)
        return 0
    flags = []
    while args and args[0] in ("-n", "-A", "-E", "--"):
        flags.append(args.pop(0))
        if flags[-1] == "--":
            break
    if not ticket.exists() or os.environ.get("FAKE_TICKET_MISSED"):
        if "-A" not in flags or "SUDO_ASKPASS" not in os.environ:
            out("sudo: a password is required\n", sys.stderr)
            return 1
        asked = subprocess.run([os.environ["SUDO_ASKPASS"], "Password:"], capture_output=True)
        if asked.stdout.decode().rstrip("\n") != password:
            out("sudo: askpass gave a wrong password\n", sys.stderr)
            return 1
        mark(ticket)
        mark(FAKES / "askpass_used")
    if args == ["-v"]:
        mark(FAKES / "keepalive.pid", str(os.getppid()))
        return 0
    home = os.environ["HOME"]
    if any(a.startswith("/") and not a.startswith(home + "/") for a in args[1:]):
        out(f"fake sudo: refusing a path outside {home}: {args}\n", sys.stderr)
        return 1
    os.execvpe(args[0], args, {**os.environ, "FAKE_SUDO": "1"})
    return 1


HOMEBREW_INSTALLER = """#!/bin/bash
set -e
[ "$NONINTERACTIVE" = 1 ]
sudo -n true
mkdir -p "{prefix}/bin"
cp "{apps}/brew" "{prefix}/bin/brew"
mkdir -p "{root}/Library/Developer/CommandLineTools"
"""


LINEAR = "https://api.linear.app/graphql"


def linear(args):
    """Linear's GraphQL API: the key from the -K - config on stdin, the query from --data-binary."""
    if args[:1] != ["-q"]:
        out("curl: Linear call without -q first\n", sys.stderr)
        return 2
    auth = re.fullmatch(r'header = "Authorization: (.*)"\n', sys.stdin.read())
    workspace = load(FAKES / "linear.json", {})
    if not auth or auth.group(1) not in workspace.get("keys", {}):
        out("curl: (22) The requested URL returned error: 400\n", sys.stderr)
        return 22
    sent = json.loads(args[args.index("--data-binary") + 1])
    query, variables = sent["query"], sent.get("variables") or {}
    if "viewer" in query:
        data = {"viewer": {"email": workspace["keys"][auth.group(1)]}}
    elif "teams(" in query:
        data = {
            "teams": {
                "nodes": [{k: t[k] for k in ("id", "key", "name")} for t in workspace["teams"]]
            }
        }
    elif "team(id" in query:
        found = [t for t in workspace["teams"] if t["id"] == variables.get("id")]
        if not found:
            out(json.dumps({"data": None, "errors": [{"message": "Entity not found: Team"}]}))
            return 0
        team = found[0]
        data = {
            "team": {
                "key": team["key"],
                **{k: {"nodes": team[k]} for k in ("states", "members", "projects")},
            }
        }
    elif "issueLabels" in query:
        data = {"issueLabels": {"nodes": workspace["labels"]}}
    else:
        out(f"curl: unexpected Linear query {query}\n", sys.stderr)
        return 2
    out(json.dumps({"data": data}))
    return 0


def curl(args):
    url = args[-1]
    if url == LINEAR:
        return linear(args)
    if url == "https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh":
        out(HOMEBREW_INSTALLER.format(prefix=PREFIX, apps=FAKES / "apps", root=ROOT))
        return 0
    if url == "https://raw.githubusercontent.com/Homebrew/install/HEAD/uninstall.sh":
        out(f'#!/bin/bash\nset -e\n[ "$NONINTERACTIVE" = 1 ]\nrm -f "{PREFIX}/bin/brew"\n')
        return 0
    if url.startswith("https://api.github.com/repos/windroc-labs/ensemble/commits/"):
        out((FAKES / "commit").read_text())
        return 0
    raw = "https://raw.githubusercontent.com/windroc-labs/ensemble/"
    if url.startswith(raw) and url.endswith("/src/broker/internal/launcher/versions.go"):
        out((FAKES / "pkg-src/src/broker/internal/launcher/versions.go").read_text())
        return 0
    out(f"curl: unexpected {url}\n", sys.stderr)
    return 22


def homebrew_sudo():
    """Homebrew's sudo for a cask's artifacts, which macOS's per-terminal ticket misses."""
    flags = ["-A"] if "SUDO_ASKPASS" in os.environ else []
    env = {**os.environ, "FAKE_TICKET_MISSED": "1"}
    return subprocess.run(["sudo", *flags, "-E", "--", "true"], env=env, check=False).returncode


def brew(args):
    trust = Path(os.environ["HOME"], ".homebrew/trust.json")
    if not trust.exists():
        mark(trust, "{}")
    if args == ["shellenv"]:
        out(f'export HOMEBREW_PREFIX="{PREFIX}"; export PATH="{PREFIX}/bin:$PATH";\n')
        return 0
    verb, rest = args[0], args[1:]
    cask = "--cask" in rest
    name = [a for a in rest if not a.startswith("--")][0]
    installed = FAKES / "brew" / name
    if verb == "list":
        return 0 if installed.exists() else 1
    if verb == "install":
        if name == "docker-desktop" and homebrew_sudo():
            return 1
        mark(installed)
        if command := (CASKS if cask else APPS).get(name):
            link_app(command)
        if name == "node":
            link_app("node")
        if app := CASK_APPS.get(name):
            (ROOT / "Applications" / app).mkdir(parents=True, exist_ok=True)
        if name == "docker-desktop":
            docker_app(ROOT / "Applications" / "Docker.app")
        return 0
    if verb == "uninstall":
        if (FAKES / "dependents" / name).exists():
            out(f"Error: Refusing to uninstall {name} because it is required by mine\n", sys.stderr)
            return 1
        installed.unlink()
        if command := (CASKS if cask else APPS).get(name):
            (PREFIX / "bin" / command).unlink(missing_ok=True)
        if app := CASK_APPS.get(name):
            shutil.rmtree(ROOT / "Applications" / app)
        if (
            name == "docker-desktop"
        ):  # the cask's uninstall stanza: its CLI links, helper and app state
            for gone in (
                ROOT / "usr/local/bin/docker",
                FAKES / "docker_license",
                FAKES / "docker_started",
            ):
                gone.unlink(missing_ok=True)
            (FAKES / "docker_running").unlink(missing_ok=True)
        return 0
    return 1


DOCKER_BLOCK = """
# The following lines have been added by Docker Desktop to enable Docker CLI completions.
export PATH="$PATH:{home}/.docker/bin"
# End of Docker Desktop section.
"""


def docker_app(app):
    """Docker.app as the cask puts it in place: Info.plist, its binary, its install and uninstall."""
    macos = app / "Contents/MacOS"
    macos.mkdir(parents=True, exist_ok=True)
    minimum = (
        (FAKES / "docker_min_os").read_text() if (FAKES / "docker_min_os").exists() else "14.0"
    )
    info = {"CFBundleExecutable": "Docker", "LSMinimumSystemVersion": minimum}
    (app / "Contents/Info.plist").write_bytes(
        plistlib.dumps({**info, "CFBundleShortVersionString": "4.96.0"})
    )
    arch = (FAKES / "docker_arch").read_text() if (FAKES / "docker_arch").exists() else "arm64"
    (macos / "Docker").write_text(arch)
    for name in ("install", "uninstall"):
        (macos / name).write_text(
            f'#!/bin/sh\nexec {sys.executable} -I {__file__} docker-{name} "$@"\n'
        )
        (macos / name).chmod(0o755)


def docker_install(args):
    if os.environ.get("FAKE_SUDO") != "1" or args != ["--accept-license", "--user=tester"]:
        out(
            f"docker install: want sudo and --accept-license --user=tester, not {args}\n",
            sys.stderr,
        )
        return 1
    mark(FAKES / "docker_license")
    cli = ROOT / "usr/local/bin/docker"
    if not cli.exists():
        cli.parent.mkdir(parents=True, exist_ok=True)
        cli.symlink_to(PREFIX / "bin/docker")
    return 0


def docker_uninstall(_args):
    (ROOT / "usr/local/bin/docker").unlink(missing_ok=True)
    (FAKES / "docker_license").unlink(missing_ok=True)
    (FAKES / "docker_running").unlink(missing_ok=True)
    (FAKES / "docker_started").unlink(missing_ok=True)
    return 0


def login(client, args, status, start, logout):
    flag = FAKES / "login" / client
    if args == status:
        return 0 if flag.exists() else 1
    if args == logout:
        flag.unlink(missing_ok=True)
        return 0
    if args == start:
        out(f"Log in to {client} in your browser.\n")
        mark(flag)
        return 0
    return None


def gh(args):
    for path in (
        xdg("XDG_CACHE_HOME", ".cache") / "gh",
        xdg("XDG_STATE_HOME", ".local/state") / "gh",
    ):
        mark(path / "installer-cache", "fake gh cache")
    logout = ["auth", "logout", "--hostname", "github.com"]
    return login("gh", args, ["auth", "status"], ["auth", "login", *args[2:]], logout)


def marketplace_name(source):
    name = source.split("#")[0].split("/")[-1].removesuffix(".git")
    return {"agent-plugins-marketplace": "windroc"}.get(name, name)


def claude(args):
    if args in (["auth", "login"], ["auth", "logout"], ["setup-token"]) and bun("claude"):
        return 1
    if args == ["auth", "login"]:
        store_key("Claude Code-credentials", "oauth")
    done = login("claude", args, ["auth", "status"], ["auth", "login"], ["auth", "logout"])
    if done is not None:
        return done
    if args == ["setup-token"]:
        out(f"Your token: {(FAKES / 'claude_token').read_text()}\n")
        return 0
    markets_file, plugins_file = FAKES / "claude/marketplaces.json", FAKES / "claude/plugins.json"
    markets, plugins = load(markets_file, []), load(plugins_file, [])
    if args == ["plugin", "marketplace", "list", "--json"]:
        out(json.dumps(markets))
    elif args[:3] == ["plugin", "marketplace", "add"]:
        repo, _, ref = args[3].partition("#")
        entry = {"name": marketplace_name(repo), "source": "github", "repo": repo}
        save(markets_file, [*markets, {**entry, **({"ref": ref} if ref else {})}])
    elif args[:3] == ["plugin", "marketplace", "remove"]:
        save(markets_file, [m for m in markets if m["name"] != args[3]])
    elif args == ["plugin", "list", "--json"]:
        out(json.dumps(plugins))
    elif args[:2] == ["plugin", "install"]:
        name, market = args[2].split("@")
        source = next(m for m in markets if m["name"] == market)
        path = FAKES / "claude/cache" / str(len(list((FAKES / "claude").glob("cache/*"))))
        if name == "ensemble":
            shutil.copytree(FAKES / "pkg-src", path)
            mark(path / "REF", source.get("ref", ""))
        save(plugins_file, [*plugins, {"id": args[2], "enabled": True, "installPath": str(path)}])
    elif args[:2] == ["plugin", "enable"]:
        save(plugins_file, [{**p, "enabled": p["enabled"] or p["id"] == args[2]} for p in plugins])
    elif args[:2] == ["plugin", "uninstall"]:
        found = [p for p in plugins if p["id"] == args[2]]
        if not found:
            return 1
        shutil.rmtree(found[0]["installPath"], ignore_errors=True)
        save(plugins_file, [p for p in plugins if p["id"] != args[2]])
    else:
        return 2
    return 0


def codex_home():
    return Path(os.environ.get("CODEX_HOME") or Path(os.environ["HOME"], ".codex"))


def codex_config(markets):
    home = codex_home()
    lines = []
    for name, m in markets.items():
        lines += [f"[marketplaces.{name}]", 'source_type = "git"', f'source = "{m["source"]}"']
        lines += [f'ref = "{m["ref"]}"'] if m.get("ref") else []
    mark(home / "config.toml", "\n".join(lines) + "\n")


def codex(args):
    done = login("codex", args, ["login", "status"], ["login"], ["logout"])
    if done is not None:
        return done
    markets_file, plugins_file = FAKES / "codex/marketplaces.json", FAKES / "codex/plugins.json"
    markets, plugins = load(markets_file, {}), load(plugins_file, [])
    if args[:3] == ["plugin", "marketplace", "add"]:
        source = f"https://github.com/{args[3]}.git"
        ref = args[args.index("--ref") + 1] if "--ref" in args else ""
        markets[marketplace_name(args[3])] = {"source": source, "ref": ref}
    elif args[:3] == ["plugin", "marketplace", "remove"]:
        markets.pop(args[3])
    elif args == ["plugin", "list", "--json"]:
        out(json.dumps({"installed": plugins, "available": []}))
        return 0
    elif args[:2] == ["plugin", "add"]:
        plugins.append({"pluginId": args[2], "installed": True, "enabled": True})
        if args[2] == "ensemble@windroc":
            cache = codex_home() / "plugins/cache/windroc/ensemble/9.8.7"
            shutil.rmtree(cache, ignore_errors=True)
            shutil.copytree(FAKES / "pkg-src", cache)
            mark(cache / "REF", markets["windroc"].get("ref", ""))
    elif args[:2] == ["plugin", "remove"]:
        if not any(p["pluginId"] == args[2] for p in plugins):
            return 1
        plugins = [p for p in plugins if p["pluginId"] != args[2]]
        if args[2] == "ensemble@windroc":
            shutil.rmtree(codex_home() / "plugins/cache/windroc/ensemble", ignore_errors=True)
    else:
        return 2
    save(markets_file, markets)
    save(plugins_file, plugins)
    codex_config(markets)
    return 0


def store_key(service, value):
    mark(FAKES / "keychain" / service, value)


def hidden_line(prompt):
    """A line read as security reads a password: not echoed on a terminal."""
    if not os.isatty(0):
        out(prompt)
        return sys.stdin.readline().rstrip("\n")
    saved = termios.tcgetattr(0)
    quiet = [*saved[:3], saved[3] & ~termios.ECHO, *saved[4:]]
    termios.tcsetattr(0, termios.TCSADRAIN, quiet)
    try:
        out(prompt)
        return sys.stdin.readline().rstrip("\n")
    finally:
        termios.tcsetattr(0, termios.TCSADRAIN, saved)
        out("\n")


def security(args):
    if args[:1] == ["find-generic-password"]:
        item = FAKES / "keychain" / args[args.index("-s") + 1]
        if not item.exists():
            return 44
        if "-w" in args:
            out(item.read_text() + "\n")
        return 0
    if args[:1] == ["add-generic-password"] and args[-1] == "-w":
        store_key(args[args.index("-s") + 1], hidden_line("password data for new item: "))
        return 0
    if args[:1] == ["delete-generic-password"]:
        item = FAKES / "keychain" / args[args.index("-s") + 1]
        if not item.exists():
            return 44
        item.unlink()
        return 0
    return 2


def xcode_select(args):
    return 0 if args == ["-p"] and (ROOT / "Library/Developer/CommandLineTools").exists() else 2


def docker(args):
    return 0 if args == ["info"] and (FAKES / "docker_running").exists() else 1


def open_(args):
    """Docker Desktop's first start also adds its block to ~/.zprofile, ~/.zshrc and ~/.bashrc and writes its own
    files under ~/Library. Installed by the cask, it asks for its
    license unless its install command accepted it."""
    cask = (FAKES / "brew/docker-desktop").exists()
    if cask and not (FAKES / "docker_license").exists():
        return 0
    if args == ["-a", "Docker"] and not (FAKES / "docker_wont_start").exists():
        if not (FAKES / "docker_started").exists():
            home = Path(os.environ["HOME"])
            for name in (".zprofile", ".zshrc", ".bashrc"):
                rc = home / name
                if "by Docker Desktop" not in (rc.read_text() if rc.exists() else ""):
                    with open(rc, "a") as profile:
                        profile.write(DOCKER_BLOCK.format(home=home))
            mark(home / "Library/Application Support/Docker Desktop/state.json", "{}")
            mark(home / "Library/Preferences/com.docker.docker.plist", "prefs")
            agent = ROOT / "Applications/Docker.app/Contents/Resources/cli-plugins/docker-agent"
            mark(
                FAKES / "processes",
                "".join(f"{line}\n" for line in [*processes(), f"{agent} serve"]),
            )
            mark(FAKES / "docker_started")
        mark(FAKES / "docker_running")
    return 0


def processes():
    path = FAKES / "processes"
    return path.read_text().splitlines() if path.exists() else []


def pgrep(args):
    """pgrep -f <regex>: the 1-based line numbers of the matching processes as their pids."""
    pattern = re.compile(args[args.index("-f") + 1])
    found = [str(n) for n, line in enumerate(processes(), 1) if pattern.search(line)]
    out("".join(f"{pid}\n" for pid in found))
    return 0 if found else 1


def pkill(args):
    """pkill -TERM|-KILL -f <regex>: ends the matching processes, but none listed in $FAKES/unkillable."""
    pattern = re.compile(args[args.index("-f") + 1])
    stays = (
        set((FAKES / "unkillable").read_text().splitlines())
        if (FAKES / "unkillable").exists()
        else set()
    )
    running = processes()
    kept = [line for line in running if not pattern.search(line) or line in stays]
    mark(FAKES / "processes", "".join(f"{line}\n" for line in kept))
    return 0 if any(pattern.search(line) for line in running) else 1


def defaults(_args):
    return 0


def launchctl(args):
    return 3 if args[:1] == ["bootout"] else 0


def shasum(args):
    files = args[2:]
    if not files:
        out(f"{hashlib.sha256(sys.stdin.buffer.read()).hexdigest()}  -\n")
    for name in files:
        out(f"{hashlib.sha256(Path(name).read_bytes()).hexdigest()}  {name}\n")
    return 0


def broker(args):
    codex_file = BROKER.parent / "codex/state.json"
    state = load(codex_file, None)
    if args[:2] in (["login", "opencode"], ["login", "codex"]):
        service = "ensemble-" + args[1]
        if (FAKES / "keychain" / ("foreign-" + service)).exists():
            account = "auth" if args[1] == "codex" else args[2]
            out(
                f"ensemble-broker: keychain item {service}/{account} belongs to another process"
                " (keychain status -25244); delete this item from the login keychain in Keychain"
                " Access, then rerun install.sh\n",
                sys.stderr,
            )
            return 11
    if args == ["status", "--cleanup"]:
        default = {"state": "installation_invalid", "version": ""}
        default["repair"] = (
            "sudo /usr/local/libexec/ensemble/ensemble-broker install-codex <version>"
        )
        claude = (FAKES / "keychain/ensemble-claude").exists()
        claude = claude and not (FAKES / "src/broker/claude_unusable").exists()
        out(
            json.dumps(
                {"revocations": [], "codex": state or default, "claude": {"default": claude}}
            )
        )
        return 0
    if args[:1] in (["install-codex"], ["update-codex"]):
        if os.environ.get("FAKE_SUDO") != "1":
            return 1
        if args[0] == "install-codex" and state is not None:
            out("ensemble-broker: Codex already installed; use update-codex\n", sys.stderr)
            return 1
        kept = state if args[0] == "update-codex" and state and state.get("version") else {}
        fresh = {"state": "login_unavailable", "repair": "login"}
        save(codex_file, {**fresh, **kept, "version": args[1]})
        return 0
    if args == ["login", "claude"]:
        out("Paste the setup-token (hidden): ")
        mark(FAKES / "src/broker/claude_token", sys.stdin.readline().rstrip("\n"))
        store_key("ensemble-claude", "token")
        (FAKES / "src/broker/claude_unusable").unlink(missing_ok=True)
        return 0
    if args[:3] == ["login", "github", "ensemble-bot"] and args[3:4] == ["--key-file"]:
        if (
            (FAKES / "keychain/ensemble-bot").exists()
            or args[5:6] != ["--app-id"]
            or len(args) != 7
        ):
            return 1
        store_key("ensemble-bot", Path(args[4]).read_text())
        mark(FAKES / "keychain/ensemble-bot.account", args[6])
        args = args[:3]
    if args == ["login", "github", "ensemble-bot"]:
        if not (FAKES / "keychain/ensemble-bot").exists():
            return 1
        store_key("ensemble-github-users", "42")
        out("GitHub App ensemble-test-bot bot user ensemble-test-bot[bot] has ID 42\n")
        return 0
    if args == ["login", "codex"]:
        save(codex_file, {**(state or {}), "state": "ready", "repair": ""})
        store_key("ensemble-codex", "auth")
        return 0
    if args[:2] == ["login", "opencode"]:
        if len(args) == 5 and args[3] == "--auth-file":
            data = json.loads(Path(args[4]).read_text())
            entry = data.get(args[2], {})
            key = entry.get("key") if entry.get("type") == "api" else None
        elif len(args) == 3:
            key = hidden_line(f"OpenCode provider {args[2]} API key (hidden): ")
        else:
            return 2
        if not key:
            out("ensemble-broker: API key must be 1 to 16384 bytes\n", sys.stderr)
            return 1
        store_key("ensemble-opencode", key)
        return 0
    return 2


def opencode_auth():
    data = Path(os.environ.get("XDG_DATA_HOME") or Path(os.environ["HOME"], ".local/share"))
    return data / "opencode/auth.json"


STUB = '#!/bin/sh\necho "Error: opencode-ai\'s postinstall script was not run." >&2\nexit 1\n'


def npm(args):
    """Installs opencode-ai as npm's allowScripts leaves it: the stub, its postinstall not run."""
    prefix = Path(args[args.index("--prefix") + 1])
    version = args[-1].split("@", 1)[1]
    package = prefix / "lib/node_modules/opencode-ai"
    mark(package / "postinstall.mjs")
    mark(package / "VERSION", version)
    (prefix / "bin").mkdir(parents=True, exist_ok=True)
    (prefix / "bin/opencode").write_text(STUB)
    (prefix / "bin/opencode").chmod(0o755)
    return 0


def node(args):
    """opencode-ai's postinstall: the platform binary in place of the stub."""
    package = Path(args[0]).parent
    if (
        args[0].endswith("/opencode-ai/postinstall.mjs")
        and not (FAKES / "postinstall_broken").exists()
    ):
        shutil.copy(FAKES / "apps/opencode", package.parents[2] / "bin/opencode")
        mark(FAKES / "opencode_version", (package / "VERSION").read_text())
    return 0


def xdg(name, default):
    return Path(os.environ.get(name) or Path(os.environ["HOME"], default))


def opencode_plugin(spec):
    """opencode plugin -g -f: the spec in the global config, the files npm adds there, and the package,
    which OpenCode downloads only when the spec has none yet: then the ensemble commit GitHub reports.
    """
    conf = xdg("XDG_CONFIG_HOME", ".config") / "opencode"
    path = conf / "opencode.json" if (conf / "opencode.json").exists() else conf / "opencode.jsonc"
    data = (
        json.JSONDecoder().raw_decode(path.read_text().lstrip("\ufeff \t\r\n"))[0]
        if path.exists()
        else {"$schema": "https://opencode.ai/config.json"}
    )
    name = spec.split("@git+")[0]
    data["plugin"] = [p for p in data.get("plugin", []) if not p.startswith(name + "@")] + [spec]
    mark(path, json.dumps(data, indent=2))
    for extra in ("package.json", "package-lock.json", ".gitignore"):
        mark(conf / extra, "{}")
    (conf / "node_modules").mkdir(exist_ok=True)
    package = Path(os.path.normpath(xdg("XDG_CACHE_HOME", ".cache") / "opencode/packages" / spec))
    if not package.exists():
        shutil.copytree(FAKES / "pkg-src", package / "node_modules/ensemble")
        mark(package / "node_modules/ensemble/REF", spec.split("#")[1])
        shutil.copy(FAKES / "commit", package / "node_modules/ensemble/COMMIT")
    out(f"Installed {spec}\n")
    return 0


def opencode(args):
    mark(xdg("XDG_DATA_HOME", ".local/share") / "opentui/installer-cache", "fake OpenTUI cache")
    if args[:3] == ["plugin", "-g", "-f"] and len(args) == 4:
        return opencode_plugin(args[3])
    if args == ["--version"]:
        out((FAKES / "opencode_version").read_text() + "\n")
        return 0
    if args == ["auth", "login"]:
        if bun("opencode"):
            return 1
        out("Provider: ")
        name = sys.stdin.readline().strip()
        key = hidden_line("API key: ")
        if not key:
            return 1
        path = opencode_auth()
        data = json.loads(path.read_text()) if path.exists() else {}
        data[name] = {"type": "api", "key": key}
        mark(path, json.dumps(data))
        path.chmod(0o600)
        return 0
    return 2


TOOLS = {
    "uname": uname,
    "id": id_,
    "ps": ps,
    "sudo": sudo,
    "curl": curl,
    "brew": brew,
    "gh": gh,
    "tmux": lambda _args: 0,
    "git": lambda _args: 0,
    "go": lambda _args: 0,
    "npm": npm,
    "node": node,
    "opencode": opencode,
    "claude": claude,
    "codex": codex,
    "security": security,
    "docker": docker,
    "xcode-select": xcode_select,
    "open": open_,
    "launchctl": launchctl,
    "defaults": defaults,
    "pgrep": pgrep,
    "pkill": pkill,
    "shasum": shasum,
    "ensemble-broker": broker,
    "docker-uninstall": docker_uninstall,
    "docker-install": docker_install,
    "sw_vers": sw_vers,
    "lipo": lipo,
}


def main(argv):
    name, args = argv[1], argv[2:]
    call = " ".join([name, *args])
    with open(os.environ["LOG"], "a") as log:
        log.write(call + "\n")
    with open(os.environ["ENVLOG"], "a") as log:
        log.write("\n".join(os.environ.values()) + "\n")
    fail = FAKES / "fail"
    if fail.exists() and call.startswith(fail.read_text()):
        fail.unlink()
        out(f"{name}: fake failure\n", sys.stderr)
        return 1
    code = TOOLS[name](args)
    return 0 if code is None else code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
