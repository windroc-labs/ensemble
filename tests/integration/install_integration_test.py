import json
import os
import plistlib
import re
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from collections import namedtuple
from datetime import UTC, datetime
from pathlib import Path

from ensemble.orchestrator.engine import config as orchestrator_config
from ensemble.orchestrator.trackers.local import board as local_board
from tests.case import REPO, Case

SCRIPT = Path(REPO, "scripts/install.sh")
CLEANUP = Path(REPO, "scripts/install-cleanup.sh")
FAKE_TOOLS = Path(REPO, "tests/integration/install_fakes.py")
REAL = (
    "bash",
    "sh",
    "grep",
    "sed",
    "cat",
    "mkdir",
    "rmdir",
    "mv",
    "rm",
    "cp",
    "chmod",
    "sleep",
    "sort",
    "join",
    "find",
    "cut",
    "dirname",
    "basename",
    "cksum",
    "true",
    "head",
    "tail",
    "cmp",
    "awk",
    "tr",
    "mktemp",
    "mkfifo",
    "stty",
    "tar",
    "gzip",
    "ln",
    "ls",
)
FAKED = ("uname", "id", "sudo", "curl", "security", "open", "launchctl", "shasum")
FAKED += ("xcode-select", "ps", "sw_vers", "lipo", "pgrep", "pkill", "defaults")
APPS = ("brew", "gh", "tmux", "git", "go", "claude", "codex", "docker", "ensemble-broker")
APPS += ("npm", "node", "opencode")
PASSWORD = "pw-SENTINEL-sudo"
TOKEN = "sk-ant-oat01-SENTINEL-token"
KEY = "sk-SENTINEL-opencode-key"
OLD, NEW = "1" * 40, "2" * 40
CLIENT, GO = (
    "src/ensemble/session/hosts/docker/broker.py",
    "src/broker/internal/protocol/protocol.go",
)
# The repo's own broker protocol, never a literal: another protocol is one more.
PROTOCOL = re.search(r'^PROTOCOL = "([0-9]+)"$', Path(REPO, CLIENT).read_text(), re.M)[1]
OTHER = str(int(PROTOCOL) + 1)
MARKET = "https://raw.githubusercontent.com/windroc-labs/agent-plugins-marketplace/main/"
RAW = "https://raw.githubusercontent.com/windroc-labs/ensemble/"
SPEC = "ensemble@git+https://github.com/windroc-labs/ensemble.git#main"
API_CALL = (
    "curl -fsSL -H Accept: application/vnd.github.sha"
    " https://api.github.com/repos/windroc-labs/ensemble/commits/main"
)
MENU_Q = r"Choose what to install \(up/down move, space ticks, Enter confirms\):"
PROVIDER_Q = r"OpenCode provider name:"
URL_Q = r"Its base URL \(ending /v1\):"
MODEL_Q = r"Its model:"
CONTEXT_Q = r"Its context window in tokens \[65536\]:"
OPENCODE_LOGIN_Q = r"Provider:"
PROVIDER_ANSWERS = (
    (PROVIDER_Q, "lan"),
    (URL_Q, "http://10.0.0.5:8080/v1"),
    (MODEL_Q, "m1"),
    (CONTEXT_Q, "204800"),
)
SUDO_Q = r"Password:"
WRITE_Q = r"Write it\? \[y/N\]"
TOKEN_Q = r"Paste the setup-token \(hidden\):"
LEAVE_Q = r"Press Enter to leave this screen:"
SCREEN_ON, SCREEN_OFF = "\x1b[?1049h\x1b[H\x1b[2J", "\x1b[H\x1b[2J\x1b[?1049l"
PEM_Q = r"private key \(\.pem; [^)]*\), empty to skip:"
DOCKER_LOGINS = ((TOKEN_Q, TOKEN), (PEM_Q, ""))
DOCKER_PRESETS = {"UseVirtualizationFrameworkRosetta": False, "DisplayedOnboarding": True}
PRESETS_NOT_WRITTEN = "    Docker Desktop presets not written (Rosetta off, onboarding done): "
NOT_TOLD = "    not recorded for cleanup: "
REMOVE_Q = r"Remove all of this\? \[y/N\]"
SHARED = (
    "say",
    "step",
    "skip",
    "die",
    "ask",
    "ask_yn",
    "under_home",
    "ledger_valid",
    "agent_tree_find",
    "sudo_once",
    "sudo_end",
    "terminal",
)
RELOAD = (
    "install.sh: in this terminal, run source ~/.zprofile first (or open a new one): its PATH"
    " lacks what install.sh added to ~/.zprofile"
)
DOCKER_STEPS = (
    "Docker Desktop running",
    "egress proxy",
    "broker",
    "broker login: Claude",
    "broker login: GitHub",
    "broker Codex install",
    "broker login: Codex",
)
BROKER_INSTALL = """#!/bin/sh
set -eu
here=$(cd "$(dirname "$0")" && pwd)
echo "broker-install $*" >> "$LOG"
[ ! -e "$FAKES/broker_install_fails" ] || exit 1
rm -f "$FAKES/src/broker/protocol"
target="$ENSEMBLE_INSTALL_ROOT/usr/local/libexec/ensemble"
sudo mkdir -p "$target"
{ cat "$FAKES/apps/ensemble-broker"; echo "# $(cksum < "$here/internal/codex/versions.go")"; } > "$target/ensemble-broker"
chmod 755 "$target/ensemble-broker"
"""
CTMUX = """#!/bin/sh
echo "ctmux $*" >> "$LOG"
mkdir -p "$HOME/.ensemble/bin"
uv_cache=${UV_CACHE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/uv}
mkdir -p "$uv_cache"
printf 'fake uv cache' > "$uv_cache/installer-cache"
for n in ctmux ensemble-crew ensemble-session ensemble-delegate ensemble-orchestrator; do
  cp "$(dirname "$0")/launcher" "$HOME/.ensemble/bin/$n"
  chmod 755 "$HOME/.ensemble/bin/$n"
done
"""
ITERM_DIRS = (
    "Application Support/iTerm2",
    "Application Support/iTerm",
    "Caches/com.googlecode.iterm2",
    "HTTPStorages/com.googlecode.iterm2",
    "WebKit/com.googlecode.iterm2",
    "Saved Application State/com.googlecode.iterm2.savedState",
    "Containers/com.googlecode.iterm2.iTermFileProvider",
    "Containers/iTermAI",
    "Application Scripts/com.googlecode.iterm2.iTermFileProvider",
)
ITERM_FILES = (
    "Preferences/com.googlecode.iterm2.plist",
    "Preferences/com.googlecode.iterm2.private.plist",
    "Cookies/com.googlecode.iterm2.binarycookies",
    "HTTPStorages/com.googlecode.iterm2.binarycookies",
)
SECRET_Q = r"password data for new item:"
SLOTS = ("harness", "manager", "researcher", "pm", "engineer")
STATES = ("Backlog", "Todo", "In Progress", "In Review", "Handoff", "Done", "Canceled")


def uuid(n):
    return f"00000000-0000-4000-8000-{n:012d}"


def app_config(slot):
    return {
        "client_id": f"client-{slot}",
        "secret_service": f"linear-app-{slot}-secret",
        "token_service": f"linear-app-{slot}-token",
        "user_id": uuid(100 + (*SLOTS, "reviewer").index(slot)),
    }


def app_table(slot):
    table = slot if slot in ("harness", "manager") else f"roles.{slot}"
    return f"\n[orchestrator.linear.{table}]\n" + "".join(
        f'{key} = "{value}"\n' for key, value in app_config(slot).items()
    )


def label(n, name, parent=None):
    return {
        "id": uuid(n),
        "name": name,
        "isGroup": parent is None and name in ("Tasks", "Agent"),
        "parent": {"id": uuid(parent)} if parent else None,
    }


LINEAR = {
    "apps": {
        f"client-{slot}": {
            "secret": f"k-SENTINEL-{slot}",
            "access_token": f"t-SENTINEL-{slot}",
            "user_id": app_config(slot)["user_id"],
        }
        for slot in (*SLOTS, "reviewer")
    },
    "teams": [
        {
            "id": uuid(1),
            "key": "ENG",
            "name": "Engineering",
            "states": [{"id": uuid(10 + i), "name": name} for i, name in enumerate(STATES)],
            "members": [
                {"email": "me@example.com", "active": True},
                {"email": "gone@example.com", "active": False},
            ],
            "projects": [{"id": uuid(30), "name": "Widget"}],
        }
    ],
    "labels": [
        label(40, "Tasks"),
        label(41, "build", 40),
        label(42, "light-build", 40),
        label(43, "Agent"),
        label(44, "claude", 43),
        label(45, "bug"),
    ],
}
PROJECT_Q = r"GitHub repo of Linear project Widget \(<owner>/<name>, empty to skip\):"
DOCS_Q = r"GitHub repo for the board's documents, research reports and PRDs \(<owner>/<name>\):"
DOCS = ((DOCS_Q, "acme/docs"), (r"Its branch \[main\]:", ""))
BOARD = ((PROJECT_Q, "acme/widget"), *DOCS, (WRITE_Q, "y"), (WRITE_Q, "y"))
STATE_KEYS = ("backlog", "todo", "in_progress", "in_review", "handoff", "done", "canceled")
HAND_CONFIG = (
    f'[orchestrator]\ntracker = "linear"\n\n[orchestrator.linear]\nteam = "{uuid(1)}"\n'
    f'task_label_group = "{uuid(40)}"\n\n'
    "[orchestrator.linear.states]\n"
    + "".join(f'{key} = "{uuid(10 + i)}"\n' for i, key in enumerate(STATE_KEYS))
    + "".join(app_table(slot) for slot in SLOTS)
    + "".join(
        f'\n[delegate.roles.{role}.output]\ntype = "github"\nrepo = "acme/docs"\nbranch = "main"\ndir = "{d}"\n'
        for role, d in (("researcher", "Research/"), ("pm", "Product Design/"))
    )
)
Result = namedtuple("Result", "code stdout stderr tty")
ITEMS = ("claude", "codex", "opencode", "docker", "board")


def menu(want, have=("claude", "codex")):
    """The checklist keys that turn the ticked items `have` into `want`: space where they differ, down to the
    next item; the answer's newline confirms."""
    return "".join((" " if (i in want) != (i in have) else "") + "\x1b[B" for i in ITEMS)


def executable(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)


class Tty:
    """A pty whose slave is the script's terminal; answers go in as each prompt appears."""

    def __init__(self):
        self.master, self.slave = os.openpty()
        self.path = os.ttyname(self.slave)
        self.text, self.done = "", False
        self.lock = threading.Lock()
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()

    def read(self):
        while not self.done:
            try:
                ready, _, _ = select.select([self.master], [], [], 0.1)
                if not ready:
                    continue
                chunk = os.read(self.master, 4096)
            except OSError:
                time.sleep(0.05)
                continue
            with self.lock:
                self.text += chunk.decode(errors="replace")

    def answer(self, answers, proc):
        position = 0
        for pattern, reply in answers:
            deadline = time.monotonic() + 30
            while True:
                with self.lock:
                    found = re.compile(pattern).search(self.text, position)
                if found:
                    position = found.end()
                    os.write(self.master, (reply + "\n").encode())
                    break
                if proc.poll() is not None or time.monotonic() > deadline:
                    return f"no prompt {pattern!r} in terminal output:\n{self.text}"
                time.sleep(0.02)
        return None

    def close(self):
        self.done = True
        os.close(self.slave)
        os.close(self.master)
        self.reader.join(timeout=5)


class InstallTest(Case):
    def test_terminal_reader_stops_while_slave_still_open(self):
        terminal = Tty()
        held = os.dup(terminal.slave)
        try:
            terminal.close()
            self.assertFalse(terminal.reader.is_alive())
        finally:
            os.close(held)
            terminal.reader.join(timeout=5)

    def setUp(self):
        self.bin, self.fakes, self.root = (Path(self.home, n) for n in ("bin", "fakes", "root"))
        self.bin.mkdir()
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for tool in REAL:
            (self.bin / tool).symlink_to(shutil.which(tool))
        (self.bin / "python3").symlink_to(sys.executable)
        wrapper = '#!/bin/sh\nexec {} -I {} {} "$@"\n'
        for tool in FAKED:
            executable(self.bin / tool, wrapper.format(sys.executable, FAKE_TOOLS, tool))
        for app in APPS:
            executable(self.fakes / "apps" / app, wrapper.format(sys.executable, FAKE_TOOLS, app))
        (self.fakes / "sudo_password").write_text(PASSWORD)
        (self.fakes / "claude_token").write_text(TOKEN)
        (self.fakes / "tty_name").write_text("ttys001\n")
        (self.fakes / "commit").write_text(OLD)
        (self.root / "dev").mkdir(parents=True)
        # A test machine's terminal has Full Disk Access (install-cleanup.sh reads TCC.db); its tests revoke it.
        self.tcc = Path(self.home, "Library/Application Support/com.apple.TCC/TCC.db")
        self.tcc.parent.mkdir(parents=True)
        self.tcc.write_text("granted")
        self.pkg_src = self.fakes / "pkg-src"
        (self.pkg_src / "scripts").mkdir(parents=True)
        shutil.copyfile(
            Path(REPO, "scripts/install_linear_app.py"),
            self.pkg_src / "scripts/install_linear_app.py",
        )
        executable(self.pkg_src / "src/broker/install.sh", BROKER_INSTALL)
        self.versions = self.pkg_src / "src/broker/internal/codex/versions.go"
        self.versions.parent.mkdir(parents=True)
        self.versions.write_text('package codex\n\nconst MinVersion = "0.163.0"\n')
        launcher = self.pkg_src / "src/broker/internal/launcher/versions.go"
        launcher.parent.mkdir(parents=True)
        launcher.write_text('package launcher\n\nconst opencodeMinVersion = "1.19.3"\n')
        executable(self.pkg_src / "skills/manage/scripts/ctmux", CTMUX)
        (self.pkg_src / "skills/manage/scripts/launcher").write_text("#!/bin/sh\nexit 0\n")
        (self.pkg_src / ".claude-plugin").mkdir()
        (self.pkg_src / ".claude-plugin/plugin.json").write_text('{"version": "9.8.7"}\n')
        shutil.copytree(Path(REPO, "launchd"), self.pkg_src / "launchd")
        for source in (CLIENT, GO):
            (self.pkg_src / source).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(Path(REPO, source), self.pkg_src / source)
        self.serve()
        flow = "src/ensemble/orchestrator/engine/flow"
        (self.pkg_src / flow).mkdir(parents=True)
        charters = "src/ensemble/delegate/team/roles"
        (self.pkg_src / charters).mkdir(parents=True)
        # copyfile: CI makes the shipped source read-only, and the cleanup must delete these copies
        for source in [Path(REPO, flow, "workflow.toml"), *Path(REPO, charters).glob("*.md")]:
            shutil.copyfile(source, self.pkg_src / source.relative_to(REPO))
        self.log, self.envlog = Path(self.home, "calls.log"), Path(self.home, "env.log")
        self.env = {
            "HOME": self.home,
            "PATH": str(self.bin),
            "SHELL": "/bin/zsh",
            "LOG": str(self.log),
            "ENVLOG": str(self.envlog),
            "FAKES": str(self.fakes),
            "TMPDIR": str(self.tmp),
            "ENSEMBLE_INSTALL_ROOT": str(self.root),
        }

    def serve(self, claude=None, codex=None):
        """The windroc marketplace's manifests: ensemble's source for Claude Code and Codex
        (a dict merged into the url source, or a list of plugins; None: no ref, the default branch)."""
        url = {"source": "url", "url": "https://github.com/windroc-labs/ensemble.git"}
        for path, source in (
            (".claude-plugin/marketplace.json", claude),
            (".agents/plugins/marketplace.json", claude if codex is None else codex),
        ):
            plugins = source if isinstance(source, list) else None
            plugins = plugins or [{"name": "ensemble", "source": {**url, **(source or {})}}]
            manifest = self.fakes / "marketplace" / path
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(json.dumps({"name": "windroc", "plugins": plugins}))

    def at_ref(self, ref, path, text):
        """Ensemble's file `path` at `ref` on GitHub; None: unreadable."""
        target = self.fakes / "refs" / ref / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if text is None:
            target.with_name(target.name + ".missing").write_text("")
        else:
            target.write_text(text)

    def protocol_at(self, ref, protocol):
        """Ensemble at `ref` with both sides at `protocol`."""
        self.at_ref(ref, CLIENT, f'PROTOCOL = "{protocol}"\n')
        self.at_ref(ref, GO, f'package protocol\n\nconst Version = "{protocol}"\n')

    def refused(self, *args, rerun=False, then=()):
        """A run of `args` that stops before any client step: the sudo password, then on a first
        run the brew profile line, then `then` (the configuration's)."""
        answers = [(SUDO_Q, PASSWORD), *(() if rerun else ((WRITE_Q, "y"),)), *then]
        result = self.run_install(*args, answers=answers)
        self.assertEqual(result.code, 1, result.stderr)
        self.assertEqual(self.plugin_calls(), [])
        return result

    def run_install(self, *args, answers=(), stdin=None, tty=True, script=SCRIPT, ctty=None):
        """`ctty`: what /dev/tty opens, else the terminal, which is also /dev/ttys001."""
        self.log.unlink(missing_ok=True)
        terminal = Tty() if tty else None
        links = (self.root / "dev/tty", self.root / "dev/ttys001")
        for link in links:
            link.unlink(missing_ok=True)
        if terminal:
            links[0].symlink_to(ctty or terminal.path)
            links[1].symlink_to(terminal.path)
        out, err = Path(self.home, "stdout"), Path(self.home, "stderr")
        try:
            with out.open("w") as stdout, err.open("w") as stderr:
                proc = subprocess.Popen(
                    ["bash", str(script), *args],
                    env=self.env,
                    stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    text=True,
                )
                if stdin is not None:
                    proc.stdin.write(stdin)
                    proc.stdin.close()
                missed = terminal.answer(answers, proc) if terminal else None
                try:
                    proc.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                    missed = missed or "install.sh hung (waiting on the terminal?)"
            text = terminal.text if terminal else ""
        finally:
            if terminal:
                terminal.close()
        result = Result(proc.returncode, out.read_text(), err.read_text(), text)
        if missed:
            self.fail(f"{missed}\nstderr:\n{result.stderr}\nterminal:\n{result.tty}")
        return result

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def run_calls(self):
        """The calls, sorted, but the keep-alive's and the terminal lookup's (its pid varies):
        pipelines and the keep-alive interleave.
        """
        return sorted(
            c for c in self.calls() if c != "sudo -n -v" and not c.startswith("ps -o tty= -p ")
        )

    def state(self):
        path = Path(self.ensemble, "install/state")
        lines = path.read_text().splitlines() if path.exists() else []
        return dict(line.split("=", 1) for line in lines)

    def assert_ok(self, result):
        self.assertEqual(result.code, 0, f"stderr:\n{result.stderr}\nterminal:\n{result.tty}")

    def assert_secret_free(self, *secrets, result):
        kept = {
            self.fakes / "keychain",
            self.fakes / "src/broker/claude_token",
            self.fakes / "linear.json",
        }
        kept |= {self.fakes / "sudo_password", self.fakes / "claude_token", self.auth_json()}
        texts = {"stdout": result.stdout, "stderr": result.stderr}
        for path in Path(self.home).rglob("*"):
            if (
                path.is_file()
                and not path.is_symlink()
                and not any(path == k or k in path.parents for k in kept)
            ):
                texts[str(path)] = path.read_text(errors="replace")
        for secret in secrets:
            for name, text in texts.items():
                self.assertNotIn(secret, text, name)

    def fresh(
        self,
        *args,
        clients="",
        docker="n",
        opencode=(),
        logins=None,
        ctty=None,
        then=(),
        provider_write=True,
        egress_failure=False,
    ):
        """A first run answering every question: brew profile line, clients, Docker, the
        OpenCode provider, the config, the Docker logins, the PATH line, then `then` (the board's).
        """
        chosen = args[args.index("--clients") + 1] if "--clients" in args else clients
        if logins is None and (docker == "y" or "--docker" in args):
            logins = [(TOKEN_Q, TOKEN)] if "claude" in (chosen or "claude") else []
            logins.append((PEM_Q, ""))
        answers = [(SUDO_Q, PASSWORD), (WRITE_Q, "y")]
        if "--clients" not in args or not {"--docker", "--no-docker"} & set(args):
            on = "--docker" in args or ("--no-docker" not in args and docker == "y")
            have = ("claude", "codex", *(("board",) if "--board" in args else ()))
            if "--clients" in args:
                have = chosen.split(",")
            if "--docker" in args:
                have = (*have, "docker")
            want = (*(chosen or "claude,codex").split(","), *(("docker",) if on else ()))
            want = (*want, *(("board",) if "--board" in args else ()))
            answers.append((MENU_Q, menu(want, have)))
        plugin = [(WRITE_Q, "y")] if "opencode" in (chosen or "") else []
        provider = plugin if provider_write else []
        answers += [
            *opencode,
            (WRITE_Q, "y"),
            *plugin,
            *provider,
            *((logins or ()) if not egress_failure else ()),
            *([(WRITE_Q, "y")] if not egress_failure else []),
            *([(WRITE_Q, "y")] if "opencode" in (chosen or "") and not egress_failure else []),
            *then,
        ]
        return self.run_install(*args, answers=answers, ctty=ctty)

    def again(self, *args, answers=()):
        """A rerun: the checklist, unless flags answer it, confirmed as the last run left it."""
        flagged = "--clients" in args and {"--docker", "--no-docker"} & set(args)
        shown = [] if flagged else [(MENU_Q, "")]
        if self.state().get("board") == "yes" and {"--launchd", "--no-launchd"} & set(args):
            return self.run_install(*args, answers=[*shown, (SUDO_Q, PASSWORD), *answers])
        return self.run_install(*args, answers=[(SUDO_Q, PASSWORD), *shown, *answers])

    def config(self):
        return Path(self.ensemble, "config.local.toml")

    def linear(self, **changes):
        """The fake Linear workspace: LINEAR with `changes`."""
        (self.fakes / "linear.json").write_text(json.dumps({**LINEAR, **changes}))

    def secrets(self, *slots, config=None):
        """Hidden credentials, then each missing public OAuth Client ID."""
        if config is None:
            config = self.config().read_text() if self.config().exists() else ""
        data = tomllib.loads(config)
        linear = data.get("orchestrator", {}).get("linear", {})
        answers = []
        for slot in slots:
            answers.append((SECRET_Q, f"k-SENTINEL-{slot}"))
            app = (
                linear.get(slot, {})
                if slot in ("harness", "manager")
                else linear.get("roles", {}).get(slot, {})
            )
            if not app.get("client_id"):
                answers.append((f"Linear OAuth Client ID for {slot}:", f"client-{slot}"))
        return answers

    def configured(self, text, then=(), board="linear"):
        """A first run with Board ticked over a config.local.toml of `text` that already has the
        configuration: brew profile line, the checklist, the PATH line, then `then`."""
        Path(self.ensemble).mkdir(parents=True, exist_ok=True)
        self.config().write_text(text)
        ticked = ("claude", "codex", "board")
        answers = [
            (SUDO_Q, PASSWORD),
            (WRITE_Q, "y"),
            (MENU_Q, menu(ticked, ticked)),
            (WRITE_Q, "y"),
        ]
        return self.run_install("--board", board, "--launchd", answers=[*answers, *then])

    def plugin_calls(self):
        return [c for c in self.calls() if " plugin " in c and " list " not in c]

    def broker_calls(self):
        own = {"sudo -n true", "sudo -n -v", "sudo -k", "sudo -S -p  -v", "sudo -A -E -- true"}
        wanted = ("broker-", "sudo", "ensemble-broker")
        return [
            c
            for c in self.calls()
            if c.startswith(wanted)
            and c not in own
            and "status" not in c
            and c != "ensemble-broker version"
            and "/Docker.app/" not in c
        ]

    def broker_path(self):
        return Path(self.root, "usr/local/libexec/ensemble/ensemble-broker")

    def auth_json(self):
        return Path(self.home, ".local/share/opencode/auth.json")

    def test_usage_and_platform(self):
        result = self.run_install("--help", tty=False)
        self.assertEqual(result.code, 0)
        self.assertIn("usage: scripts/install.sh [--ref <ref>]", result.stdout)
        cases = (
            ["--bogus"],
            ["--ref"],
            ["--ref", "-x"],
            ["--docker", "--no-docker"],
            ["--clients", "claude,,codex"],
            ["--clients", "claude,vim"],
            ["--opencode-provider", "ollama"],
            ["--opencode-provider", "lan.1"],
            ["--opencode-url", 'http://h"x/v1'],
            ["--opencode-url", "http://user:key@host/v1"],
            ["--opencode-url", "http://host/api"],
            ["--opencode-model", "a b"],
            ["--opencode-context", "0"],
            ["--opencode-context", "64k"],
        )
        for args in cases:
            result = self.run_install(*args, tty=False)
            self.assertEqual(result.code, 2, args)
            self.assertIn("install.sh: usage: scripts/install.sh [--ref <ref>]", result.stderr)
        tty = Tty()
        try:
            for flag in ("--password-stdin", "--opencode-key-stdin"):
                with open(tty.path) as stdin:
                    proc = subprocess.run(
                        ["bash", str(SCRIPT), flag],
                        env=self.env,
                        stdin=stdin,
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                self.assertEqual(proc.returncode, 2, flag)
                self.assertIn("read a pipe, not a terminal", proc.stderr)
        finally:
            tty.close()
        (self.fakes / "platform").write_text("Linux x86_64\n")
        result = self.run_install(tty=False)
        self.assertEqual(result.code, 1)
        self.assertIn(
            "needs macOS on Apple silicon (Darwin arm64), not Linux x86_64", result.stderr
        )
        (self.fakes / "platform").unlink()
        result = self.run_install(tty=False)
        self.assertEqual(result.code, 1)
        self.assertIn("install.sh: needs a terminal for its questions and logins", result.stderr)
        self.assertEqual(self.calls(), ["uname -sm"])

    def test_password_stdin(self):
        result = self.run_install(
            "--password-stdin",
            "--no-docker",
            "--clients",
            "claude,codex",
            stdin=PASSWORD + "\n",
            answers=[(WRITE_Q, "y"), (WRITE_Q, "y"), (WRITE_Q, "y")],
        )
        self.assert_ok(result)
        self.assertNotRegex(result.tty, SUDO_Q)
        calls = self.calls()
        self.assertIn("sudo -S -p  -v", calls)
        self.assertNotIn("sudo -v", calls)
        self.assert_secret_free(PASSWORD, result=result)

    def test_wrong_password_on_stdin_fails(self):
        result = self.run_install("--password-stdin", "--no-docker", stdin="wrong\n")
        self.assertEqual(result.code, 1)
        self.assertIn("install.sh: sudo rejected the password from stdin", result.stderr)
        self.assertNotIn("brew", "\n".join(self.calls()))

    def test_sudo_wrong_password_asked_again(self):
        result = self.run_install(
            "--no-docker",
            "--clients",
            "claude",
            answers=[(SUDO_Q, "wrong"), (SUDO_Q, PASSWORD), *[(WRITE_Q, "y")] * 3],
        )
        self.assert_ok(result)
        self.assertEqual(result.tty.count("Sorry, try again."), 1)
        self.assertNotIn(PASSWORD, result.tty)

    def test_cask_sudo_gets_the_password_from_memory(self):
        """Homebrew's sudo for docker-desktop misses the ticket; the askpass FIFO answers it."""
        result = self.run_install(
            "--password-stdin",
            "--docker",
            "--clients",
            "claude",
            stdin=PASSWORD + "\n",
            answers=[(WRITE_Q, "y"), (WRITE_Q, "y"), *DOCKER_LOGINS, (WRITE_Q, "y")],
        )
        self.assert_ok(result)
        self.assertIn("brew install --cask docker-desktop", self.calls())
        self.assertTrue((self.fakes / "askpass_used").exists())
        self.assertNotRegex(result.tty, SUDO_Q)
        self.assertEqual(list(self.tmp.iterdir()), [])
        self.assert_secret_free(PASSWORD, TOKEN, result=result)
        self.assertNotIn(PASSWORD, self.envlog.read_text())

    def test_sudo_prompt_once(self):
        result = self.fresh()
        self.assert_ok(result)
        self.assertEqual(len(re.findall(SUDO_Q, result.tty)), 1)
        calls = self.calls()
        self.assertEqual(calls.count("sudo -S -p  -v"), 1)
        self.assertIn("sudo -n -v", calls)
        self.assertEqual(calls[-1], "sudo -k")
        keepalive = int((self.fakes / "keepalive.pid").read_text())
        deadline = time.monotonic() + 2
        with self.assertRaises(ProcessLookupError):
            while time.monotonic() < deadline:
                os.kill(keepalive, 0)
                time.sleep(0.05)
        self.assertFalse((self.fakes / "sudo_ticket").exists())

    def test_fresh_install_core(self):
        result = self.fresh()
        self.assert_ok(result)
        calls = self.calls()
        installer = calls.index(
            "curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh"
        )
        self.assertLess(calls.index("sudo -S -p  -v"), installer)
        brew = [c for c in calls if c.startswith("brew install")]
        self.assertEqual(
            brew,
            [
                "brew install gh",
                "brew install tmux",
                "brew install git",
                "brew install --cask iterm2",
                "brew install --cask claude-code",
                "brew install --cask codex",
            ],
        )
        tty = result.tty
        self.assertLess(tty.index("Write it?"), tty.index("Choose what to install"))
        self.assertLess(tty.index("Choose what to install"), tty.index("config.local.toml:"))
        logins = [c for c in calls if " login" in c or c.endswith(" status")]
        self.assertEqual(
            logins,
            [
                "gh auth status",
                "gh auth login --hostname github.com --git-protocol https --web",
                "claude auth status",
                "claude auth login",
                "codex login status",
                "codex login",
            ],
        )
        self.assertIn("python3 is 3.11+", result.stderr)
        profile = Path(self.home, ".zprofile").read_text()
        self.assertIn(f'eval "$({self.root}/opt/homebrew/bin/brew shellenv)"\n', profile)
        self.assertTrue(result.stderr.endswith(RELOAD + "\n"), result.stderr)
        self.assertIn("| |___| | | \\__ \\  __/ | | | | | |_) | |  __/\n", result.stderr)
        self.assertNotIn("| (_| | (_| |  __/ | | | ||_____| |_) | | | | | |", result.stderr)
        self.assertIn(
            "ensemble is installed (ref main, clients claude,codex, Docker no).\n"
            "Welcome onboard, and congrats: your crew is ready.",
            result.stderr,
        )
        self.assertIn("Log in to gh in your browser.", result.tty)
        self.assertEqual(self.state()["docker"], "no")
        self.assertEqual(self.state()["clients"], "claude,codex")

    def test_no_reload_when_the_path_has_it(self):
        brew = f"{self.root}/opt/homebrew/bin"
        self.env["PATH"] = f"{brew}:{self.ensemble}/bin:{self.bin}"
        result = self.fresh()
        self.assert_ok(result)
        self.assertNotIn("run source", result.stderr)
        self.env["PATH"] = f"{brew}:{self.bin}"
        result = self.again()
        self.assert_ok(result)
        self.assertTrue(result.stderr.endswith(RELOAD + "\n"), result.stderr)

    def test_checklist(self):
        """j/k move too; no client ticked asks again; a first run leaves the board unticked, which skips its steps."""
        keys = " j jjj"
        result = self.run_install(
            answers=[
                (SUDO_Q, PASSWORD),
                (WRITE_Q, "y"),
                (MENU_Q, keys),
                (r"Tick at least one client\.", "kkkk "),
                (WRITE_Q, "y"),
                (WRITE_Q, "y"),
            ]
        )
        self.assert_ok(result)
        self.assertEqual(
            (self.state()["clients"], self.state()["docker"], self.state()["board"]),
            ("codex", "no", "no"),
        )
        self.assertIn("  [ ] Board: local or Linear", result.tty)
        self.assertIn("==> board launchd jobs\n    skip: board not chosen", result.stderr)
        self.assertIn("==> Linear apps\n    skip: board not chosen", result.stderr)

    def test_checklist_skipped_by_flags(self):
        result = self.fresh("--clients", "claude", "--no-docker", "--no-board")
        self.assert_ok(result)
        self.assertNotRegex(result.tty, MENU_Q)
        self.assertEqual(self.state()["board"], "no")
        self.assertEqual(
            self.run_install("--board", "linear", "--launchd", "--no-board", tty=False).code, 2
        )

    def test_answers_kept_and_flags_override(self):
        self.assert_ok(self.fresh(clients="claude", docker="y"))
        self.assertEqual((self.state()["clients"], self.state()["docker"]), ("claude", "yes"))
        self.assertEqual(self.state()["board"], "no")
        self.assertNotIn("brew install --cask codex", self.calls())
        result = self.again()
        self.assert_ok(result)
        lines = (
            "> [x] Claude Code",
            "  [ ] Codex",
            "  [ ] OpenCode",
            "  [x] Docker workers",
            "  [ ] Board",
        )
        for line in lines:
            self.assertIn(line, result.tty)
        result = self.again("--clients", "claude,codex")
        self.assert_ok(result)
        self.assertEqual(self.state()["clients"], "claude,codex")
        self.assertIn("brew install --cask codex", self.calls())
        self.assertIn("codex plugin add ensemble@windroc", self.calls())
        result = self.again("--no-docker", answers=[(WRITE_Q, "y")])
        self.assert_ok(result)
        self.assertEqual(self.state()["docker"], "no")
        self.assertIn('default = "tmux"', self.config().read_text())
        Path(self.ensemble, "install/state").unlink()
        self.config().write_text(
            '[session.hosts]\ndefault = "docker"\n[delegate]\nclient = "codex"\n'
        )
        result = self.again(answers=[(WRITE_Q, "y"), (PEM_Q, "")])
        self.assert_ok(result)
        self.assertIn("> [ ] Claude Code\r\n\r\x1b[K  [x] Codex", result.tty)
        self.assertEqual((self.state()["clients"], self.state()["docker"]), ("codex", "yes"))

    def test_config_template_and_edit(self):
        self.assert_ok(self.fresh("--docker", "--clients", "codex"))
        self.assertEqual(
            self.config().read_text(),
            "# Written by install.sh. Keys and defaults: each layer's packaged config.toml "
            "(its `# local:` lines);\n# the Linear board: docs/board.md#setup.\n"
            '[session.hosts]\ndefault = "docker"\n\n[delegate]\nclient = "codex"\n\n'
            '[delegate.delegates]\nharness_host = "tmux"\n',
        )
        cases = (
            (
                '# mine\n[session.hosts]\ndefault = "tmux" # x\n\n[other]\nx = 1\n',
                '# mine\n[session.hosts]\ndefault = "docker"\n\n[other]\nx = 1\n\n'
                '[delegate]\nclient = "codex"\n\n'
                '[delegate.delegates]\nharness_host = "tmux"\n',
            ),
            (
                '[delegate.delegates]\n\n[session.hosts]\ndefault = "docker"\n[delegate]\nclient = "codex"\n',
                '[delegate.delegates]\nharness_host = "tmux"\n\n[session.hosts]\ndefault = "docker"\n'
                '[delegate]\nclient = "codex"\n',
            ),
        )
        for before, after in cases:
            self.config().write_text(before)
            result = self.again(answers=[(WRITE_Q, "y")])
            self.assert_ok(result)
            self.assertEqual(self.config().read_text(), after)
        dotted = 'session.hosts.default = "tmux"\ndelegate.client = "codex"\n'
        self.config().write_text(dotted)
        result = self.again()
        self.assert_ok(result)
        self.assertIn("skip: set it by hand", result.stderr)
        self.assertIn('[session.hosts] default = "docker"', result.stderr)
        self.assertEqual(self.config().read_text(), dotted)
        self.config().write_text("[broken\n")
        result = self.again()
        self.assertEqual(result.code, 1)
        self.assertIn("config.local.toml failed", result.stderr)
        self.assertEqual(self.config().read_text(), "[broken\n")

    def test_declined_writes_nothing(self):
        result = self.run_install(
            "--no-docker",
            answers=[
                (SUDO_Q, PASSWORD),
                (WRITE_Q, "n"),
                (MENU_Q, ""),
                (WRITE_Q, "n"),
                (WRITE_Q, "n"),
            ],
        )
        self.assert_ok(result)
        self.assertEqual(result.stderr.count("skip: declined"), 3)
        self.assertFalse(Path(self.home, ".zprofile").exists())
        self.assertNotIn("run source", result.stderr)
        self.assertFalse(self.config().exists())

    def test_codex_only(self):
        result = self.fresh("--no-docker", clients="codex")
        self.assert_ok(result)
        calls = "\n".join(self.calls())
        for absent in ("claude-code", "claude auth", "claude plugin"):
            self.assertNotIn(absent, calls)
        pkg = Path(self.home, ".codex/plugins/cache/windroc/ensemble/9.8.7")
        self.assertIn(f"    {pkg}", result.stderr)
        self.assertIn("ctmux --install", self.calls())
        self.assertIn('[delegate]\nclient = "codex"\n', self.config().read_text())
        self.assertNotIn("api.github.com", calls)

    def test_claude_only(self):
        result = self.fresh(clients="claude", docker="y")
        self.assert_ok(result)
        calls = "\n".join(self.calls())
        for absent in (
            "--cask codex",
            "codex login",
            "codex plugin",
            "install-codex",
            "login codex",
        ):
            self.assertNotIn(absent, calls)
        self.assertIn("==> broker Codex install\n    skip: Codex not chosen", result.stderr)
        self.assertIn("ensemble-broker login claude", calls)
        self.assertNotIn("[delegate]", self.config().read_text())

    def test_ref_with_claude_or_codex_must_be_the_served_ref(self):
        self.serve({"ref": "v9.8.7"})
        flags = ("--clients", "claude,codex", "--no-docker")
        result = self.fresh("--ref", "v9.8.7", *flags)
        self.assert_ok(result)
        self.assertIn("ensemble is installed (ref v9.8.7, clients claude,codex", result.stderr)
        result = self.refused("--ref", "orchestrator-revamped", *flags, rerun=True)
        self.assertIn(
            "install.sh: --ref orchestrator-revamped: Claude Code and Codex install ensemble@windroc"
            " from the windroc marketplace, which serves ensemble at v9.8.7; use --ref v9.8.7,"
            " or --clients opencode for another ref\n",
            result.stderr,
        )

    def test_served_ref_is_the_default(self):
        self.serve({"ref": "v9.8.7"})
        result = self.fresh(clients="claude")
        self.assert_ok(result)
        self.assertIn(
            "ensemble is installed (ref v9.8.7, clients claude, Docker no)", result.stderr
        )
        market = "https://raw.githubusercontent.com/windroc-labs/agent-plugins-marketplace/main/"
        self.assertIn(f"curl -fsSL {market}.claude-plugin/marketplace.json", self.calls())
        self.assertNotIn(f"curl -fsSL {market}.agents/plugins/marketplace.json", self.calls())

    def test_served_refs_must_agree(self):
        self.serve({"ref": "v9.8.7"}, {"ref": "v9.8.6"})
        result = self.refused("--clients", "claude,codex", "--no-docker")
        self.assertIn(
            "install.sh: the windroc marketplace serves ensemble at v9.8.7 to Claude Code and"
            " v9.8.6 to Codex: they must match\n",
            result.stderr,
        )

    def test_invalid_served_ref_or_sha_pin_stops(self):
        flags = ("--clients", "claude", "--no-docker")
        cases = (
            ({"ref": "../../x/y/main"}, "not a git ref: '../../x/y/main'"),
            ({"ref": "v1#x"}, "not a git ref: 'v1#x'"),
            ({"ref": "-x"}, "not a git ref: '-x'"),
            ({"ref": "v1\nx"}, "not a git ref: 'v1\\nx'"),
            ({"ref": 7}, "not a git ref: 7"),
            ({"sha": OLD}, "pinned by sha, not a release tag"),
            ([{"name": "updraft", "source": "./"}], "no ensemble entry"),
        )
        for rerun, (source, reason) in enumerate(cases):
            with self.subTest(source=source):
                self.serve(source)
                result = self.refused(*flags, rerun=bool(rerun))
                self.assertIn(
                    "install.sh: cannot read ensemble's ref from the windroc marketplace"
                    f" (.claude-plugin/marketplace.json): {reason}\n",
                    result.stderr,
                )

    def test_unreadable_marketplace_stops(self):
        (self.fakes / "marketplace/.agents/plugins/marketplace.json").unlink()
        result = self.refused("--clients", "codex", "--no-docker")
        self.assertIn(
            "install.sh: cannot read ensemble's ref from the windroc marketplace"
            " (.agents/plugins/marketplace.json)\n",
            result.stderr,
        )

    def test_installs_from_the_windroc_marketplace(self):
        self.assert_ok(self.fresh())
        self.assertEqual(
            self.plugin_calls(),
            [
                "claude plugin marketplace add windroc-labs/agent-plugins-marketplace",
                "claude plugin install ensemble@windroc",
                "claude plugin install updraft@windroc",
                "codex plugin marketplace add windroc-labs/agent-plugins-marketplace",
                "codex plugin add ensemble@windroc",
                "codex plugin add updraft@windroc",
            ],
        )
        self.assert_ok(self.again())
        self.assertEqual(self.plugin_calls(), [])
        result = self.again("--ref", "next")
        self.assertEqual(result.code, 1)
        self.assertEqual(self.plugin_calls(), [])

    def test_disabled_plugin_is_enabled(self):
        self.assert_ok(self.fresh())
        plugins = Path(self.fakes, "claude/plugins.json")
        plugins.write_text(plugins.read_text().replace("true", "false"))
        self.assert_ok(self.again())
        self.assertEqual(
            self.plugin_calls(),
            [
                "claude plugin enable ensemble@windroc",
                "claude plugin enable updraft@windroc",
            ],
        )

    def test_docker_installs_only_ensemble_plugins_on_host(self):
        self.assert_ok(self.fresh(docker="y"))
        self.assertEqual(
            self.plugin_calls(),
            [
                "claude plugin marketplace add windroc-labs/agent-plugins-marketplace",
                "claude plugin install ensemble@windroc",
                "codex plugin marketplace add windroc-labs/agent-plugins-marketplace",
                "codex plugin add ensemble@windroc",
            ],
        )

    def test_docker_does_not_enable_existing_disabled_updraft(self):
        self.assert_ok(self.fresh())
        for client in ("claude", "codex"):
            path = Path(self.fakes, client, "plugins.json")
            path.write_text(path.read_text().replace("true", "false"))
        self.assert_ok(
            self.again("--docker", answers=[(WRITE_Q, "y"), (TOKEN_Q, TOKEN), (PEM_Q, "")])
        )
        self.assertNotIn("updraft", "\n".join(self.plugin_calls()))

    def test_docker_keeps_an_explicit_image_plugin_list(self):
        self.assert_ok(self.fresh(clients="claude", docker="y"))
        self.config().write_text(
            self.config().read_text()
            + '\n[session.hosts.docker]\nplugins = ["custom@owner/repo"]\n'
        )
        self.assert_ok(self.again("--clients", "codex", "--docker", answers=[(WRITE_Q, "y")]))
        self.assertIn('plugins = ["custom@owner/repo"]', self.config().read_text())

    def test_fresh_install(self):
        result = self.fresh(docker="y")
        self.assert_ok(result)
        calls = self.calls()
        self.assertIn("brew install --cask docker-desktop", calls)
        self.assertNotIn("brew install go", calls)
        broker = str(self.broker_path())
        self.assertEqual(
            self.broker_calls(),
            [
                "broker-install ",
                f"sudo mkdir -p {self.root}/usr/local/libexec/ensemble",
                "ensemble-broker login claude",
                f"sudo -A {broker} install-codex 0.163.0",
                "ensemble-broker install-codex 0.163.0",
                "ensemble-broker login codex",
            ],
        )
        self.assertLess(calls.index("open -a Docker"), calls.index("broker-install "))
        self.assertLess(
            calls.index("claude setup-token"), calls.index("ensemble-broker login claude")
        )
        self.assertEqual((self.fakes / "src/broker/claude_token").read_text(), TOKEN)
        self.assertIn(
            "skip: no GitHub App key: create the App by docs/docker-host.md#create-the-github-app",
            result.stderr,
        )
        state = self.state()
        self.assertEqual(state["github_app_key"], "skipped")
        self.assertEqual(len(state["login_claude"]), 64)
        self.assertNotIn("login_github", state)
        self.assert_secret_free(PASSWORD, TOKEN, result=result)

    def test_no_docker(self):
        result = self.fresh("--no-docker")
        self.assert_ok(result)
        for name in DOCKER_STEPS:
            self.assertIn(f"==> {name}\n    skip: Docker not chosen", result.stderr)
        calls = "\n".join(self.calls())
        for absent in (
            "docker-desktop",
            "broker",
            "open -a Docker",
            "setup-token",
            "brew install go",
            RAW,
        ):
            self.assertNotIn(absent, calls)
        self.assertFalse(Path(self.home, ".egress").exists())

    def test_egress_pinned_and_healthy_rerun_runs_start_and_keeps_checkout(self):
        self.assert_ok(self.fresh("--clients", "claude", "--docker"))
        egress = Path(self.home, ".egress")
        sha = "9ed47580dc9eb4c1fe794d12e54a8f2d3f258831"
        checkout = egress / "versions" / sha
        self.assertEqual((egress / "current").resolve(), checkout)
        config = egress / "config.local.yaml"
        self.assertEqual(
            config.read_text(), "version: 1\nnetworks:\n  workers:\n    mode: denylist\n"
        )
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in checkout.iterdir()}
        before[config] = (config.read_bytes(), config.stat().st_mtime_ns)
        self.assertIn(f"gh api repos/windroc/egress-proxy/tarball/{sha}", self.calls())
        self.assertIn("docker egress-start", self.calls())
        self.assert_ok(self.again("--clients", "claude", "--docker"))
        self.assertEqual({p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before}, before)
        # start.sh itself leaves an unchanged proxy running; the installer never restarts it
        self.assertIn("docker egress-start", self.calls())
        self.assertFalse(
            any(c.split()[:2] in (["docker", "restart"], ["docker", "rm"]) for c in self.calls())
        )
        self.assertFalse(any("tarball/" in c for c in self.calls()))

    def test_egress_rerun_applies_a_changed_or_recreated_config(self):
        self.assert_ok(self.fresh("--clients", "claude", "--docker"))
        config = Path(self.home, ".egress", "config.local.yaml")
        served = self.fakes / "egress_served"
        edited = "version: 1\nnetworks:\n  workers:\n    mode: denylist\n    deny_ranges: [203.0.113.7/32]\n"
        config.write_text(edited)
        self.assert_ok(self.again("--clients", "claude", "--docker"))
        self.assertEqual(served.read_text(), edited)
        self.assertEqual(config.read_text(), edited)
        config.unlink()
        self.assert_ok(self.again("--clients", "claude", "--docker"))
        default = "version: 1\nnetworks:\n  workers:\n    mode: denylist\n"
        self.assertEqual(config.read_text(), default)
        self.assertEqual(served.read_text(), default)

    def test_egress_existing_config_preserved(self):
        egress = Path(self.home, ".egress")
        egress.mkdir()
        target = egress / "config.local.yaml"
        target.write_text("# user policy\nversion: 1\nnetworks:\n  workers:\n    mode: denylist\n")
        target.chmod(0o600)
        before = (target.read_bytes(), target.stat().st_mode, target.stat().st_mtime_ns)
        self.assert_ok(self.fresh("--clients", "claude", "--docker"))
        self.assertEqual(
            (target.read_bytes(), target.stat().st_mode, target.stat().st_mtime_ns), before
        )

    def test_egress_bad_config_paths_are_not_replaced(self):
        egress = Path(self.home, ".egress")
        egress.mkdir()
        config = egress / "config.local.yaml"
        config.symlink_to(egress / "absent")
        result = self.fresh("--clients", "claude", "--docker", egress_failure=True)
        self.assertEqual(result.code, 1)
        self.assertTrue(config.is_symlink())
        self.assertIn("symlink is unsupported", result.stderr)
        self.assertNotIn("docker egress-start", self.calls())

    def test_egress_external_config_symlink_is_kept_and_rejected(self):
        egress = Path(self.home, ".egress")
        egress.mkdir()
        target = Path(self.home, "my-egress.yaml")
        target.write_text("# user config\n")
        config = egress / "config.local.yaml"
        config.symlink_to(target)
        before = (target.read_bytes(), target.stat().st_mode, target.stat().st_mtime_ns)
        result = self.fresh("--clients", "claude", "--docker", egress_failure=True)
        self.assertEqual(result.code, 1)
        self.assertIn("symlink is unsupported", result.stderr)
        self.assertTrue(config.is_symlink())
        self.assertEqual(
            (target.read_bytes(), target.stat().st_mode, target.stat().st_mtime_ns), before
        )
        self.assertNotIn("docker egress-start", self.calls())

    def test_egress_upgrade_keeps_old_version_and_publishes_current_only_when_healthy(self):
        old_sha = "0" * 40
        egress = Path(self.home, ".egress")
        old = egress / "versions" / old_sha
        old.mkdir(parents=True)
        (old / "COMMIT").write_text(old_sha + "\n")
        (old / "start.sh").write_text("# old checkout\n")
        before = {
            p: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns) for p in old.iterdir()
        }
        (egress / "current").symlink_to(old)
        state = Path(self.ensemble, "install/state")
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(f"egress_commit={old_sha}\n")
        (self.fakes / "fail").write_text("docker egress-start")
        result = self.fresh("--clients", "claude", "--docker", egress_failure=True)
        self.assertEqual(result.code, 1)
        self.assertEqual((egress / "current").resolve(), old)
        self.assertEqual(self.state()["egress_commit"], old_sha)
        (self.fakes / "egress_unhealthy").touch()
        (self.bin / "sleep").unlink()
        executable(self.bin / "sleep", "#!/bin/sh\n")
        result = self.again("--clients", "claude", "--docker")
        self.assertEqual(result.code, 1)
        self.assertIn("not healthy after 120 seconds", result.stderr)
        self.assertIn("docker egress-start", self.calls())
        self.assertEqual((egress / "current").resolve(), old)
        self.assertEqual(self.state()["egress_commit"], old_sha)
        self.assertEqual(
            {p: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns) for p in old.iterdir()},
            before,
        )
        (self.fakes / "egress_unhealthy").unlink()
        self.assert_ok(
            self.again("--clients", "claude", "--docker", answers=DOCKER_LOGINS + ((WRITE_Q, "y"),))
        )
        self.assertEqual(
            (egress / "current").resolve(),
            egress / "versions" / "9ed47580dc9eb4c1fe794d12e54a8f2d3f258831",
        )
        self.assertEqual(
            {p: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns) for p in old.iterdir()},
            before,
        )

    def test_egress_stopped_and_invalid_metadata_cannot_finish_installation(self):
        self.assert_ok(self.fresh("--clients", "claude", "--docker"))
        (self.bin / "sleep").unlink()
        executable(self.bin / "sleep", "#!/bin/sh\n")
        good = "true|healthy|1|egress-workers|http://egress-proxy-workers:4750|workers"
        for status in (
            good.replace("true", "false"),
            good.replace("healthy|1", "healthy|2"),
            good.replace("|egress-workers|", "||"),
            good.replace("http://egress-proxy-workers:4750", "<no value>"),
            "true|starting|1|||mini-op",
        ):
            with self.subTest(status=status):
                (self.fakes / "egress_inspect").write_text(status + "\n")
                result = self.again("--clients", "claude", "--docker")
                self.assertEqual(result.code, 1)
                self.assertIn("not healthy after 120 seconds", result.stderr)
                self.assertNotIn("ensemble-broker login claude", self.calls())

    def test_egress_config_without_workers_fails_at_once(self):
        egress = Path(self.home, ".egress")
        egress.mkdir()
        config = egress / "config.local.yaml"
        config.write_text("version: 1\nnetworks:\n  mini-op:\n    mode: allowlist\n")
        config.chmod(0o600)
        before = (config.read_bytes(), config.stat().st_mode, config.stat().st_mtime_ns)
        (self.fakes / "egress_inspect").write_text("true|healthy|1|||mini-op\n")
        slept = self.fakes / "slept"
        (self.bin / "sleep").unlink()
        real = shutil.which("sleep")
        executable(
            self.bin / "sleep", f'#!/bin/sh\n[ "$1" != 2 ] || touch \'{slept}\'\nexec {real} "$@"\n'
        )
        result = self.fresh("--clients", "claude", "--docker", egress_failure=True)
        self.assertEqual(result.code, 1)
        self.assertIn(f"{config} has no networks.workers section", result.stderr)
        self.assertIn("your config was kept unchanged", result.stderr)
        self.assertNotIn("not healthy after 120 seconds", result.stderr)
        self.assertFalse(slept.exists())
        self.assertEqual(
            (config.read_bytes(), config.stat().st_mode, config.stat().st_mtime_ns), before
        )
        self.assertNotIn("ensemble-broker login claude", self.calls())
        self.assertFalse((egress / "current").exists())

    def test_egress_download_failure_is_retryable(self):
        (self.fakes / "fail").write_text("gh api repos/windroc/egress-proxy/tarball/")
        result = self.fresh("--clients", "claude", "--docker", egress_failure=True)
        self.assertEqual(result.code, 1)
        self.assertIn("cannot fetch pinned windroc/egress-proxy", result.stderr)
        versions = Path(self.home, ".egress/versions")
        self.assertEqual(list(versions.iterdir()), [])
        self.assert_ok(
            self.again("--clients", "claude", "--docker", answers=DOCKER_LOGINS + ((WRITE_Q, "y"),))
        )

    def test_egress_unusable_download_leaves_no_version_and_is_retryable(self):
        versions = Path(self.home, ".egress/versions")
        (self.fakes / "egress_tarball").write_text("corrupt")
        result = self.fresh("--clients", "claude", "--docker", egress_failure=True)
        self.assertEqual(result.code, 1)
        self.assertIn("cannot unpack pinned windroc/egress-proxy", result.stderr)
        self.assertEqual(list(versions.iterdir()), [])
        (self.fakes / "egress_tarball").write_text("noexec")
        result = self.again("--clients", "claude", "--docker")
        self.assertEqual(result.code, 1)
        self.assertIn("pinned egress version has no executable start.sh", result.stderr)
        self.assertEqual(list(versions.iterdir()), [])
        self.assertNotIn("docker egress-start", self.calls())
        (self.fakes / "egress_tarball").unlink()
        self.assert_ok(
            self.again("--clients", "claude", "--docker", answers=DOCKER_LOGINS + ((WRITE_Q, "y"),))
        )

    def test_egress_incomplete_checkout_is_kept_and_rejected(self):
        version = Path(self.home, ".egress/versions/9ed47580dc9eb4c1fe794d12e54a8f2d3f258831")
        version.mkdir(parents=True)
        executable(version / "start.sh", "#!/bin/sh\n")
        (version / "COMMIT").write_text("0" * 40 + "\n")
        before = {p.name: p.read_bytes() for p in version.iterdir()}
        result = self.fresh("--clients", "claude", "--docker", egress_failure=True)
        self.assertEqual(result.code, 1)
        self.assertIn(f"incomplete egress checkout {version}", result.stderr)
        self.assertEqual({p.name: p.read_bytes() for p in version.iterdir()}, before)
        self.assertNotIn("docker egress-start", self.calls())
        self.assertFalse(any("tarball/" in c for c in self.calls()))

    def test_egress_config_directory_is_kept_and_rejected(self):
        config = Path(self.home, ".egress/config.local.yaml")
        config.mkdir(parents=True)
        (config / "keep").write_text("mine\n")
        result = self.fresh("--clients", "claude", "--docker", egress_failure=True)
        self.assertEqual(result.code, 1)
        self.assertIn(
            "config.local.yaml is not a readable file; existing path was kept", result.stderr
        )
        self.assertEqual((config / "keep").read_text(), "mine\n")
        self.assertNotIn("docker egress-start", self.calls())

    def test_egress_current_directory_is_kept_and_not_published(self):
        current = Path(self.home, ".egress/current")
        current.mkdir(parents=True)
        (current / "keep").write_text("mine\n")
        result = self.fresh("--clients", "claude", "--docker", egress_failure=True)
        self.assertEqual(result.code, 1)
        self.assertIn(f"{current} is not an installer symlink; kept it", result.stderr)
        self.assertFalse(current.is_symlink())
        self.assertEqual((current / "keep").read_text(), "mine\n")
        self.assertNotIn("egress_commit", self.state())
        self.assertNotIn("ensemble-broker login claude", self.calls())

    def test_egress_start_and_health_failures_stop_before_broker_logins(self):
        (self.fakes / "fail").write_text("docker egress-start")
        result = self.fresh("--clients", "claude", "--docker", egress_failure=True)
        self.assertEqual(result.code, 1)
        self.assertIn("egress-proxy", result.stderr)
        self.assertIn("docker logs egress-proxy", result.stderr)
        self.assertNotIn("ensemble-broker login claude", self.calls())
        (self.fakes / "egress_unhealthy").touch()
        (self.bin / "sleep").unlink()
        executable(self.bin / "sleep", "#!/bin/sh\n")
        result = self.again("--clients", "claude", "--docker")
        self.assertEqual(result.code, 1)
        self.assertIn("not healthy after 120 seconds", result.stderr)
        self.assertNotIn("ensemble-broker login claude", self.calls())

    def test_branch_broker_is_built(self):
        result = self.fresh(
            "--ref",
            "next",
            clients="opencode",
            docker="y",
            opencode=PROVIDER_ANSWERS,
            logins=[(PEM_Q, ""), (OPENCODE_LOGIN_Q, "lan"), (r"API key:", KEY)],
        )
        self.assert_ok(result)
        self.assertIn("brew install go", self.calls())
        self.assertIn("broker-install --build", self.calls())
        self.assertNotIn("agent-plugins-marketplace", "\n".join(self.calls()))
        self.assertNotIn(f"{RAW}v9.8.7/{GO}", "\n".join(self.calls()))

    def test_release_tag_broker_is_downloaded(self):
        self.protocol_at("main", OTHER)
        result = self.fresh(
            "--ref",
            "v9.8.7",
            clients="opencode",
            docker="y",
            opencode=PROVIDER_ANSWERS,
            logins=[(PEM_Q, ""), (OPENCODE_LOGIN_Q, "lan"), (r"API key:", KEY)],
        )
        self.assert_ok(result)
        self.assertNotIn("brew install go", self.calls())
        self.assertIn("broker-install ", self.calls())
        self.assertIn("skip: go: the broker comes from release v9.8.7", result.stderr)
        self.assertNotIn("agent-plugins-marketplace", "\n".join(self.calls()))

    def test_release_ref_served_by_the_marketplace(self):
        self.serve({"ref": "v9.8.7"})
        self.protocol_at("main", OTHER)
        result = self.fresh(docker="y")
        self.assert_ok(result)
        self.assertIn(
            f"    broker protocol {PROTOCOL}: ensemble v9.8.7 and release v9.8.7 agree\n",
            result.stderr,
        )
        self.assertEqual(self.broker_calls()[0], "broker-install ")
        self.assertNotIn("brew install go", self.calls())
        self.assertNotIn(f"{RAW}main/", "\n".join(self.calls()))
        self.assertIn("ensemble is installed (ref v9.8.7, clients claude,codex", result.stderr)

    def test_protocol_parsers_read_the_repo_sources(self):
        self.assert_ok(self.fresh(clients="claude", docker="y"))
        for path in (f"main/{CLIENT}", "main/.claude-plugin/plugin.json", f"v9.8.7/{GO}"):
            self.assertIn(f"curl -fsSL {RAW}{path}", self.calls())
        result = self.again("--clients", "claude", "--docker")
        self.assert_ok(result)
        self.assertIn(f"    broker protocol {PROTOCOL}: ", result.stderr)

    def test_protocol_mismatch_stops_before_client_steps(self):
        self.protocol_at("v9.8.7", OTHER)
        for rerun, (clients, remedy) in enumerate(
            (
                (
                    "claude,codex",
                    "the windroc marketplace serves ensemble at main, ahead of release v9.8.7:"
                    " wait until it serves a release, or use --clients opencode --ref v9.8.7",
                ),
                ("codex", "the windroc marketplace serves ensemble at main"),
            )
        ):
            with self.subTest(clients=clients):
                result = self.refused("--clients", clients, "--docker", rerun=bool(rerun))
                self.assertIn(
                    f"install.sh: ensemble main wants broker protocol {PROTOCOL}, but its broker,"
                    f" release v9.8.7, speaks protocol {OTHER}: {remedy}",
                    result.stderr,
                )
                calls = "\n".join(self.calls())
                for absent in ("--cask claude", "--cask codex", "--cask docker", "login"):
                    self.assertNotIn(absent, calls)
                self.assertEqual(self.broker_calls(), [])
                self.assertFalse(self.config().exists())

    def test_rerun_reinstalls_a_broker_with_another_protocol(self):
        self.assert_ok(self.fresh(clients="claude", docker="y"))
        (self.fakes / "src/broker/protocol").write_text(OTHER)
        result = self.again("--clients", "claude", "--docker")
        self.assert_ok(result)
        self.assertIn(
            f"    installed broker speaks protocol {OTHER}, the plugin wants {PROTOCOL}:"
            " reinstalling\n",
            result.stderr,
        )
        self.assertIn("broker-install ", self.broker_calls())
        result = self.again("--clients", "claude", "--docker")
        self.assert_ok(result)
        self.assertIn("==> broker\n    skip: installed from this plugin\n", result.stderr)
        self.assertNotIn("broker-install ", self.broker_calls())

    def claude_ensemble(self):
        """The directory of the ensemble plugin the fake Claude Code installed."""
        installed = json.loads((self.fakes / "claude/plugins.json").read_text())
        return next(p["installPath"] for p in installed if p["id"] == "ensemble@windroc")

    def test_rerun_keeps_a_current_hand_built_broker(self):
        self.assert_ok(self.fresh(clients="claude", docker="y"))
        path = self.claude_ensemble()
        Path(path, CLIENT).write_text(f'PROTOCOL = "{OTHER}"\n')
        (self.fakes / "src/broker/protocol").write_text(OTHER)
        result = self.again("--clients", "claude", "--docker")
        self.assert_ok(result)
        self.assertIn(
            f"    broker protocol {OTHER}: {path} and the installed broker agree\n", result.stderr
        )
        self.assertNotIn(f"{RAW}v9.8.7/{GO}", "\n".join(self.calls()))
        self.assertIn("==> broker\n    skip: installed from this plugin\n", result.stderr)

    def test_failing_broker_install_names_the_broker_step(self):
        (self.fakes / "broker_install_fails").write_text("")
        result = self.fresh("--clients", "claude", "--docker", egress_failure=True)
        self.assertEqual(result.code, 1)
        self.assertIn(
            "install.sh: broker failed; rerun the same command to continue\n", result.stderr
        )

    def test_opencode_rerun_keeps_a_current_hand_built_broker(self):
        self.assert_ok(
            self.fresh(
                clients="opencode",
                docker="y",
                opencode=PROVIDER_ANSWERS,
                logins=[(PEM_Q, ""), (OPENCODE_LOGIN_Q, "lan"), (r"API key:", KEY)],
            )
        )
        (self.opencode_package() / CLIENT).write_text(f'PROTOCOL = "{OTHER}"\n')
        (self.fakes / "src/broker").mkdir(parents=True, exist_ok=True)
        (self.fakes / "src/broker/protocol").write_text(OTHER)
        result = self.again()
        self.assert_ok(result)
        self.assertIn(
            f"    broker protocol {OTHER}: {self.opencode_package()} and the installed broker"
            " agree\n",
            result.stderr,
        )
        self.assertIn("==> broker\n    skip: installed from this plugin\n", result.stderr)

    def test_mixed_run_remedy_updates_the_plugin(self):
        """With Claude Code chosen only the served ref installs, so OpenCode's copy names the plugin update."""
        self.assert_ok(
            self.fresh(
                clients="claude,opencode",
                docker="y",
                opencode=PROVIDER_ANSWERS,
                logins=[
                    (TOKEN_Q, TOKEN),
                    (PEM_Q, ""),
                    (OPENCODE_LOGIN_Q, "lan"),
                    (r"API key:", KEY),
                ],
            )
        )
        (self.opencode_package() / CLIENT).write_text(f'PROTOCOL = "{OTHER}"\n')
        result = self.again()
        self.assertEqual(result.code, 1, result.stderr)
        self.assertIn(
            f"install.sh: {self.opencode_package()} wants broker protocol {OTHER}, but its broker,"
            f" the installed broker, speaks protocol {PROTOCOL}: update the plugin (Claude Code:",
            result.stderr,
        )

    def test_stale_opencode_copy_next_to_an_installed_plugin_names_the_update(self):
        self.assert_ok(
            self.fresh(
                clients="claude,opencode",
                docker="y",
                opencode=PROVIDER_ANSWERS,
                logins=[
                    (TOKEN_Q, TOKEN),
                    (PEM_Q, ""),
                    (OPENCODE_LOGIN_Q, "lan"),
                    (r"API key:", KEY),
                ],
            )
        )
        (self.fakes / "commit").write_text(NEW)
        self.protocol_at("main", OTHER)
        result = self.again()
        self.assertEqual(result.code, 1, result.stderr)
        self.assertIn(
            f"install.sh: ensemble main wants broker protocol {OTHER}, but its broker, the installed"
            f" broker, speaks protocol {PROTOCOL}: update the plugin (Claude Code:",
            result.stderr,
        )

    def test_other_client_plugin_against_the_installed_broker_stops(self):
        self.assert_ok(self.fresh(docker="y"))
        Path(self.claude_ensemble(), CLIENT).write_text(f'PROTOCOL = "{OTHER}"\n')
        (self.fakes / "src/broker/protocol").write_text(OTHER)
        codex = Path(self.home, ".codex/plugins/cache/windroc/ensemble/9.8.7")
        result = self.refused("--clients", "claude,codex", "--docker", rerun=True)
        self.assertIn(
            f"install.sh: {codex} wants broker protocol {PROTOCOL}, but its broker, the installed"
            f" broker, speaks protocol {OTHER}: update the plugin",
            result.stderr,
        )
        self.assertEqual(self.broker_calls(), [])

    def test_opencode_main_remedy_names_a_branch_other_than_main(self):
        self.protocol_at("v9.8.7", OTHER)
        flags = ("--clients", "opencode", "--docker", "--ref", "main")
        result = self.refused(*flags, then=PROVIDER_ANSWERS)
        self.assertIn(
            f"install.sh: ensemble main wants broker protocol {PROTOCOL}, but its broker, release"
            f" v9.8.7, speaks protocol {OTHER}: use --ref v9.8.7 or a branch other than main\n",
            result.stderr,
        )

    def test_codex_plugin_from_its_newest_cache_dir(self):
        self.assert_ok(self.fresh(clients="codex", docker="y"))
        cache = Path(self.home, ".codex/plugins/cache/windroc/ensemble")
        # 9.8.10 is newer than 9.8.7 by version, older by text.
        shutil.copytree(cache / "9.8.7", cache / "9.8.10")
        (cache / "9.8.10" / CLIENT).write_text(f'PROTOCOL = "{OTHER}"\n')
        result = self.refused("--clients", "codex", "--docker", rerun=True)
        self.assertIn(
            f"install.sh: {cache / '9.8.10'} wants broker protocol {OTHER}, but its broker, release"
            f" v9.8.7, speaks protocol {PROTOCOL}: update the plugin",
            result.stderr,
        )

    def test_installed_plugin_with_another_protocol_stops_up_front(self):
        self.assert_ok(self.fresh(clients="claude", docker="y"))
        path = self.claude_ensemble()
        Path(path, CLIENT).write_text(f'PROTOCOL = "{OTHER}"\n')
        result = self.refused("--clients", "claude", "--docker", rerun=True)
        self.assertIn(
            f"install.sh: {path} wants broker protocol {OTHER}, but its broker, release v9.8.7,"
            f" speaks protocol {PROTOCOL}: update the plugin (Claude Code: claude plugin"
            " marketplace update windroc, then claude plugin update ensemble@windroc; Codex:"
            " codex plugin marketplace upgrade windroc), then rerun\n",
            result.stderr,
        )
        self.assertEqual(self.broker_calls(), [])

    def test_invalid_version_or_protocol_stops(self):
        cases = (
            (
                "main",
                ".claude-plugin/plugin.json",
                '{"version": "9.8.7/../../x"}\n',
                "no release version in .claude-plugin/plugin.json at ref main",
            ),
            (
                "main",
                CLIENT,
                f"PROTOCOL = {PROTOCOL}\n",
                f"no broker protocol in {CLIENT} at ref main",
            ),
            (
                "v9.8.7",
                GO,
                'const Version = "x"\n',
                f"no broker protocol in {GO} at ref v9.8.7",
            ),
            (
                "main",
                ".claude-plugin/plugin.json",
                None,
                "cannot read .claude-plugin/plugin.json at ref main of windroc-labs/ensemble",
            ),
        )
        for rerun, (ref, path, text, message) in enumerate(cases):
            with self.subTest(path=path, text=text):
                shutil.rmtree(self.fakes / "refs", ignore_errors=True)
                self.at_ref(ref, path, text)
                result = self.refused("--clients", "claude", "--docker", rerun=bool(rerun))
                self.assertIn(f"install.sh: {message}\n", result.stderr)

    def test_existing_marketplace_is_refreshed_before_a_fresh_plugin(self):
        self.assert_ok(self.fresh())
        for client, key in (("claude", "id"), ("codex", "pluginId")):
            path = self.fakes / client / "plugins.json"
            kept = [p for p in json.loads(path.read_text()) if p[key] != "ensemble@windroc"]
            path.write_text(json.dumps(kept))
        self.assert_ok(self.again())
        self.assertEqual(
            self.plugin_calls(),
            [
                "claude plugin marketplace update windroc",
                "claude plugin install ensemble@windroc",
                "codex plugin marketplace upgrade windroc",
                "codex plugin add ensemble@windroc",
            ],
        )

    def test_docker_not_running_fails(self):
        (self.fakes / "docker_wont_start").touch()
        (self.bin / "sleep").unlink()
        executable(self.bin / "sleep", "#!/bin/sh\n")
        result = self.run_install(
            answers=[
                (SUDO_Q, PASSWORD),
                (WRITE_Q, "y"),
                (MENU_Q, menu(("claude", "codex", "docker"))),
                (WRITE_Q, "y"),
            ]
        )
        self.assertEqual(result.code, 1)
        self.assertEqual(self.calls().count("docker info"), 102)
        self.assertIn(
            "install.sh: Docker Desktop did not start in 5 minutes: check its window, then rerun",
            result.stderr,
        )
        self.assertIn(
            "install.sh: Docker Desktop running failed; rerun the same command", result.stderr
        )
        self.assertNotIn("broker-install ", self.calls())
        (self.fakes / "docker_wont_start").unlink()
        self.assert_ok(self.again(answers=[*DOCKER_LOGINS, (WRITE_Q, "y")]))
        self.assertIn("broker-install ", self.calls())

    def test_docker_first_start_unattended(self):
        result = self.fresh(clients="claude", docker="y")
        self.assert_ok(result)
        calls = self.calls()
        install = f"sudo -A {self.root}/Applications/Docker.app/Contents/MacOS/install"
        self.assertLess(
            calls.index(f"{install} --accept-license --user=tester"), calls.index("open -a Docker")
        )
        self.assertEqual(json.loads(self.docker_settings().read_text()), DOCKER_PRESETS)
        self.assertNotIn(PRESETS_NOT_WRITTEN, result.stderr)
        self.assertIn(
            "Docker Desktop 4.96.0 for arm64, macOS 14.0+: matches arm64, macOS 26.7.1",
            result.stderr,
        )
        self.assertNotIn("press Enter", result.tty)
        self.assertEqual(self.state()["docker_license"], "accepted")

    def test_docker_cli_not_in_usr_local_bin(self):
        self.assert_ok(self.fresh(clients="claude", docker="y"))
        self.assertNotIn("[session.hosts.docker]", self.config().read_text())
        Path(self.root, "usr/local/bin/docker").unlink()
        result = self.again(answers=[(WRITE_Q, "y")])
        self.assert_ok(result)
        self.assertTrue(
            self.config()
            .read_text()
            .endswith(f'[session.hosts.docker]\ndocker = "{self.root}/opt/homebrew/bin/docker"\n')
        )
        self.assertNotIn("[session.hosts.docker] docker", self.again().tty)

    def test_docker_settings_kept(self):
        self.docker_group().mkdir(parents=True)
        self.docker_settings().write_text(
            '{"UseVirtualizationFrameworkRosetta": true, "Theme": "dark"}'
        )
        self.assert_ok(self.fresh(clients="claude", docker="y"))
        self.assertEqual(
            json.loads(self.docker_settings().read_text()),
            {
                "UseVirtualizationFrameworkRosetta": True,
                "Theme": "dark",
                "DisplayedOnboarding": True,
            },
        )

    def test_docker_group_container_denied(self):
        group = self.docker_group()
        group.mkdir(parents=True)
        self.docker_settings().write_text('{"Theme": "dark"}')
        group.chmod(0)
        self.addCleanup(lambda: group.exists() and group.chmod(0o700))
        result = self.fresh(clients="claude", docker="y")
        self.assert_ok(result)
        self.assertEqual(result.stderr.count(PRESETS_NOT_WRITTEN), 1, result.stderr)
        group.chmod(0o700)
        self.assertEqual([p.name for p in group.iterdir()], ["settings-store.json"])
        self.assertEqual(self.docker_settings().read_text(), '{"Theme": "dark"}')

    def test_docker_group_container_create_denied(self):
        parent = self.docker_group().parent
        parent.mkdir(parents=True)
        parent.chmod(0o500)
        self.addCleanup(lambda: parent.exists() and parent.chmod(0o700))
        result = self.fresh(clients="claude", docker="y")
        self.assert_ok(result)
        self.assertEqual(result.stderr.count(PRESETS_NOT_WRITTEN), 1, result.stderr)
        self.assertFalse(self.docker_group().exists())

    def test_docker_settings_invalid_kept(self):
        self.docker_group().mkdir(parents=True)
        self.docker_settings().write_text("[]")
        result = self.fresh(clients="claude", docker="y")
        self.assert_ok(result)
        self.assertEqual(result.stderr.count(PRESETS_NOT_WRITTEN), 1, result.stderr)
        self.assertEqual(self.docker_settings().read_text(), "[]")
        self.assertEqual([p.name for p in self.docker_group().iterdir()], ["settings-store.json"])

    def test_docker_group_container_unreadable_parent(self):
        """A container whose lstat macOS denies may be the user's: never recorded, so cleanup keeps it."""
        group = self.docker_group()
        group.mkdir(parents=True)
        (group / "data").write_text("user's")
        group.parent.chmod(0o600)
        self.addCleanup(lambda: group.parent.exists() and group.parent.chmod(0o700))
        result = self.fresh(clients="claude", docker="y")
        self.assert_ok(result)
        self.assertEqual(
            result.stderr.count(f"{NOT_TOLD}{group}: cannot tell whether it is there"), 1
        )
        self.assertNotIn(f"dir\t{group}", self.ledger())
        self.assert_ok(self.cleanup("--yes"))
        group.parent.chmod(0o700)
        self.assertEqual((group / "data").read_text(), "user's")

    def test_dangling_symlink_not_recorded(self):
        """A dangling symlink is there, not missing: cleanup leaves it (Docker Desktop writes through it)."""
        plist = Path(self.home, "Library/Preferences/com.docker.docker.plist")
        plist.parent.mkdir(parents=True)
        Path(self.home, "dotfiles").mkdir()
        plist.symlink_to(Path(self.home, "dotfiles/docker.plist"))
        self.assert_ok(self.fresh(clients="claude", docker="y"))
        self.assertNotIn(f"file\t{plist}", self.ledger())
        self.assert_ok(self.cleanup("--yes"))
        self.assertTrue(plist.is_symlink())

    def docker_group(self):
        return Path(self.home, "Library/Group Containers/group.com.docker")

    def docker_settings(self):
        return self.docker_group() / "settings-store.json"

    def assert_docker_build_refused(self, marker, text, said):
        (self.fakes / marker).write_text(text)
        result = self.run_install(
            "--docker", "--clients", "claude", answers=[(SUDO_Q, PASSWORD), *[(WRITE_Q, "y")] * 2]
        )
        self.assertEqual(result.code, 1)
        self.assertIn(f"install.sh: {said}", result.stderr)
        self.assertNotIn("open -a Docker", self.calls())

    def test_docker_build_for_another_cpu(self):
        said = "Docker Desktop 4.96.0 is built for x86_64, not this Mac's arm64"
        self.assert_docker_build_refused("docker_arch", "x86_64", said)

    def test_docker_build_for_a_newer_macos(self):
        said = "Docker Desktop 4.96.0 needs macOS 27.0 or later, not 26.7.1"
        self.assert_docker_build_refused("docker_min_os", "27.0", said)

    def test_github_app_key_optional(self):
        self.assert_ok(self.fresh(docker="y"))
        result = self.again()
        self.assert_ok(result)
        self.assertNotRegex(result.tty, PEM_Q)
        self.assertIn(
            "skip: no GitHub App key: create the App by docs/docker-host.md#create-the-github-app",
            result.stderr,
        )
        (self.fakes / "keychain").mkdir(exist_ok=True)
        (self.fakes / "keychain/ensemble-bot").write_text("stored by hand")
        result = self.again(answers=[(WRITE_Q, "y")])
        self.assert_ok(result)
        self.assertIn("ensemble-broker login github ensemble-bot", self.calls())
        self.assertEqual(len(self.state()["login_github"]), 64)
        self.assertEqual(self.state()["github_app"], "ensemble-test-bot")
        self.assertIn('[session.hosts.docker.github] app = "ensemble-test-bot"', result.tty)
        self.assertTrue(
            self.config()
            .read_text()
            .endswith('[session.hosts.docker.github]\napp = "ensemble-test-bot"\n')
        )
        result = self.again()
        self.assert_ok(result)
        self.assertNotIn("login github", "\n".join(self.calls()))

    def test_github_app_key_stored_by_the_broker(self):
        """The broker creates the App key item from the .pem path, so the key is on no command line."""
        pem = Path(self.home, "app.pem")
        secret = "-----BEGIN RSA PRIVATE KEY-----SENTINELKEY-----END RSA PRIVATE KEY-----"
        pem.write_text(secret)
        logins = [(TOKEN_Q, TOKEN), (PEM_Q, str(pem)), (r"App ID \(digits\):", "12 -w x")]
        logins += [(r"App ID \(digits\):", "0123"), (r"App ID \(digits\):", "1234567")]
        result = self.fresh(docker="y", logins=[*logins, (WRITE_Q, "y")])
        self.assert_ok(result)
        self.assertEqual((self.fakes / "keychain/ensemble-bot").read_text(), secret)
        self.assertEqual((self.fakes / "keychain/ensemble-bot.account").read_text(), "1234567")
        calls = self.calls()
        login = f"ensemble-broker login github ensemble-bot --key-file {pem} --app-id 1234567"
        self.assertIn(login, calls)
        self.assertFalse(
            [c for c in calls if c.startswith(("security -i", "security add-", "base64"))]
        )
        self.assertIn("keychain\tensemble-bot\t1234567", self.ledger())
        self.assertEqual(self.state()["github_app"], "ensemble-test-bot")
        pem.unlink()
        self.assert_secret_free("SENTINELKEY", TOKEN, result=result)

    def test_claude_login_follows_broker_credential_usability(self):
        self.assert_ok(self.fresh(docker="y"))
        state = Path(self.ensemble, "install/state")
        state.write_text(re.sub(r"(?m)^login_claude=.*\n", "", state.read_text()))
        versions = Path(self.fakes, "claude/cache/0/src/broker/internal/codex/versions.go")
        versions.write_text('package codex\n\nconst MinVersion = "0.170.0"\n')
        result = self.again()
        self.assert_ok(result)
        calls = self.calls()
        self.assertIn("broker-install ", calls)
        self.assertIn("skip: default Claude credential usable by this broker", result.stderr)
        self.assertNotIn("claude setup-token", calls)
        self.assertNotIn("ensemble-broker login claude", calls)
        self.assertNotIn("ensemble-broker login codex", calls)
        self.assertIn(f"sudo -A {self.broker_path()} update-codex 0.170.0", calls)
        self.assertIn("login_claude", self.state())
        (self.fakes / "src/broker/claude_unusable").touch()
        self.assert_ok(self.again(answers=[(TOKEN_Q, TOKEN)]))
        self.assertIn("claude setup-token", self.calls())
        self.assertIn("ensemble-broker login claude", self.calls())

    def test_codex_install_or_update(self):
        self.assert_ok(self.fresh(docker="y"))
        codex = self.broker_path().parent / "codex/state.json"
        broker = self.broker_path()
        cases = (
            (
                {"state": "installation_invalid", "repair": "sudo B update-codex <version>"},
                "update",
            ),
            ({"state": "ready", "version": "0.163.0", "repair": ""}, None),
            ({"state": "expired", "version": "0.163.1", "repair": "B login codex"}, None),
            ({"state": "busy", "version": "0.163.0"}, "busy"),
        )
        for status, action in cases:
            codex.write_text(json.dumps(status))
            result = self.again()
            calls = self.calls()
            if action == "busy":
                self.assertEqual(result.code, 1)
                self.assertIn("install.sh: broker busy; rerun", result.stderr)
                continue
            self.assert_ok(result)
            update = f"sudo -A {broker} update-codex 0.163.0"
            self.assertEqual(update in calls, action == "update", status)
            self.assertNotIn(f"sudo -A {broker} install-codex 0.163.0", calls)
            login = "ensemble-broker login codex" in calls
            self.assertEqual(login, status["state"] != "ready", status)

    def test_broker_audit_warning_preserves_ready_credentials(self):
        self.assert_ok(self.fresh(docker="y"))
        warning = self.fakes / "src/broker/audit_warning.json"
        for value, message in (
            ("audit_write_failed", "audit_write_failed: broker audit persistence failed"),
            ("AUDIT_SECRET_SENTINEL", "invalid audit warning metadata"),
        ):
            warning.write_text(json.dumps(value))
            result = self.again()
            self.assert_ok(result)
            self.assertIn(message, result.stderr)
            self.assertIn("the operation result still applies", result.stderr)
            self.assertNotIn("AUDIT_SECRET_SENTINEL", result.stdout + result.stderr + result.tty)
            calls = self.calls()
            self.assertIn("ensemble-broker status --cleanup", calls)
            self.assertNotIn("ensemble-broker login claude", calls)
            self.assertNotIn("ensemble-broker login codex", calls)

    def test_opencode_config_and_flags(self):
        flags = ("--clients", "claude,opencode", "--no-docker")
        flags += ("--opencode-provider", "lan", "--opencode-url", "http://10.0.0.5:8080/v1")
        flags += ("--opencode-model", "m1", "--opencode-context", "204800", "--opencode-key-stdin")
        result = self.run_install(
            *flags,
            stdin=KEY + "\n",
            answers=[(SUDO_Q, PASSWORD), *[(WRITE_Q, "y")] * 6],
        )
        self.assert_ok(result)
        exe = f"{self.ensemble}/opencode/bin/opencode"
        tiers = "".join(f'{n} = "lan/m1"\n' for n in "1234")
        self.assertTrue(
            self.config()
            .read_text()
            .endswith(
                f'[session.clients.opencode]\nexecutable = "{exe}"\nmodel = "lan/m1"\n\n'
                '[session.clients.opencode.providers.lan]\nbase_url = "http://10.0.0.5:8080/v1"\n'
                f'models = ["m1"]\ncontext = 204800\n\n[session.clients.opencode.tiers]\n{tiers}'
            ),
            self.config().read_text(),
        )
        self.assertEqual(
            (self.state()["opencode_provider"], self.state()["opencode_model"]), ("lan", "m1")
        )
        text = self.config().read_text().replace('3 = "lan/m1"', '3 = "lan/mine"')
        self.config().write_text(text)
        result = self.again("--opencode-model", "m2", answers=[(WRITE_Q, "y")])
        self.assert_ok(result)
        text = self.config().read_text()
        self.assertIn('models = ["m2"]', text)
        self.assertIn('1 = "lan/m2"', text)
        self.assertIn('3 = "lan/mine"', text)
        self.assertIn('model = "lan/m2"', text)
        self.assertIn("context = 204800", text)
        self.assertNotIn("Which clients", result.tty)
        result = self.again("--opencode-context", "131072", answers=[(WRITE_Q, "y")])
        self.assert_ok(result)
        self.assertIn("context = 131072", self.config().read_text())
        self.assertEqual(self.state()["opencode_context"], "131072")

    def test_opencode_context_asked_when_missing(self):
        """A State from before the context question: the rerun asks it and adds it to the table."""
        self.assert_ok(
            self.fresh(
                "--no-docker",
                clients="claude,opencode",
                opencode=PROVIDER_ANSWERS,
                logins=[(OPENCODE_LOGIN_Q, "lan"), (r"API key:", KEY)],
            )
        )
        state = Path(self.ensemble, "install/state")
        state.write_text(re.sub(r"(?m)^opencode_context=.*\n", "", state.read_text()))
        self.config().write_text(self.config().read_text().replace("context = 204800\n", ""))
        result = self.again(answers=[(CONTEXT_Q, "204800"), (WRITE_Q, "y")])
        self.assert_ok(result)
        self.assertIn("[session.clients.opencode.providers.lan] context = 204800", result.tty)
        self.assertIn("context = 204800", self.config().read_text())

    def test_opencode_flags_refused(self):
        result = self.run_install("--clients", "claude", "--opencode-model", "m", tty=False)
        self.assertEqual(result.code, 2)
        self.assertIn("opencode is not among the clients", result.stderr)
        self.assertEqual(self.calls(), [])
        result = self.run_install(
            "--no-docker",
            answers=[
                (SUDO_Q, PASSWORD),
                (WRITE_Q, "y"),
                (MENU_Q, menu(("claude", "opencode"))),
                (PROVIDER_Q, "ollama"),
                (PROVIDER_Q, "lan.1"),
                (PROVIDER_Q, "lan"),
                (URL_Q, 'http://h"/v1'),
                (URL_Q, "http://host/v1"),
                (MODEL_Q, "m"),
                (CONTEXT_Q, "0"),
                (CONTEXT_Q, ""),
                (WRITE_Q, "y"),
                (WRITE_Q, "y"),
                (WRITE_Q, "y"),
                (OPENCODE_LOGIN_Q, "lan"),
                (r"API key:", KEY),
                (WRITE_Q, "y"),
                (WRITE_Q, "y"),
            ],
        )
        self.assert_ok(result)
        self.assertEqual(self.state()["opencode_provider"], "lan")
        self.assertEqual(self.state()["opencode_context"], "65536")
        self.assertIn("context = 65536", self.config().read_text())

    def test_opencode_tmux_interactive(self):
        result = self.fresh(
            "--no-docker",
            clients="claude,opencode",
            opencode=PROVIDER_ANSWERS,
            logins=[(OPENCODE_LOGIN_Q, "lan"), (r"API key:", KEY)],
        )
        self.assert_ok(result)
        calls = self.calls()
        self.assertIn("brew install node", calls)
        prefix = f"{self.ensemble}/opencode"
        self.assertIn(
            f"npm install -g --prefix {prefix} --cache {prefix}/.npm-cache opencode-ai@1.19.3",
            calls,
        )
        self.assertIn(f"node {prefix}/lib/node_modules/opencode-ai/postinstall.mjs", calls)
        self.assertIn("opencode auth login", calls)
        self.assertIn("In opencode auth login pick Other, enter lan", result.stderr)
        self.assertEqual(json.loads(self.auth_json().read_text())["lan"]["key"], KEY)
        result = self.again()
        self.assert_ok(result)
        self.assertNotIn("opencode auth login", self.calls())
        self.assertNotIn("npm install", "\n".join(self.calls()))
        self.assert_secret_free(KEY, PASSWORD, result=result)

    def refused_tty(self):
        """Another terminal as /dev/tty, which the Bun-built clients die on, as macOS's kqueue does."""
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        (self.fakes / "kqueue_refuses").write_text(os.ttyname(slave))
        return os.ttyname(slave)

    def test_logins_use_the_terminal_device(self):
        result = self.fresh(
            "--no-docker",
            clients="claude,opencode",
            opencode=PROVIDER_ANSWERS,
            logins=[(OPENCODE_LOGIN_Q, "lan"), (r"API key:", KEY)],
            ctty=self.refused_tty(),
        )
        self.assert_ok(result)
        self.assertIn("claude auth login", self.calls())
        self.assertIn("opencode auth login", self.calls())

    def test_broker_logins_use_the_terminal_device(self):
        result = self.fresh(clients="claude", docker="y", ctty=self.refused_tty())
        self.assert_ok(result)
        self.assertIn("claude setup-token", self.calls())
        self.assertIn("ensemble-broker login claude", self.calls())

    def test_setup_token_on_a_cleared_alternate_screen(self):
        """The token setup-token prints and its paste stay on the alternate screen, cleared before leaving it."""
        result = self.fresh(clients="claude", docker="y")
        self.assert_ok(result)
        self.assertEqual((result.tty.count(SCREEN_ON), result.tty.count(SCREEN_OFF)), (1, 1))
        on = result.tty.index(SCREEN_ON)
        off = result.tty.index(SCREEN_OFF)
        self.assertLess(on, off)
        shown = [m.start() for m in re.finditer(re.escape(TOKEN), result.tty)]
        self.assertTrue(shown)
        self.assertTrue(all(on < at < off for at in shown), result.tty)
        self.assertRegex(result.tty[on:off], TOKEN_Q)
        self.assertIn("    setup-token saved in the broker; its screen is cleared\n", result.stderr)
        self.assertEqual((self.fakes / "src/broker/claude_token").read_text(), TOKEN)
        self.assert_secret_free(TOKEN, result=result)

    def test_setup_token_failure_leaves_the_alternate_screen(self):
        """A failed setup-token or broker login waits for Enter on that screen, then leaves it and stops."""
        failed = "install.sh: broker login: Claude failed; rerun the same command to continue\n"
        self.assert_ok(self.fresh("--no-docker", clients="claude"))
        config = [(WRITE_Q, "y")]
        for call in ("claude setup-token", "ensemble-broker login claude"):
            with self.subTest(call=call):
                (self.fakes / "fail").write_text(call)
                result = self.again("--docker", answers=[*config, (LEAVE_Q, "")])
                config = []
                self.assertEqual(result.code, 1, result.stderr)
                on = result.tty.index(SCREEN_ON)
                self.assertGreater(result.tty.index(LEAVE_Q.replace("\\", ""), on), on)
                self.assertTrue(result.tty.endswith(SCREEN_OFF), result.tty[-200:])
                self.assertEqual(result.stderr.count(failed), 1, result.stderr)
                self.assertNotIn("setup-token saved in the broker", result.stderr)
                self.assertNotIn("login_claude", self.state())

    def test_terminal_without_a_device_name(self):
        (self.fakes / "tty_name").write_text("??\n")
        self.assert_ok(self.fresh())
        self.assertIn("claude auth login", self.calls())

    def test_opencode_that_does_not_run_stops(self):
        (self.fakes / "postinstall_broken").write_text("")
        flags = ("--clients", "claude,opencode", "--no-docker", "--opencode-provider", "lan")
        flags += ("--opencode-url", "http://localhost:8080/v1", "--opencode-model", "m")
        flags += ("--opencode-context", "65536", "--opencode-key-stdin")
        result = self.run_install(
            *flags, stdin=KEY + "\n", answers=[(SUDO_Q, PASSWORD), (WRITE_Q, "y"), (WRITE_Q, "y")]
        )
        self.assertEqual(result.code, 1)
        self.assertIn(
            f"install.sh: OpenCode 1.19.3 in {self.ensemble}/opencode does not run:"
            " opencode --version gave ''",
            result.stderr,
        )
        (self.fakes / "postinstall_broken").unlink()
        result = self.run_install(
            *flags, stdin=KEY + "\n", answers=[(SUDO_Q, PASSWORD), *[(WRITE_Q, "y")] * 4]
        )
        self.assert_ok(result)
        self.assertIn("1.19.3", (self.fakes / "opencode_version").read_text())

    def opencode_only(self, provider_answer="y", *, provider="lan", path_answer="y"):
        flags = ("--clients", "opencode", "--no-docker", "--opencode-provider", provider)
        flags += ("--opencode-url", "http://localhost:8080/v1", "--opencode-model", "m")
        flags += ("--opencode-context", "65536", "--opencode-key-stdin")
        provider_answers = [(WRITE_Q, provider_answer)] if provider_answer is not None else []
        return self.run_install(
            *flags,
            stdin=KEY + "\n",
            answers=[
                (SUDO_Q, PASSWORD),
                *[(WRITE_Q, "y")] * 3,
                *provider_answers,
                (WRITE_Q, "y"),
                *([(WRITE_Q, path_answer)] if path_answer is not None else []),
            ],
        )

    def opencode_rerun(self, *answers):
        """A rerun of opencode_only's install: the checklist confirmed, then `answers`."""
        return self.run_install(
            "--password-stdin", stdin=PASSWORD + "\n", answers=[(MENU_Q, ""), *answers]
        )

    def opencode_package(self):
        return Path(
            self.home,
            ".cache/opencode/packages/ensemble@git+https:/github.com/windroc-labs/ensemble.git#main",
            "node_modules/ensemble",
        )

    def test_opencode_selects_usable_path_binary_or_installs_private_copy(self):
        cases = [(v, True) for v in ("1.19.3", "1.20.0", "1.100.0")]
        cases += [
            (v, False)
            for v in ("1.19.2", "1.9.9", "1.20.0-beta.1", "2.0.0", "garbage", "", "failed", None)
        ]
        for version, usable in cases:
            with self.subTest(version=version):
                external = self.bin / "opencode"
                if version is not None:
                    shutil.copy(self.fakes / "apps/opencode", external)
                (self.fakes / "opencode_version").write_text(version or "")
                if version == "failed":
                    (self.fakes / "fail").write_text("opencode --version")
                self.assert_ok(self.opencode_only(path_answer=None if usable else "y"))
                private = Path(self.ensemble, "opencode/bin/opencode")
                self.assertEqual(private.exists(), not usable)
                self.assertEqual(
                    tomllib.loads(self.config().read_text())["session"]["clients"]["opencode"][
                        "executable"
                    ],
                    str(external if usable else private),
                )
                calls = self.calls()
                self.assertEqual(any(c.startswith("npm install") for c in calls), not usable)
                if usable:
                    self.assertNotIn("brew install node", calls)
                    self.assertNotIn(
                        ".ensemble/opencode/bin", Path(self.home, ".zprofile").read_text()
                    )
                    self.assert_ok(self.opencode_rerun())
                    self.assertFalse(any("auth login" in c for c in self.calls()))
                    self.assertFalse(private.exists())
                self.assert_ok(self.cleanup("--yes"))
                if version is not None:
                    self.assertEqual(
                        external.read_bytes(), (self.fakes / "apps/opencode").read_bytes()
                    )
                    external.unlink()

    def test_opencode_only(self):
        result = self.opencode_only()
        self.assert_ok(result)
        calls = self.calls()
        self.assertIn(f"opencode plugin -g -f {SPEC}", calls)
        self.assertIn(
            "curl -fsSL https://raw.githubusercontent.com/windroc-labs/ensemble/main/src/broker/internal/launcher/versions.go",
            calls,
        )
        self.assertFalse([c for c in calls if c.startswith(("claude", "codex"))])
        self.assertIn("ctmux --install", calls)
        self.assertIn('[delegate]\nclient = "opencode"\n', self.config().read_text())
        conf = json.loads(Path(self.home, ".config/opencode/opencode.jsonc").read_text())
        self.assertEqual(conf["plugin"], [SPEC])
        self.assertIn(f"    {self.opencode_package()}\n", result.stderr)
        self.assertEqual((self.opencode_package() / "COMMIT").read_text(), OLD)
        self.assertEqual(self.state()["opencode_plugin_commit"], OLD)
        result = self.opencode_rerun()
        self.assert_ok(result)
        self.assertIn("> [ ] Claude Code", result.tty)
        self.assertNotIn("opencode plugin", "\n".join(self.calls()))
        self.assertIn(f"==> OpenCode plugin\n    skip: {SPEC} at {OLD[:12]}\n", result.stderr)

    def assert_opencode_in_new_shell(self, profile):
        result = subprocess.run(
            [
                "bash",
                "--noprofile",
                "--norc",
                "-c",
                '. "$1"; command -v opencode; opencode --version',
                "bash",
                str(profile),
            ],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(), [f"{self.ensemble}/opencode/bin/opencode", "1.19.3"]
        )

    def test_opencode_path_fresh_install_preserves_selected_provider(self):
        result = self.opencode_only(provider="strata")
        self.assert_ok(result)
        self.assert_opencode_in_new_shell(Path(self.home, ".zprofile"))
        config = json.loads(Path(self.home, ".config/opencode/opencode.jsonc").read_text())
        self.assertIn("m", config["provider"]["strata"]["models"])
        self.assertEqual(json.loads(self.auth_json().read_text())["strata"]["type"], "api")
        self.assert_secret_free(KEY, PASSWORD, result=result)

    def test_opencode_path_bash_profile(self):
        self.env["SHELL"] = "/bin/bash"
        self.assert_ok(self.opencode_only())
        self.assert_opencode_in_new_shell(Path(self.home, ".bash_profile"))
        self.assertFalse(Path(self.home, ".zprofile").exists())

    def test_opencode_path_upgrade_and_rerun(self):
        # Declining leaves the profile in the shape made by older installers.
        self.assert_ok(self.opencode_only(path_answer="n"))
        profile = Path(self.home, ".zprofile")
        line = 'export PATH="$HOME/.ensemble/opencode/bin:$PATH"'
        self.assertNotIn(line, profile.read_text())
        before = profile.read_text()
        self.assert_ok(self.opencode_rerun((WRITE_Q, "y")))
        self.assertEqual(profile.read_text(), before + line + "\n")
        self.assertFalse(any(c.startswith("npm install") for c in self.calls()))
        self.assert_opencode_in_new_shell(profile)
        self.assert_ok(self.opencode_rerun())
        self.assertEqual(profile.read_text().splitlines().count(line), 1)
        self.assertEqual(self.ledger().count(f"profile\t{profile}\t{line}"), 1)
        self.assert_opencode_in_new_shell(profile)

    def test_unselected_opencode_does_not_change_its_path(self):
        self.assert_ok(self.fresh("--no-docker"))
        profile = Path(self.home, ".zprofile")
        before = profile.read_bytes()
        self.assertNotIn(".ensemble/opencode", profile.read_text())
        self.assert_ok(self.again())
        self.assertEqual(profile.read_bytes(), before)
        self.assertFalse(Path(self.ensemble, "opencode").exists())

    def test_opencode_path_cleanup_preserves_user_lines(self):
        profile = Path(self.home, ".zprofile")
        profile.write_text("export EDITOR=vim\n")
        self.assert_ok(self.opencode_only())
        self.assert_ok(self.cleanup("--yes", "--password-stdin", stdin=PASSWORD + "\n"))
        self.assertEqual(profile.read_text(), "export EDITOR=vim\n")

    def test_opencode_rerun_downloads_new_commit(self):
        self.assert_ok(self.opencode_only())
        (self.fakes / "commit").write_text(NEW)
        (self.pkg_src / "NEW").write_text("")
        result = self.opencode_rerun()
        self.assert_ok(result)
        self.assertIn(f"opencode plugin -g -f {SPEC}", self.calls())
        self.assertIn(
            f"    {SPEC}: main is at {NEW[:12]}, OpenCode's copy at {OLD[:12]}: downloading it again\n",
            result.stderr,
        )
        package = self.opencode_package()
        self.assertEqual((package / "COMMIT").read_text(), NEW)
        self.assertTrue((package / "NEW").exists())
        self.assertEqual(self.state()["opencode_plugin_commit"], NEW)
        self.assertIn(f"    {package}\n", result.stderr)

    def test_plugin_layout_upgrade_refreshes_existing_executable_launchers_once(self):
        self.assert_ok(self.opencode_only())
        for name in (
            "ctmux",
            "ensemble-crew",
            "ensemble-session",
            "ensemble-delegate",
            "ensemble-orchestrator",
        ):
            path = Path(self.ensemble, "bin", name)
            path.write_text("#!/bin/sh\n# old plugin layout\n")
            path.chmod(0o755)
        self.assert_ok(self.opencode_rerun())
        self.assertIn("ctmux --install", self.calls())
        for path in Path(self.ensemble, "bin").iterdir():
            self.assertEqual(path.read_text(), "#!/bin/sh\nexit 0\n")
        self.assert_ok(self.opencode_rerun())
        self.assertNotIn("ctmux --install", self.calls())

    def test_opencode_new_commit_reinstalls_broker(self):
        result = self.fresh(
            clients="opencode",
            docker="y",
            opencode=PROVIDER_ANSWERS,
            logins=[(PEM_Q, ""), (OPENCODE_LOGIN_Q, "lan"), (r"API key:", KEY)],
        )
        self.assert_ok(result)
        self.assertIn("broker-install ", self.calls())
        (self.fakes / "commit").write_text(NEW)
        self.versions.write_text('package codex\n\nconst MinVersion = "0.170.0"\n')
        result = self.again()
        self.assert_ok(result)
        calls = self.calls()
        self.assertIn(f"opencode plugin -g -f {SPEC}", calls)
        self.assertIn("broker-install ", calls)
        self.assertNotIn("ctmux --install", calls)
        self.assertNotIn("opencode auth login", calls)
        self.assertEqual((self.opencode_package() / "COMMIT").read_text(), NEW)

    def test_opencode_rerun_without_recorded_commit(self):
        """An install from before install.sh recorded the plugin's commit downloads it once more."""
        self.assert_ok(self.opencode_only())
        state = Path(self.ensemble, "install/state")
        lines = state.read_text().splitlines(keepends=True)
        state.write_text("".join(x for x in lines if not x.startswith("opencode_plugin_commit=")))
        result = self.opencode_rerun()
        self.assert_ok(result)
        self.assertIn(
            f"    {SPEC}: main is at {OLD[:12]}, OpenCode's copy at unknown: downloading it again\n",
            result.stderr,
        )
        self.assertIn(f"opencode plugin -g -f {SPEC}", self.calls())
        self.assertEqual(self.state()["opencode_plugin_commit"], OLD)

    def test_opencode_unresolvable_ref_stops(self):
        self.assert_ok(self.opencode_only())
        (self.fakes / "fail").write_text(API_CALL)
        failed = self.opencode_rerun()
        (self.fakes / "commit").write_text("main\n")
        garbled = self.opencode_rerun()
        for result in (failed, garbled):
            self.assertEqual(result.code, 1)
            self.assertIn(
                "install.sh: cannot resolve main on github.com/windroc-labs/ensemble\n",
                result.stderr,
            )
            self.assertIn(
                "install.sh: OpenCode plugin failed; rerun the same command to continue",
                result.stderr,
            )
        self.assertNotIn("opencode plugin", "\n".join(self.calls()))
        self.assertEqual((self.opencode_package() / "COMMIT").read_text(), OLD)
        self.assertEqual(self.state()["opencode_plugin_commit"], OLD)

    def test_opencode_failed_download_keeps_no_commit(self):
        self.assert_ok(self.opencode_only())
        (self.fakes / "commit").write_text(NEW)
        (self.fakes / "fail").write_text(f"opencode plugin -g -f {SPEC}")
        result = self.opencode_rerun()
        self.assertEqual(result.code, 1)
        self.assertIn(
            "install.sh: OpenCode plugin failed; rerun the same command to continue", result.stderr
        )
        self.assertEqual(self.state()["opencode_plugin_commit"], OLD)
        self.assertFalse(self.opencode_package().exists())
        result = self.opencode_rerun()
        self.assert_ok(result)
        self.assertIn("OpenCode's copy at unknown: downloading it again", result.stderr)
        self.assertEqual((self.opencode_package() / "COMMIT").read_text(), NEW)
        self.assertEqual(self.state()["opencode_plugin_commit"], NEW)

    def test_opencode_plugin_declined_keeps_cache(self):
        self.assert_ok(self.opencode_only())
        Path(self.home, ".config/opencode/opencode.jsonc").write_text('{"plugin": []}')
        (self.fakes / "commit").write_text(NEW)
        result = self.opencode_rerun((WRITE_Q, "n"), (WRITE_Q, "n"))
        self.assert_ok(result)
        self.assertIn("==> OpenCode plugin\n    skip: declined\n", result.stderr)
        self.assertNotIn("opencode plugin", "\n".join(self.calls()))
        self.assertEqual((self.opencode_package() / "COMMIT").read_text(), OLD)
        self.assertEqual(self.state()["opencode_plugin_commit"], OLD)

    def test_opencode_ref_outside_cache_deletes_nothing(self):
        victim = Path(self.home, ".cache/opencode/victim")
        victim.mkdir(parents=True)
        ref = "../../../../../../victim"
        flags = ("--ref", ref, "--clients", "opencode", "--no-docker", "--opencode-provider", "lan")
        flags += ("--opencode-url", "http://localhost:8080/v1", "--opencode-model", "m")
        flags += ("--opencode-context", "65536", "--opencode-key-stdin")
        result = self.run_install(
            *flags, stdin=KEY + "\n", answers=[(SUDO_Q, PASSWORD), *[(WRITE_Q, "y")] * 2]
        )
        self.assertEqual(result.code, 1)
        spec = f"ensemble@git+https://github.com/windroc-labs/ensemble.git#{ref}"
        self.assertIn(f"install.sh: {spec}: not an OpenCode package dir\n", result.stderr)
        self.assertTrue(victim.is_dir())
        calls = "\n".join(self.calls())
        self.assertNotIn("opencode plugin", calls)
        self.assertNotIn("api.github.com", calls)

    def test_codex_only_docker_keeps_default_image_plugins(self):
        result = self.fresh("--clients", "codex", "--docker", logins=[(PEM_Q, "")])
        self.assert_ok(result)
        self.assertNotIn("plugins =", self.config().read_text())
        self.assertNotIn("updraft", "\n".join(self.plugin_calls()))

    def test_opencode_only_docker_keeps_default_image_plugins(self):
        result = self.fresh(
            clients="opencode",
            docker="y",
            opencode=PROVIDER_ANSWERS,
            logins=[(PEM_Q, ""), (OPENCODE_LOGIN_Q, "lan"), (r"API key:", KEY)],
        )
        self.assert_ok(result)
        self.assertNotIn("plugins =", self.config().read_text())
        self.assertEqual(self.plugin_calls(), [f"opencode plugin -g -f {SPEC}"])

    def test_cleanup_round_trip_opencode_only(self):
        conf = Path(self.home, ".config/opencode")
        conf.mkdir(parents=True)
        (conf / "opencode.json").write_text('{"theme": "mine"}')
        before = self.snapshot()
        self.assert_ok(self.opencode_only())
        self.assertIn("ensemble@git+", (conf / "opencode.json").read_text())
        result = self.cleanup("--yes", "--password-stdin", stdin=PASSWORD + "\n")
        self.assert_ok(result)
        self.assertEqual(self.snapshot(), before)

    def test_opencode_cleanup_two_upgraded_install_cycles(self):
        shutil.rmtree(self.ensemble)
        conf = Path(self.home, ".config/opencode")
        for cycle in range(2):
            with self.subTest(cycle=cycle):
                self.assert_ok(self.opencode_only())
                (self.fakes / "commit").write_text(NEW if cycle == 0 else OLD)
                self.assert_ok(self.opencode_rerun())
                path = conf / "opencode.jsonc"
                self.assertNotIn(f"opencode-config\t{path}", self.ledger())
                self.assertFalse(Path(self.ensemble, "install/opencode.jsonc.orig").exists())
                self.assert_ok(self.cleanup("--yes", "--password-stdin", stdin=PASSWORD + "\n"))
                self.assertFalse(path.exists())
                for name in ("node_modules", "package.json", "package-lock.json", ".gitignore"):
                    self.assertFalse((conf / name).exists(), name)
                self.assertFalse(conf.exists())
                self.assertFalse(Path(self.ensemble).exists())

    def test_opencode_cleanup_two_cycles_restore_user_jsonc(self):
        path = Path(self.home, ".config/opencode/opencode.jsonc")
        path.parent.mkdir(parents=True)
        original = b'{"theme": "mine", "plugin": ["user-plugin"]}\n  \n'
        path.write_bytes(original)
        originals = {
            "package.json": b'{"dependencies":{"user-plugin":"1.0.0"}}\n',
            "package-lock.json": b'{"name":"user-lock","lockfileVersion":3}\n',
            ".gitignore": b"# user ignores\nuser-cache/\n",
        }
        for name, contents in originals.items():
            (path.parent / name).write_bytes(contents)
        for cycle in range(2):
            with self.subTest(cycle=cycle):
                self.assert_ok(self.opencode_only())
                (self.fakes / "commit").write_text(NEW if cycle == 0 else OLD)
                self.assert_ok(self.opencode_rerun())
                self.assert_ok(self.cleanup("--yes", "--password-stdin", stdin=PASSWORD + "\n"))
                self.assertEqual(path.read_bytes(), original)
                self.assertFalse((path.parent / "node_modules").exists())
                for name, contents in originals.items():
                    self.assertEqual((path.parent / name).read_bytes(), contents, name)

    def runtime_files(self):
        paths = [
            Path(self.ensemble, relative)
            for relative in (
                "logs/runs.jsonl",
                "work/X/result.txt",
                "adhoc/X/result.txt",
                "managers/X/state.json",
                "docker-state/X/state.json",
                "workers/X/state.json",
                "tmp/X/state.json",
                "unrecorded/cache",
                ".runtime/cache",
            )
        ]
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("runtime data")
        return paths

    def test_cleanup_removes_runtime_data_and_created_root(self):
        shutil.rmtree(self.ensemble)
        self.assert_ok(self.opencode_only())
        self.runtime_files()
        self.assert_ok(self.cleanup("--yes"))
        self.assertFalse(Path(self.ensemble).exists())

    def test_cleanup_keeps_root_ownership_across_board_and_install_cycles(self):
        shutil.rmtree(self.ensemble)
        self.assert_ok(
            self.fresh(
                "--clients",
                "opencode",
                "--docker",
                "--board",
                "local",
                "--no-launchd",
                opencode=PROVIDER_ANSWERS,
                logins=[(PEM_Q, ""), (OPENCODE_LOGIN_Q, "lan"), (r"API key:", KEY)],
                then=[(WRITE_Q, "y")] * 2,
            )
        )
        board = Path(self.ensemble, "board")
        issue = board / "unassigned/keep.md"
        issue.write_text("keep")
        runtime = self.runtime_files()
        self.assert_ok(self.cleanup("--yes"))
        self.assertEqual(self.ledger(), [f"dir\t{self.ensemble}"])
        self.assertEqual(issue.read_text(), "keep")
        for path in runtime:
            self.assertFalse(path.exists(), str(path))
        self.assertEqual(
            {path.name for path in Path(self.ensemble).iterdir()}, {"board", "install"}
        )
        self.assert_ok(self.opencode_only())
        (self.fakes / "commit").write_text(NEW)
        self.assert_ok(self.opencode_rerun())
        runtime = self.runtime_files()
        self.assert_ok(self.cleanup("--yes"))
        self.assertEqual(self.ledger(), [f"dir\t{self.ensemble}"])
        self.assertEqual(issue.read_text(), "keep")
        for path in runtime:
            self.assertFalse(path.exists(), str(path))
        self.assertFalse(Path(self.home, ".config/opencode").exists())
        shutil.rmtree(board)
        self.assert_ok(self.cleanup("--yes"))
        self.assertFalse(Path(self.ensemble).exists())

    def test_cleanup_preserves_preexisting_user_files_and_runtime_directories(self):
        user = Path(self.ensemble, "keep.txt")
        user.write_text("keep")
        original = Path(self.ensemble, "work/user-project/result.txt")
        original.parent.mkdir(parents=True)
        original.write_text("user work")
        special = original.parent / "user [*]\tline\n.txt"
        special.write_text("user special")
        self.assert_ok(self.opencode_only())
        new = original.parent / "runtime-result.txt"
        new.write_text("runtime data")
        special_new = original.parent / "runtime [*]\tline\n.tmp"
        special_new.write_text("runtime data")
        other = Path(self.ensemble, "work/X/result.txt")
        other.parent.mkdir()
        other.write_text("runtime data")
        for name in ("logs", "adhoc", "managers", "docker-state", "workers", "tmp"):
            path = Path(self.ensemble, name, "runtime-data")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("runtime data")
        self.assert_ok(self.cleanup("--yes"))
        self.assertEqual(user.read_text(), "keep")
        self.assertEqual(original.read_text(), "user work")
        self.assertEqual(special.read_text(), "user special")
        self.assertFalse(special_new.exists())
        self.assertFalse(new.exists())
        self.assertFalse(other.parent.exists())
        self.assertFalse(self.ledger())
        for name in ("logs", "adhoc", "managers", "docker-state", "workers", "tmp"):
            self.assertFalse(Path(self.ensemble, name).exists(), name)

    def test_cleanup_preserves_preexisting_dangling_runtime_link(self):
        link = Path(self.ensemble, "work")
        target = Path(self.home, "unmounted-user-work")
        link.symlink_to(target)
        self.assert_ok(self.opencode_only())
        self.assert_ok(self.cleanup("--yes"))
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.readlink(), target)

    def test_cleanup_runtime_removal_failure_keeps_records_for_retry(self):
        shutil.rmtree(self.ensemble)
        self.assert_ok(self.opencode_only())
        locked = Path(self.ensemble, "unrecorded/locked")
        locked.mkdir(parents=True)
        (locked / "runtime.txt").write_text("runtime data")
        locked.chmod(0o500)
        self.addCleanup(lambda: locked.exists() and locked.chmod(0o700))
        result = self.cleanup("--yes")
        self.assertEqual(result.code, 1, result.stderr)
        self.assertIn(f"dir\t{self.ensemble}", self.ledger())
        self.assertIn(f"agent-tree\t{self.ensemble}", self.ledger())
        locked.chmod(0o700)
        self.assert_ok(self.cleanup("--yes"))
        self.assertFalse(Path(self.ensemble).exists())

    def test_cleanup_final_root_removal_failure_restores_ownership_record(self):
        shutil.rmtree(self.ensemble)
        self.assert_ok(self.opencode_only())
        (self.fakes / "fail-root-remove").touch()
        rm = self.bin / "rm"
        rm.unlink()
        executable(
            rm,
            """#!/bin/sh
if [ "$#" = 2 ] && [ "$1" = -rf ] && [ "$2" = "$HOME/.ensemble" ] && [ -f "$FAKES/fail-root-remove" ]; then
  /bin/rm -f "$FAKES/fail-root-remove" "$HOME/.ensemble/install/added"
  exit 1
fi
exec /bin/rm "$@"
""",
        )
        result = self.cleanup("--yes")
        self.assertEqual(result.code, 1, result.stderr)
        self.assertIn("its record stays for a rerun", result.stderr)
        self.assertEqual(self.ledger(), [f"dir\t{self.ensemble}"])
        self.assert_ok(self.cleanup("--yes"))
        self.assertFalse(Path(self.ensemble).exists())

    def test_cleanup_runtime_data_with_installer_owned_python(self):
        shutil.rmtree(self.ensemble)
        (self.bin / "python3").unlink()
        executable(self.fakes / "apps/python3", f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        self.assert_ok(self.opencode_only())
        self.assertIn("formula\tpython", self.ledger())
        self.runtime_files()
        self.assert_ok(self.cleanup("--yes"))
        self.assertFalse(Path(self.ensemble).exists())
        self.assertFalse((self.root / "opt/homebrew/bin/python3").exists())

    def user_file_after_kept_board(self):
        shutil.rmtree(self.ensemble)
        self.assert_ok(self.opencode_only())
        board = Path(self.ensemble, "board")
        board.mkdir()
        (board / "keep.md").write_text("board data")
        self.assert_ok(self.cleanup("--yes"))
        shutil.rmtree(board)
        original = Path(self.ensemble, "keep.txt")
        original.write_text("preexisting user data")
        self.assert_ok(self.opencode_only())
        return original

    def test_cleanup_retries_preserve_user_file_after_kept_board(self):
        original = self.user_file_after_kept_board()
        (self.fakes / "fail").write_text("brew uninstall node")
        result = self.cleanup("--yes")
        self.assertEqual(result.code, 1, result.stderr)
        self.assertIn(f"agent-tree\t{self.ensemble}", self.ledger())
        self.assertEqual(original.read_text(), "preexisting user data")
        self.assert_ok(self.cleanup("--yes"))
        self.assertEqual(original.read_text(), "preexisting user data")
        self.assert_ok(self.cleanup("--yes"))
        self.assertEqual(original.read_text(), "preexisting user data")
        original.unlink()
        self.assert_ok(self.cleanup("--yes"))
        self.assertFalse(Path(self.ensemble).exists())

    def test_cleanup_repeated_success_preserves_user_file_after_kept_board(self):
        original = self.user_file_after_kept_board()
        for _ in range(2):
            self.assert_ok(self.cleanup("--yes"))
            self.assertEqual(original.read_text(), "preexisting user data")
        second = Path(self.ensemble, "second.txt")
        second.write_text("user data before next install")
        self.assert_ok(self.opencode_only())
        self.runtime_files()
        for _ in range(2):
            self.assert_ok(self.cleanup("--yes"))
            self.assertEqual(original.read_text(), "preexisting user data")
            self.assertEqual(second.read_text(), "user data before next install")
        original.unlink()
        second.unlink()
        self.assert_ok(self.cleanup("--yes"))
        self.assertFalse(Path(self.ensemble).exists())

    def test_failed_baseline_refresh_preserves_user_files_until_install_retry(self):
        original = self.user_file_after_kept_board()
        self.assert_ok(self.cleanup("--yes"))
        second = Path(self.ensemble, "second.txt")
        second.write_text("user data before next install")
        find = self.bin / "find"
        find.unlink()
        executable(find, "#!/bin/sh\nexit 1\n")
        result = self.run_install("--clients", "claude", "--no-docker")
        self.assertEqual(result.code, 1, result.stderr)
        result = self.run_install("--yes", script=CLEANUP)
        self.assertEqual(result.code, 1, result.stderr)
        self.assertIn("path inventory is incomplete", result.stderr)
        self.assertEqual(original.read_text(), "preexisting user data")
        self.assertEqual(second.read_text(), "user data before next install")
        find.unlink()
        find.symlink_to(shutil.which("find"))
        self.assert_ok(self.opencode_only())
        self.assert_ok(self.cleanup("--yes"))
        self.assertEqual(original.read_text(), "preexisting user data")
        self.assertEqual(second.read_text(), "user data before next install")

    def test_cleanup_after_bootstrap_failure_without_python(self):
        shutil.rmtree(self.ensemble)
        (self.bin / "python3").unlink()
        (self.fakes / "fail").write_text(
            "curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh"
        )
        result = self.run_install(
            "--clients", "claude", "--no-docker", answers=[(SUDO_Q, PASSWORD)]
        )
        self.assertEqual(result.code, 1, result.stderr)
        self.assert_ok(self.cleanup("--yes"))
        self.assertFalse(Path(self.ensemble).exists())

    def test_cleanup_keeps_board_with_glob_characters_in_home(self):
        home = Path(self.home, "user[1]*?")
        home.mkdir()
        self.env["HOME"] = str(home)
        self.root = home / "root"
        (self.root / "dev").mkdir(parents=True)
        self.env["ENSEMBLE_INSTALL_ROOT"] = str(self.root)
        self.assert_ok(self.fresh("--clients", "claude", "--no-docker"))
        root = home / ".ensemble"
        board = root / "board"
        board.mkdir()
        issue = board / "keep.md"
        issue.write_text("board data")
        runtime = root / "runtime.txt"
        runtime.write_text("runtime data")
        self.assert_ok(self.cleanup("--yes"))
        self.assertEqual(issue.read_text(), "board data")
        self.assertFalse(runtime.exists())
        self.assertEqual({path.name for path in root.iterdir()}, {"board", "install"})

    def tool_artifacts(self):
        return [
            Path(self.env.get("XDG_DATA_HOME", Path(self.home, ".local/share")), "opentui"),
            Path(self.env.get("XDG_CACHE_HOME", Path(self.home, ".cache")), "gh"),
            Path(self.env.get("XDG_STATE_HOME", Path(self.home, ".local/state")), "gh"),
            Path(
                self.env.get("UV_CACHE_DIR")
                or Path(self.env.get("XDG_CACHE_HOME", Path(self.home, ".cache")), "uv")
            ),
            Path(self.home, ".homebrew/trust.json"),
        ]

    def test_cleanup_tracks_tool_artifacts_and_lists_them(self):
        for cycle in range(2):
            with self.subTest(cycle=cycle):
                self.assert_ok(self.opencode_only())
                for path in self.tool_artifacts():
                    self.assertTrue(path.exists(), str(path))
                    kind = "file" if path.name == "trust.json" else "dir"
                    self.assertIn(f"{kind}\t{path}", self.ledger())
                result = self.cleanup("--yes")
                self.assert_ok(result)
                for path in self.tool_artifacts():
                    self.assertIn(str(path), result.tty)
                    self.assertFalse(path.exists(), str(path))
                self.assertFalse(Path(self.home, ".homebrew").exists())

    def test_cleanup_preserves_preexisting_tool_artifacts(self):
        sentinels = []
        for path in self.tool_artifacts():
            if path.name != "trust.json":
                path.mkdir(parents=True)
                path = path / "user-data"
            else:
                path.parent.mkdir(parents=True)
            path.write_text("user data")
            sentinels.append(path)
        self.assert_ok(self.opencode_only())
        for path in self.tool_artifacts():
            self.assertFalse(any(record.endswith("\t" + str(path)) for record in self.ledger()))
        self.assert_ok(self.cleanup("--yes"))
        for path in sentinels:
            self.assertEqual(path.read_text(), "user data")

    def test_cleanup_tracks_xdg_tool_artifacts_and_uv_override(self):
        for key in ("XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "UV_CACHE_DIR"):
            self.env[key] = str(Path(self.home, key.lower()))
        self.assert_ok(self.opencode_only())
        self.assert_ok(self.cleanup("--yes"))
        for path in self.tool_artifacts():
            self.assertFalse(path.exists(), str(path))

    def test_opencode_cleanup_restores_legacy_backup_without_installed_record(self):
        path = Path(self.home, ".config/opencode/opencode.jsonc")
        path.parent.mkdir(parents=True)
        original = '{"theme": "mine"}\n'
        path.write_text(original)
        self.assert_ok(self.opencode_only())
        state = Path(self.ensemble, "install")
        self.assertFalse(list(state.glob("*.installed*")))
        ledger = state / "added"
        ledger.write_text(
            "".join(
                line
                for line in ledger.read_text().splitlines(keepends=True)
                if not line.startswith("opencode-provider\t")
            )
        )
        result = self.cleanup("--yes", "--password-stdin", stdin=PASSWORD + "\n")
        self.assert_ok(result)
        self.assertEqual(path.read_text(), original)
        self.assert_secret_free(KEY, PASSWORD, result=result)

    def test_opencode_existing_global_provider_is_not_owned(self):
        path = Path(self.home, ".config/opencode/opencode.jsonc")
        path.parent.mkdir(parents=True)
        original = '{"provider":{"lan":{"name":"user-defined"}},"theme":"mine"}'
        path.write_text(original)
        self.assert_ok(self.opencode_only(provider_answer=None))
        self.assertEqual(json.loads(path.read_text())["provider"]["lan"], {"name": "user-defined"})
        self.assertNotIn(f"opencode-provider\t{path}\tlan", self.ledger())
        self.assert_ok(self.cleanup("--yes", "--password-stdin", stdin=PASSWORD + "\n"))
        self.assertEqual(path.read_text(), original)

    def test_opencode_install_accepts_bom_and_duplicate_keys(self):
        path = Path(self.home, ".config/opencode/opencode.jsonc")
        path.parent.mkdir(parents=True)
        for original in ('\ufeff{"theme":"mine"}', '{"theme":"first","theme":"mine"}'):
            with self.subTest(original=original):
                path.write_text(original)
                self.assert_ok(self.opencode_only())
                data = json.loads(path.read_text().lstrip("\ufeff"))
                self.assertEqual(data["theme"], "mine")
                self.assertIn("lan", data["provider"])
                self.assertIn(SPEC, data["plugin"])
                self.assert_ok(self.cleanup("--yes", "--password-stdin", stdin=PASSWORD + "\n"))
                self.assertEqual(path.read_text(), original)

    def test_opencode_install_and_cleanup_preserve_config_symlinks(self):
        conf = Path(self.home, ".config/opencode")
        conf.mkdir(parents=True)
        for filename in ("opencode.json", "opencode.jsonc"):
            with self.subTest(filename=filename):
                target = Path(self.home, "dotfiles", filename)
                target.parent.mkdir(exist_ok=True)
                original = '{"theme":"mine","provider":{"user":{"name":"mine"}}}'
                target.write_text(original)
                path = conf / filename
                path.symlink_to(target)
                self.assert_ok(self.opencode_only())
                self.assertTrue(path.is_symlink())
                self.assertEqual(path.resolve(), target)
                installed = json.loads(target.read_text())
                self.assertIn("lan", installed["provider"])
                self.assertIn(SPEC, installed["plugin"])
                self.assert_ok(self.opencode_rerun())
                self.assertTrue(path.is_symlink())
                self.assertEqual(json.loads(target.read_text()), installed)
                self.assert_ok(self.cleanup("--yes", "--password-stdin", stdin=PASSWORD + "\n"))
                self.assertTrue(path.is_symlink())
                self.assertEqual(path.resolve(), target)
                self.assertEqual(target.read_text(), original)
                path.unlink()

    def test_opencode_provider_declined_then_rerun(self):
        path = Path(self.home, ".config/opencode/opencode.jsonc")
        self.assert_ok(self.opencode_only(provider_answer="n"))
        self.assertNotIn("provider", json.loads(path.read_text()))
        self.assertNotIn(f"opencode-provider\t{path}\tlan", self.ledger())
        self.assertFalse(Path(self.ensemble, "install/opencode-provider.plan").exists())
        self.assert_ok(self.opencode_rerun((WRITE_Q, "y")))
        self.assertIn("lan", json.loads(path.read_text())["provider"])
        self.assertIn(f"opencode-provider\t{path}\tlan", self.ledger())

    def test_opencode_cleanup_keeps_second_global_config(self):
        self.assert_ok(self.opencode_only())
        path = Path(self.home, ".config/opencode/opencode.json")
        original = '{"provider":{"user":{"name":"mine"}},"theme":"mine"}'
        path.write_text(original)
        self.assert_ok(self.cleanup("--yes", "--password-stdin", stdin=PASSWORD + "\n"))
        self.assertEqual(path.read_text(), original)
        self.assertFalse(path.with_suffix(".jsonc").exists())

    def opencode_without_plugin(self):
        flags = ("--clients", "claude,opencode", "--no-docker", "--opencode-provider", "lan")
        flags += ("--opencode-url", "http://localhost/v1", "--opencode-model", "m")
        flags += ("--opencode-context", "65536", "--opencode-key-stdin")
        result = self.run_install(
            *flags,
            stdin=KEY + "\n",
            answers=[
                (SUDO_Q, PASSWORD),
                *[(WRITE_Q, "y")] * 2,
                (WRITE_Q, "n"),
                *[(WRITE_Q, "y")] * 3,
            ],
        )
        self.assert_ok(result)
        path = Path(self.home, ".config/opencode/opencode.jsonc")
        self.assertIn("lan", json.loads(path.read_text())["provider"])
        self.assertFalse(any(call.startswith("opencode plugin") for call in self.calls()))
        return path

    def test_opencode_provider_without_plugin_has_cleanup_ownership(self):
        path = self.opencode_without_plugin()
        self.assert_ok(self.cleanup("--yes", "--password-stdin", stdin=PASSWORD + "\n"))
        self.assertFalse(path.exists())

    def test_opencode_plugin_later_install_preserves_user_baseline(self):
        path = self.opencode_without_plugin()
        user_spec = "ensemble@git+https://github.com/windroc-labs/ensemble.git#user"
        data = json.loads(path.read_text())
        data["plugin"] = [user_spec]
        path.write_text(json.dumps(data))
        self.assert_ok(self.again(answers=[(WRITE_Q, "y")]))
        self.assertEqual(json.loads(path.read_text())["plugin"], [SPEC])
        self.assert_ok(self.cleanup("--yes", "--password-stdin", stdin=PASSWORD + "\n"))
        self.assertEqual(json.loads(path.read_text()), {"plugin": [user_spec]})

    def test_opencode_key_from_stdin(self):
        self.auth_json().parent.mkdir(parents=True)
        self.auth_json().write_text('{"other": {"type": "api", "key": "theirs"}}')
        result = self.run_install(
            "--password-stdin",
            "--opencode-key-stdin",
            "--no-docker",
            "--clients",
            "codex,opencode",
            stdin=f"{PASSWORD}\n{KEY}\n",
            answers=[(WRITE_Q, "y"), *PROVIDER_ANSWERS, *[(WRITE_Q, "y")] * 5],
        )
        self.assert_ok(result)
        data = json.loads(self.auth_json().read_text())
        self.assertEqual(
            data, {"other": {"type": "api", "key": "theirs"}, "lan": {"type": "api", "key": KEY}}
        )
        self.assertEqual(self.auth_json().stat().st_mode & 0o777, 0o600)
        self.assertNotIn("opencode auth login", self.calls())
        self.assertEqual(list(self.auth_json().parent.glob(".auth.json.*")), [])
        self.assert_secret_free(KEY, PASSWORD, result=result)
        result = self.cleanup("--yes", "--password-stdin", stdin=PASSWORD + "\n")
        self.assert_ok(result)
        self.assertEqual(
            json.loads(self.auth_json().read_text()), {"other": {"type": "api", "key": "theirs"}}
        )
        self.assertEqual(self.auth_json().stat().st_mode & 0o777, 0o600)

    def test_opencode_docker_broker_login(self):
        result = self.fresh(
            clients="claude,opencode",
            docker="y",
            opencode=PROVIDER_ANSWERS,
            logins=[
                (TOKEN_Q, TOKEN),
                (PEM_Q, ""),
                (OPENCODE_LOGIN_Q, "lan"),
                (r"API key:", KEY),
            ],
        )
        self.assert_ok(result)
        calls = self.calls()
        login = f"ensemble-broker login opencode lan --auth-file {self.auth_json()}"
        self.assertIn(login, calls)
        self.assertLess(
            calls.index("ensemble-broker login claude"),
            calls.index(login),
        )
        self.assertEqual(calls.count("opencode auth login"), 1)
        self.assertLess(calls.index("opencode auth login"), calls.index(login))
        self.assertLess(
            result.stderr.index("==> broker\n"), result.stderr.index("==> OpenCode key")
        )
        self.assertEqual(json.loads(self.auth_json().read_text())["lan"]["key"], KEY)
        self.assertEqual((self.fakes / "keychain/ensemble-opencode").read_text(), KEY)
        global_config = Path(self.home, ".config/opencode/opencode.jsonc")
        global_before = global_config.read_bytes()
        value = json.loads(global_before)["provider"]["lan"]
        self.assertEqual(value["npm"], "@ai-sdk/openai-compatible")
        self.assertEqual(value["options"]["baseURL"], "http://10.0.0.5:8080/v1")
        self.assertEqual(value["models"]["m1"]["limit"]["context"], 204800)
        self.assertEqual(self.auth_json().stat().st_mode & 0o777, 0o600)
        self.assertNotIn(KEY, result.tty)
        self.assertTrue(self.state()["login_opencode"].endswith(" lan"))
        result = self.again()
        self.assert_ok(result)
        self.assertNotIn(login, self.calls())
        self.assertNotIn("opencode auth login", self.calls())
        self.assertEqual(global_config.read_bytes(), global_before)
        self.assert_secret_free(KEY, TOKEN, result=result)
        (self.fakes / "keychain/ensemble-opencode").unlink()
        self.assert_ok(self.again())
        self.assertIn(login, self.calls())
        self.assertNotIn("opencode auth login", self.calls())
        binary = self.root / "usr/local/libexec/ensemble/ensemble-broker"
        binary.write_text(binary.read_text() + "\n# replacement fake broker build\n")
        self.assert_ok(self.again())
        self.assertIn(login, self.calls())
        self.assertNotIn("opencode auth login", self.calls())
        self.auth_json().unlink()
        self.assert_ok(self.again(answers=[(OPENCODE_LOGIN_Q, "lan"), (r"API key:", KEY)]))
        self.assertNotIn(login, self.calls())
        self.assert_ok(self.cleanup("--yes", "--password-stdin", stdin=PASSWORD + "\n"))
        self.assertFalse(self.auth_json().exists())
        self.assertFalse((self.fakes / "keychain/ensemble-opencode").exists())

    def test_opencode_docker_import_existing_auth_and_preserve_on_cleanup(self):
        self.auth_json().parent.mkdir(parents=True)
        self.auth_json().write_text(json.dumps({"lan": {"type": "api", "key": KEY}}))
        before = self.auth_json().read_bytes()
        result = self.fresh(clients="opencode", docker="y", opencode=PROVIDER_ANSWERS)
        self.assert_ok(result)
        self.assertNotIn("opencode auth login", self.calls())
        self.assertEqual(self.auth_json().read_bytes(), before)
        self.assertEqual((self.fakes / "keychain/ensemble-opencode").read_text(), KEY)
        self.assert_ok(self.cleanup("--yes", "--password-stdin", stdin=PASSWORD + "\n"))
        self.assertEqual(self.auth_json().read_bytes(), before)
        self.assertFalse((self.fakes / "keychain/ensemble-opencode").exists())
        self.assert_secret_free(KEY, PASSWORD, result=result)

    def test_opencode_docker_import_failure_retries_without_another_key(self):
        self.auth_json().parent.mkdir(parents=True)
        self.auth_json().write_text(json.dumps({"lan": {"type": "api", "key": KEY}}))
        login = f"ensemble-broker login opencode lan --auth-file {self.auth_json()}"
        (self.fakes / "fail").write_text(login)
        result = self.fresh(
            clients="opencode",
            docker="y",
            opencode=PROVIDER_ANSWERS,
            logins=[(PEM_Q, ""), (r"Try again\? \[y/N\]", "n")],
        )
        self.assert_ok(result)
        self.assertNotIn("login_opencode", self.state())
        self.assertFalse((self.fakes / "keychain/ensemble-opencode").exists())
        result = self.again()
        self.assert_ok(result)
        self.assertIn(login, self.calls())
        self.assertNotIn("opencode auth login", self.calls())
        self.assertEqual((self.fakes / "keychain/ensemble-opencode").read_text(), KEY)

    def test_foreign_keychain_item_stops_retry_and_names_recovery(self):
        self.assert_ok(
            self.fresh(
                clients="codex,opencode",
                docker="y",
                opencode=PROVIDER_ANSWERS,
                logins=[(PEM_Q, ""), (OPENCODE_LOGIN_Q, "lan"), (r"API key:", KEY)],
            )
        )
        state = Path(self.ensemble, "install/state")
        state.write_text(re.sub(r"(?m)^login_opencode=.*\n", "", state.read_text()))
        (self.fakes / "keychain/foreign-ensemble-opencode").touch()
        for client, account, code in (("opencode", "lan", 0), ("codex", "auth", 1)):
            with self.subTest(client=client):
                if client == "codex":
                    codex = self.broker_path().parent / "codex/state.json"
                    saved = json.loads(codex.read_text())
                    codex.write_text(
                        json.dumps({**saved, "state": "login_unavailable", "repair": "login"})
                    )
                    (self.fakes / "keychain/foreign-ensemble-codex").touch()
                result = self.again()
                self.assertEqual(result.code, code, result.stderr)
                for text in (
                    f"ensemble-{client}/{account}",
                    "-25244",
                    "delete this item",
                    "rerun install.sh",
                ):
                    self.assertIn(text, result.stderr + result.tty)
                self.assertNotIn("Try again?", result.tty)
                self.assertEqual(
                    sum(c.startswith(f"ensemble-broker login {client}") for c in self.calls()), 1
                )
                if client == "opencode":
                    self.assertNotIn("login_opencode", self.state())
                self.assert_secret_free(KEY, PASSWORD, result=result)

    def test_opencode_docker_key_from_stdin(self):
        result = self.run_install(
            "--opencode-key-stdin",
            "--password-stdin",
            "--docker",
            "--clients",
            "opencode",
            stdin=f"{PASSWORD}\n{KEY}\n",
            answers=[
                (WRITE_Q, "y"),
                *PROVIDER_ANSWERS,
                (WRITE_Q, "y"),
                (WRITE_Q, "y"),
                (WRITE_Q, "y"),
                (PEM_Q, ""),
                (WRITE_Q, "y"),
                (WRITE_Q, "y"),
            ],
        )
        self.assert_ok(result)
        self.assertEqual(json.loads(self.auth_json().read_text())["lan"]["key"], KEY)
        self.assertEqual((self.fakes / "keychain/ensemble-opencode").read_text(), KEY)
        self.assertNotIn("opencode auth login", self.calls())
        self.assert_secret_free(KEY, PASSWORD, result=result)

    def test_opencode_docker_preserves_preexisting_broker_on_cleanup(self):
        item = self.fakes / "keychain/ensemble-opencode"
        item.parent.mkdir(parents=True)
        item.write_text(KEY)
        result = self.fresh(
            clients="opencode",
            docker="y",
            opencode=PROVIDER_ANSWERS,
            logins=[(PEM_Q, ""), (OPENCODE_LOGIN_Q, "lan"), (r"API key:", KEY)],
        )
        self.assert_ok(result)
        self.assert_ok(self.cleanup("--yes", "--password-stdin", stdin=PASSWORD + "\n"))
        self.assertFalse(self.auth_json().exists())
        self.assertEqual(item.read_text(), KEY)
        self.assert_secret_free(KEY, PASSWORD, result=result)

    def test_opencode_docker_canceled_login_preserves_preexisting_oauth(self):
        self.auth_json().parent.mkdir(parents=True)
        self.auth_json().write_text(json.dumps({"lan": {"type": "oauth", "access": KEY}}))
        before = self.auth_json().read_bytes()
        (self.fakes / "fail").write_text("opencode auth login")
        result = self.fresh(
            clients="opencode",
            docker="y",
            opencode=PROVIDER_ANSWERS,
            logins=[(PEM_Q, ""), (r"Try again\? \[y/N\]", "n")],
        )
        self.assert_ok(result)
        self.assertEqual(self.auth_json().read_bytes(), before)
        ledger = Path(self.ensemble, "install/added").read_text()
        self.assertNotIn(f"opencode-auth\t{self.auth_json()}\tlan", ledger)
        self.assertFalse(any(c.startswith("ensemble-broker login opencode") for c in self.calls()))
        self.assert_ok(self.cleanup("--yes", "--password-stdin", stdin=PASSWORD + "\n"))
        self.assertEqual(self.auth_json().read_bytes(), before)
        self.assert_secret_free(KEY, PASSWORD, result=result)

    def test_opencode_docker_replaces_unusable_api_entries_before_import(self):
        self.assert_ok(
            self.fresh(
                clients="opencode",
                docker="y",
                opencode=PROVIDER_ANSWERS,
                logins=[(PEM_Q, ""), (OPENCODE_LOGIN_Q, "lan"), (r"API key:", KEY)],
            )
        )
        entries = {
            "oauth": {"type": "oauth", "access": KEY},
            "wrong type": {"type": "api", "key": 42},
            "empty": {"type": "api", "key": ""},
            "whitespace": {"type": "api", "key": KEY + " "},
            "control": {"type": "api", "key": KEY + "\u0000"},
            "too long": {"type": "api", "key": "x" * 16385},
        }
        for name, entry in entries.items():
            with self.subTest(name=name):
                self.auth_json().write_text(json.dumps({"lan": entry}))
                (self.fakes / "keychain/ensemble-opencode").unlink()
                result = self.again(answers=[(OPENCODE_LOGIN_Q, "lan"), (r"API key:", KEY)])
                self.assert_ok(result)
                calls = self.calls()
                login = f"ensemble-broker login opencode lan --auth-file {self.auth_json()}"
                self.assertLess(calls.index("opencode auth login"), calls.index(login))
                self.assertEqual(json.loads(self.auth_json().read_text())["lan"]["key"], KEY)
                self.assertEqual((self.fakes / "keychain/ensemble-opencode").read_text(), KEY)
                self.assertNotIn(KEY, result.tty)
                self.assert_secret_free(KEY, PASSWORD, result=result)
        boundary = "x" * 16384
        self.auth_json().write_text(json.dumps({"lan": {"type": "api", "key": boundary}}))
        (self.fakes / "keychain/ensemble-opencode").unlink()
        result = self.again()
        self.assert_ok(result)
        self.assertNotIn("opencode auth login", self.calls())
        self.assertEqual((self.fakes / "keychain/ensemble-opencode").read_text(), boundary)
        self.assert_secret_free(boundary, PASSWORD, result=result)

    def test_opencode_empty_key_asked_again_or_skipped(self):
        key_q = r"API key:"
        again = r"No lan key stored\. Try again\? \[y/N\]"
        result = self.fresh(
            clients="claude,opencode",
            docker="y",
            opencode=PROVIDER_ANSWERS,
            logins=[
                (TOKEN_Q, TOKEN),
                (PEM_Q, ""),
                (OPENCODE_LOGIN_Q, "lan"),
                (key_q, ""),
                (again, "y"),
                (OPENCODE_LOGIN_Q, "lan"),
                (key_q, KEY),
            ],
        )
        self.assert_ok(result)
        self.assertEqual(self.calls().count("opencode auth login"), 2)
        self.assertEqual((self.fakes / "keychain/ensemble-opencode").read_text(), KEY)
        self.assertIn(
            'The first worker that reaches http://10.0.0.5:8080/v1 makes macOS ask "Allow Docker to'
            ' find devices on local networks?": click Allow',
            result.stderr,
        )
        (self.fakes / "keychain/ensemble-opencode").unlink()
        self.auth_json().unlink()
        result = self.again(answers=[(OPENCODE_LOGIN_Q, "lan"), (key_q, ""), (again, "n")])
        self.assert_ok(result)
        self.assertIn("skip: no lan key: rerun install.sh, or run", result.stderr)
        self.assertFalse((self.fakes / "keychain/ensemble-opencode").exists())

    def test_opencode_tmux_empty_key_skipped(self):
        result = self.fresh(
            "--no-docker",
            clients="claude,opencode",
            opencode=PROVIDER_ANSWERS,
            logins=[(OPENCODE_LOGIN_Q, "lan"), (r"API key:", ""), (r"Try again\? \[y/N\]", "n")],
        )
        self.assert_ok(result)
        self.assertIn("skip: no lan key: rerun install.sh, or run", result.stderr)
        self.assertFalse(self.auth_json().exists())

    def test_opencode_http_warning(self):
        flags = ("--clients", "claude,opencode", "--no-docker", "--opencode-provider", "lan")
        flags += ("--opencode-model", "m", "--opencode-context", "65536", "--opencode-key-stdin")
        for url, warned in (("http://10.0.0.5:8080/v1", True), ("http://localhost:8080/v1", False)):
            self.config().unlink(missing_ok=True)
            Path(self.ensemble, "install/state").unlink(missing_ok=True)
            self.auth_json().unlink(missing_ok=True)
            result = self.run_install(
                *flags,
                "--opencode-url",
                url,
                stdin=KEY + "\n",
                answers=[(SUDO_Q, PASSWORD), *([(WRITE_Q, "y")] * (6 if warned else 1))],
            )
            self.assert_ok(result)
            self.assertEqual("sends the API key in plaintext" in result.stderr, warned, url)

    def test_linear_apps_and_board_jobs(self):
        """A board config completed by hand: only the missing keys are asked, then the jobs installed."""
        result = self.fresh()
        self.assert_ok(result)
        self.assertIn("==> board launchd jobs\n    skip: board not chosen", result.stderr)
        self.config().write_text(self.config().read_text() + "\n" + HAND_CONFIG)
        (self.fakes / "keychain").mkdir(exist_ok=True)
        (self.fakes / "keychain/linear-app-manager-secret").write_text("k-SENTINEL-manager")
        self.linear()
        result = self.again(
            "--board",
            "linear",
            "--launchd",
            answers=[*self.secrets("harness", "researcher", "pm", "engineer"), (WRITE_Q, "y")],
        )
        self.assert_ok(result)
        self.assertEqual(
            (self.fakes / "keychain/linear-app-harness-secret").read_text(), "k-SENTINEL-harness"
        )
        self.assertEqual(
            (self.fakes / "keychain/linear-app-pm-secret").read_text(), "k-SENTINEL-pm"
        )
        calls = self.calls()
        self.assertIn(
            "security add-generic-password -s linear-app-pm-secret -a linear-app-pm-secret -w",
            calls,
        )
        self.assertIn(
            "security add-generic-password -s linear-app-harness-secret -a linear-app-harness-secret -w",
            calls,
        )
        self.assertIn("skip: linear-app-manager-secret in the keychain", result.stderr)
        self.assertIn(
            "==> linear board configuration\n    skip: has the Linear board configuration",
            result.stderr,
        )
        agents = Path(self.home, "Library/LaunchAgents")
        for job in ("router", "promote"):
            plist = (agents / f"com.windroc.ensemble.{job}.plist").read_text()
            self.assertIn(
                f"<string>{self.home}/.ensemble/bin/ensemble-orchestrator</string>", plist
            )
            self.assertIn(f"<string>{self.home}/.ensemble/logs/orchestrator.jsonl</string>", plist)
            self.assertFalse({"@HOME@", "@PATH@", "@BOARD@"} & set(re.findall(r"@[A-Z]+@", plist)))
            path = re.search(r"<key>PATH</key>\s*<string>([^<]*)</string>", plist).group(1)
            self.assertEqual(
                path,
                f"{self.bin}:{self.root}/opt/homebrew/bin:{self.ensemble}/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            )
            self.assertIn(
                f"launchctl bootstrap gui/501 {agents}/com.windroc.ensemble.{job}.plist", calls
            )
        self.assertTrue(Path(self.ensemble, "logs").is_dir())
        self.assert_secret_free("k-SENTINEL", "t-SENTINEL", result=result)
        result = self.again()
        self.assert_ok(result)
        self.assertNotIn("exists; remove it", result.stderr)
        self.assertNotRegex(result.tty, WRITE_Q)
        self.assertFalse([c for c in self.calls() if c.startswith("launchctl")])
        mine = agents / "com.windroc.ensemble.promote.plist"
        mine.write_text("<plist>mine</plist>")
        result = self.again()
        self.assert_ok(result)
        self.assertIn(
            f"skip: {mine} exists; remove it to let install.sh install the job", result.stderr
        )
        self.assertEqual(mine.read_text(), "<plist>mine</plist>")
        self.assertFalse([c for c in self.calls() if c.startswith(("launchctl", "security add"))])

    def test_board_secrets_stored_and_verified(self):
        self.linear()
        result = self.fresh("--board", "linear", "--launchd", then=[*self.secrets(*SLOTS), *BOARD])
        self.assert_ok(result)
        for slot in SLOTS:
            self.assertEqual(
                (self.fakes / f"keychain/linear-app-{slot}-secret").read_text(),
                f"k-SENTINEL-{slot}",
            )
            held = json.loads((self.fakes / f"keychain/linear-app-{slot}-token").read_text())
            self.assertEqual(held["access_token"], f"t-SENTINEL-{slot}")
            self.assertGreater(datetime.fromisoformat(held["expires_at"]), datetime.now(UTC))
            for kind in ("secret", "token"):
                self.assertIn(
                    f"keychain\tlinear-app-{slot}-{kind}\tlinear-app-{slot}-{kind}", self.ledger()
                )
        calls = self.calls()
        self.assertIn(
            "security add-generic-password -s linear-app-pm-secret -a linear-app-pm-secret -w",
            calls,
        )
        self.assertIn(
            "Linear app pm: paste its client secret at security's prompt (nothing echoes)",
            result.stderr,
        )
        viewers = [c for c in calls if c.startswith("curl -q ") and "viewer" in c]
        self.assertEqual(len(viewers), len(SLOTS))  # each app's user id
        tokens = [
            c
            for c in calls
            if c.startswith("curl -q ") and c.endswith("https://api.linear.app/oauth/token")
        ]
        self.assertEqual(len(tokens), len(SLOTS))
        self.assertNotIn("API key", result.tty)
        self.assert_secret_free("k-SENTINEL", "t-SENTINEL", result=result)
        self.assertNotIn("k-SENTINEL", result.tty)
        self.assertNotIn("t-SENTINEL", result.tty)

    def test_board_refused_secret_is_deleted(self):
        self.linear()
        result = self.fresh(
            "--board",
            "linear",
            "--launchd",
            then=[
                *self.secrets("harness", "manager"),
                (SECRET_Q, "k-SENTINEL-wrong"),
                ("Linear OAuth Client ID for researcher:", "client-researcher"),
            ],
        )
        self.assertNotEqual(result.code, 0)
        self.assertIn(
            "install.sh: Linear researcher: token request failed",
            result.stderr,
        )
        self.assertFalse((self.fakes / "keychain/linear-app-researcher-secret").exists())
        self.assertFalse((self.fakes / "keychain/linear-app-researcher-token").exists())
        self.assertTrue((self.fakes / "keychain/linear-app-manager-token").exists())
        self.assert_secret_free("k-SENTINEL", "t-SENTINEL", result=result)
        self.assertNotIn("k-SENTINEL", result.tty)
        self.assertNotIn("t-SENTINEL", result.tty)

    def assert_no_linear(self, result, *, keychain=True):
        self.assertNotRegex(result.tty, SECRET_Q)
        self.assertFalse([c for c in self.calls() if c.startswith("curl -q")])
        if keychain:
            self.assertFalse(
                [c for c in self.calls() if c.startswith("security") and "linear" in c]
            )

    def assert_jobs(self, installed):
        for job in ("router", "promote"):
            target = Path(self.home, f"Library/LaunchAgents/com.windroc.ensemble.{job}.plist")
            self.assertEqual(target.exists(), installed)
            bootstraps = [c for c in self.calls() if c == f"launchctl bootstrap gui/501 {target}"]
            self.assertEqual(bool(bootstraps), installed)
            if installed:
                data = plistlib.loads(target.read_bytes())
                self.assertNotRegex(target.read_text(), r"@(HOME|PATH|BOARD)@")
                self.assertEqual(
                    data["ProgramArguments"],
                    [
                        f"{self.ensemble}/bin/ensemble-orchestrator",
                        job,
                    ],
                )
                self.assertNotIn("ENSEMBLE_BOARD_DIR", data["EnvironmentVariables"])
                self.assertNotIn("local_board_dir", self.state())
        if (
            installed
            and tomllib.loads(self.config().read_text())["orchestrator"]["tracker"] == "local"
        ):
            directory = orchestrator_config._board()
            self.assertEqual(directory, str(Path(self.ensemble, "board").resolve()))
            self.assertEqual(local_board.scan(directory), ())

    def test_board_local_with_jobs(self):
        result = self.fresh(
            "--clients",
            "claude",
            "--no-docker",
            "--board",
            "local",
            "--launchd",
            then=[(WRITE_Q, "y"), (WRITE_Q, "y"), (WRITE_Q, "y")],
        )
        self.assert_ok(result)
        self.assertEqual(
            tomllib.loads(self.config().read_text())["orchestrator"]["tracker"], "local"
        )
        self.assert_jobs(installed=True)
        self.assert_no_linear(result)
        before = self.config().read_text()
        result = self.again()
        self.assert_ok(result)
        self.assertEqual(self.config().read_text(), before)
        self.assertEqual(
            (self.state()["board_tracker"], self.state()["board_jobs"]), ("local", "yes")
        )
        self.assertNotRegex(result.tty, WRITE_Q)
        self.assertFalse([c for c in self.calls() if c.startswith("launchctl")])
        self.assert_no_linear(result)

    def test_board_local_without_jobs(self):
        result = self.fresh(
            "--clients",
            "claude",
            "--no-docker",
            "--board",
            "local",
            "--no-launchd",
            then=[(WRITE_Q, "y"), (WRITE_Q, "y")],
        )
        self.assert_ok(result)
        self.assertEqual(
            tomllib.loads(self.config().read_text())["orchestrator"]["tracker"], "local"
        )
        self.assert_jobs(installed=False)
        self.assert_no_linear(result)
        self.assertEqual(local_board.scan(str(Path(self.ensemble, "board"))), ())
        result = self.again()
        self.assert_ok(result)
        self.assertEqual(self.state()["board_jobs"], "no")
        self.assert_jobs(installed=False)
        self.assert_no_linear(result)

    def test_board_local_tracker_inferred(self):
        self.assert_ok(self.fresh())
        self.config().write_text(
            self.config().read_text() + '\n[orchestrator]\ntracker = "local"\n'
        )
        result = self.run_install(
            "--launchd",
            answers=[
                (SUDO_Q, PASSWORD),
                (MENU_Q, menu(("claude", "codex", "board"))),
                (WRITE_Q, "y"),
                (WRITE_Q, "y"),
            ],
        )
        self.assert_ok(result)
        self.assert_jobs(installed=True)
        self.assert_no_linear(result)
        self.assertEqual(self.state()["board_tracker"], "local")
        self.assertNotIn("Board tracker [local/linear]", result.tty)
        result = self.again()
        self.assert_ok(result)
        self.assertEqual(self.state()["board_jobs"], "yes")
        self.assert_no_linear(result)

    def test_board_switch_from_linear_to_local(self):
        result = self.configured(HAND_CONFIG, board="local", then=[(WRITE_Q, "y")] * 3)
        self.assert_ok(result)
        data = tomllib.loads(self.config().read_text())["orchestrator"]
        self.assertEqual(data["tracker"], "local")
        self.assertEqual(data["linear"]["team"], uuid(1))
        self.assert_jobs(installed=True)
        self.assert_no_linear(result)
        before = self.config().read_text()
        result = self.again("--no-board")
        self.assert_ok(result)
        self.assertEqual(self.config().read_text(), before)

        self.assert_no_linear(result)
        self.assertFalse([c for c in self.calls() if c.startswith("launchctl")])
        for job in ("router", "promote"):
            self.assertTrue(
                Path(self.home, f"Library/LaunchAgents/com.windroc.ensemble.{job}.plist").exists()
            )

    def test_board_tracker_state_precedes_config(self):
        self.assert_ok(self.fresh("--board", "local", "--no-launchd", then=[(WRITE_Q, "y")] * 2))
        self.config().write_text(
            self.config().read_text().replace('tracker = "local"', 'tracker = "linear"')
        )
        result = self.again(answers=[(WRITE_Q, "y")])
        self.assert_ok(result)
        self.assertEqual(self.state()["board_tracker"], "local")
        self.assertEqual(
            tomllib.loads(self.config().read_text())["orchestrator"]["tracker"], "local"
        )
        self.assertNotIn("Board tracker [local/linear]", result.tty)
        self.assert_no_linear(result)

    def test_board_local_declined(self):
        result = self.fresh(
            "--clients",
            "claude",
            "--no-docker",
            "--board",
            "local",
            "--launchd",
            then=[(WRITE_Q, "n")],
        )
        self.assert_ok(result)
        self.assertNotIn("orchestrator", tomllib.loads(self.config().read_text()))
        self.assert_jobs(installed=False)
        self.assert_no_linear(result)
        result = self.again(answers=[(WRITE_Q, "y")] * 3)
        self.assert_ok(result)
        self.assert_jobs(installed=True)
        self.assert_no_linear(result)

    def test_board_switch_from_local_to_linear(self):
        self.linear()
        existing = HAND_CONFIG.replace('tracker = "linear"', 'tracker = "local"')
        result = self.configured(
            existing,
            then=[
                *self.secrets(*SLOTS, config=existing),
                (PROJECT_Q, "acme/widget"),
                (WRITE_Q, "y"),
                (WRITE_Q, "y"),
            ],
        )
        self.assert_ok(result)
        self.assertEqual(
            tomllib.loads(self.config().read_text())["orchestrator"]["tracker"], "linear"
        )
        orchestrator_config.load()
        self.assert_jobs(installed=True)
        self.assert_secret_free("k-SENTINEL", "t-SENTINEL", result=result)

    def test_board_local_initialization_declined(self):
        result = self.fresh(
            "--clients",
            "claude",
            "--no-docker",
            "--board",
            "local",
            "--launchd",
            then=[(WRITE_Q, "y"), (WRITE_Q, "n")],
        )
        self.assert_ok(result)
        self.assertFalse(Path(self.ensemble, "board").exists())
        self.assert_jobs(installed=False)
        self.assert_no_linear(result)
        result = self.again(answers=[(WRITE_Q, "y"), (WRITE_Q, "y")])
        self.assert_ok(result)
        self.assert_jobs(installed=True)

    def test_board_local_default_path_and_cleanup(self):
        directory = Path(self.ensemble, "board")
        ignored = Path(self.tmp, "new parent", "board & work")
        self.env["ENSEMBLE_BOARD_DIR"] = str(ignored)
        result = self.fresh(
            "--clients",
            "claude",
            "--no-docker",
            "--board",
            "local",
            "--launchd",
            then=[(WRITE_Q, "y")] * 3,
        )
        self.assert_ok(result)
        self.assert_jobs(installed=True)
        self.assert_no_linear(result)
        for path in (directory, directory / "unassigned"):
            self.assertIn(f"board-dir\t{path}", self.ledger())
        self.assertFalse(ignored.parent.exists())
        self.assertNotIn("local_board_dir", self.state())
        del self.env["ENSEMBLE_BOARD_DIR"]
        result = self.again()
        self.assert_ok(result)
        self.assertNotRegex(result.tty, WRITE_Q)
        self.assertNotIn("local_board_dir", self.state())
        result = self.cleanup("--yes")
        self.assert_ok(result)
        self.assertFalse(directory.exists())

    def test_board_local_cleanup_preserves_existing_root(self):
        directory = Path(self.ensemble, "board")
        directory.mkdir(parents=True)
        result = self.fresh(
            "--clients",
            "claude",
            "--no-docker",
            "--board",
            "local",
            "--no-launchd",
            then=[(WRITE_Q, "y"), (WRITE_Q, "y")],
        )
        self.assert_ok(result)
        self.assertEqual(local_board.scan(str(directory)), ())
        self.assertNotIn(f"board-dir\t{directory}", self.ledger())
        self.assertIn(f"board-dir\t{directory / 'unassigned'}", self.ledger())
        self.assert_ok(self.cleanup("--yes"))
        self.assertTrue(directory.is_dir())
        self.assertFalse((directory / "unassigned").exists())

    def test_board_local_mkdir_failure_stops_before_jobs(self):
        blocked = Path(self.ensemble, "board")
        blocked.parent.mkdir(parents=True, exist_ok=True)
        blocked.write_text("keep")
        result = self.fresh(
            "--clients",
            "claude",
            "--no-docker",
            "--board",
            "local",
            "--launchd",
            then=[(WRITE_Q, "y"), (WRITE_Q, "y")],
        )
        self.assertEqual(result.code, 1)
        self.assertEqual(blocked.read_text(), "keep")
        self.assertNotIn(f"board-dir\t{blocked}", self.ledger())
        self.assert_jobs(installed=False)
        self.assert_no_linear(result)

    def test_board_local_cleanup_keeps_data_and_continues(self):
        shutil.rmtree(self.ensemble)
        result = self.fresh(
            "--clients",
            "claude",
            "--no-docker",
            "--board",
            "local",
            "--launchd",
            then=[(WRITE_Q, "y")] * 3,
        )
        self.assert_ok(result)
        board = Path(self.ensemble, "board")
        data = board / "unassigned/keep.txt"
        data.write_text("keep")
        runtime = board / ".state/router.json"
        runtime.parent.mkdir()
        runtime.write_text("{}")
        key = self.fakes / "keychain/linear-app-pm-secret"
        key.parent.mkdir(exist_ok=True)
        key.write_text("fake key")
        ledger = Path(self.ensemble, "install/added")
        ledger.write_text(
            ledger.read_text() + "keychain\tlinear-app-pm-secret\tlinear-app-pm-secret\n"
        )
        self.assertIn(f"board-dir\t{board}", self.ledger())
        self.assertIn(f"dir\t{self.ensemble}/bin", self.ledger())
        self.assertIn(f"dir\t{self.ensemble}/logs", self.ledger())
        result = self.cleanup("--yes")
        self.assert_ok(result)
        self.assertEqual(data.read_text(), "keep")
        self.assertEqual(runtime.read_text(), "{}")
        for directory in (board, data.parent):
            self.assertEqual(result.stderr.count(f"kept {directory}: not empty"), 1)
        self.assertFalse(key.exists())
        self.assertIn(
            "security delete-generic-password -s linear-app-pm-secret -a linear-app-pm-secret",
            self.calls(),
        )
        self.assertIn("claude plugin uninstall ensemble@windroc", self.calls())
        self.assertFalse(self.config().exists())
        self.assertEqual(self.ledger(), [f"dir\t{self.ensemble}"])
        for job in ("router", "promote"):
            self.assertFalse(
                Path(self.home, f"Library/LaunchAgents/com.windroc.ensemble.{job}.plist").exists()
            )
        self.assertFalse(Path(self.ensemble, "bin/ensemble-orchestrator").exists())
        data.unlink()
        runtime.unlink()
        runtime.parent.rmdir()
        data.parent.rmdir()
        board.rmdir()
        result = self.cleanup("--yes")
        self.assert_ok(result)
        self.assertFalse(Path(self.ensemble).exists())

    def test_board_local_cleanup_keeps_router_state(self):
        shutil.rmtree(self.ensemble)
        result = self.fresh(
            "--clients",
            "claude",
            "--no-docker",
            "--board",
            "local",
            "--no-launchd",
            then=[(WRITE_Q, "y")] * 2,
        )
        self.assert_ok(result)
        board = Path(self.ensemble, "board")
        state = board / ".state/router.json"
        state.parent.mkdir()
        state.write_text("{}")
        result = self.cleanup("--yes")
        self.assert_ok(result)
        self.assertEqual(state.read_text(), "{}")
        self.assertFalse((board / "unassigned").exists())
        self.assertEqual(result.stderr.count(f"kept {board}: not empty"), 1)
        self.assertEqual(self.ledger(), [f"dir\t{self.ensemble}"])
        self.assertFalse(self.config().exists())

    def test_board_linear_without_jobs(self):
        self.linear()
        result = self.fresh(
            "--clients",
            "claude",
            "--no-docker",
            "--board",
            "linear",
            "--no-launchd",
            then=[*self.secrets(*SLOTS), *BOARD[:-1]],
        )
        self.assert_ok(result)
        self.assertEqual(
            tomllib.loads(self.config().read_text())["orchestrator"]["tracker"], "linear"
        )
        orchestrator_config.load()
        self.assert_jobs(installed=False)
        self.assert_secret_free("k-SENTINEL", "t-SENTINEL", result=result)
        result = self.again()
        self.assert_ok(result)
        self.assert_jobs(installed=False)
        self.assert_no_linear(result, keychain=False)

    def test_board_none_leaves_tracker_and_jobs_alone(self):
        self.assert_ok(self.fresh("--clients", "claude", "--no-docker", "--no-board"))
        self.assertNotIn("orchestrator", tomllib.loads(self.config().read_text()))
        self.assert_jobs(installed=False)
        result = self.again()
        self.assert_ok(result)
        self.assertEqual(self.state()["board"], "no")
        self.assert_jobs(installed=False)
        self.assert_no_linear(result)

    def test_board_interactive_tracker_and_jobs(self):
        result = self.run_install(
            answers=[
                (SUDO_Q, PASSWORD),
                (WRITE_Q, "y"),
                (MENU_Q, menu(("claude", "codex", "board"))),
                (r"Board tracker \[local/linear\] \[local\]:", "invalid"),
                (r"Board tracker \[local/linear\] \[local\]:", ""),
                (r"Install board launchd jobs\? \[y/N\]", "n"),
                (WRITE_Q, "y"),
                (WRITE_Q, "y"),
                (WRITE_Q, "y"),
                (WRITE_Q, "y"),
            ]
        )
        self.assert_ok(result)
        self.assertEqual(
            (self.state()["board_tracker"], self.state()["board_jobs"]), ("local", "no")
        )
        self.assert_jobs(installed=False)
        self.assert_no_linear(result)

    def test_board_flags_reject_invalid_choices(self):
        for args in (
            ("--board",),
            ("--board", "yes"),
            ("--launchd", "--no-launchd"),
            ("--board", "local", "--no-board"),
        ):
            result = self.run_install(*args, tty=False)
            self.assertEqual(result.code, 2, args)
            self.assertEqual(self.calls(), [])

    def test_board_interactive_linear_with_jobs(self):
        self.linear()
        result = self.run_install(
            answers=[
                (SUDO_Q, PASSWORD),
                (WRITE_Q, "y"),
                (MENU_Q, menu(("claude", "codex", "board"))),
                (r"Board tracker \[local/linear\] \[local\]:", "linear"),
                (r"Install board launchd jobs\? \[y/N\]", "y"),
                (WRITE_Q, "y"),
                (WRITE_Q, "y"),
                *self.secrets(*SLOTS),
                *BOARD,
            ]
        )
        self.assert_ok(result)
        self.assertEqual(
            (self.state()["board_tracker"], self.state()["board_jobs"]), ("linear", "yes")
        )
        orchestrator_config.load()
        self.assert_jobs(installed=True)
        self.assert_secret_free("k-SENTINEL", "t-SENTINEL", result=result)
        result = self.again()
        self.assert_ok(result)
        self.assertNotIn("Board tracker [local/linear]", result.tty)
        self.assertNotIn("Install board launchd jobs?", result.tty)
        self.assert_no_linear(result, keychain=False)

    def test_board_jobs_require_board(self):
        self.assert_ok(self.fresh("--clients", "claude", "--no-docker"))
        before = self.snapshot()
        for flag in ("--launchd", "--no-launchd"):
            for board_flags in (("--no-board",), ()):
                result = self.again("--clients", "claude", "--no-docker", *board_flags, flag)
                self.assertEqual(result.code, 2)
                self.assertIn("requires a board", result.stderr)
                self.assertEqual(self.snapshot(), before)
                self.assertNotRegex(result.tty, WRITE_Q)
                self.assert_jobs(installed=False)
                self.assert_no_linear(result)

    def test_board_jobs_require_board_on_a_fresh_install(self):
        result = self.run_install(
            "--clients",
            "claude",
            "--no-docker",
            "--no-board",
            "--launchd",
            answers=[(SUDO_Q, PASSWORD), (WRITE_Q, "y")],
        )
        self.assertEqual(result.code, 2)
        self.assertIn("requires a board", result.stderr)
        self.assertFalse(self.config().exists())
        self.assertEqual(self.plugin_calls(), [])
        self.assert_jobs(installed=False)
        self.assert_no_linear(result)

    def test_board_jobs_rejected_when_checklist_leaves_board_off(self):
        result = self.run_install(
            "--launchd",
            answers=[
                (SUDO_Q, PASSWORD),
                (WRITE_Q, "y"),
                (MENU_Q, ""),
            ],
        )
        self.assertEqual(result.code, 2)
        self.assertIn("requires a board", result.stderr)

    def test_installed_board_jobs_rejected_before_privileged_work(self):
        self.assert_ok(self.fresh("--board", "local", "--no-launchd", then=[(WRITE_Q, "y")] * 2))
        for python_on_path in (True, False):
            with self.subTest(python_on_path=python_on_path):
                if not python_on_path:
                    (self.bin / "python3").unlink()
                    (self.root / "opt/homebrew/bin/python3").symlink_to(sys.executable)
                before = self.snapshot()
                result = self.run_install(
                    "--launchd",
                    answers=[(MENU_Q, menu(("claude", "codex"), ("claude", "codex", "board")))],
                )
                self.assertEqual(result.code, 2)
                self.assertIn("requires a board", result.stderr)
                self.assertEqual(self.snapshot(), before)
                self.assertFalse([c for c in self.calls() if c.startswith(("sudo", "brew"))])
                self.assertNotIn("dependencies", result.stderr)

    def test_board_tracker_inference_handles_invalid_config(self):
        self.assert_ok(self.fresh("--clients", "claude", "--no-docker"))
        before = self.config().read_text()
        for broken in ("[broken\n", "orchestrator = 7\n" + before):
            self.config().write_text(broken)
            state = Path(self.ensemble, "install/state")
            state.write_text(
                "\n".join(
                    line
                    for line in state.read_text().splitlines()
                    if not line.startswith("board_tracker=")
                )
                + "\n"
            )
            result = self.run_install(
                "--clients",
                "claude",
                "--no-docker",
                "--board",
                "local",
                "--no-launchd",
                answers=[(SUDO_Q, PASSWORD)],
            )
            self.assertNotIn("Traceback", result.stderr)
            self.assertEqual(self.config().read_text(), broken)
            self.assert_jobs(installed=False)
            self.assert_no_linear(result)

    def test_board_tracker_inference_keeps_config_columns_separate(self):
        Path(self.ensemble).mkdir(parents=True, exist_ok=True)
        self.config().write_text(
            '[delegate]\nclient = "codex"\n[orchestrator]\ntracker = "local"\n'
        )
        ticked = ("codex", "board")
        result = self.run_install(
            "--launchd",
            answers=[
                (SUDO_Q, PASSWORD),
                (WRITE_Q, "y"),
                (MENU_Q, menu(ticked, ("codex",))),
                (WRITE_Q, "y"),
                (WRITE_Q, "y"),
                (WRITE_Q, "y"),
            ],
        )
        self.assert_ok(result)
        self.assertEqual(self.state()["clients"], "codex")
        self.assertEqual(self.state()["docker"], "no")
        self.assertEqual(self.state()["board_tracker"], "local")
        self.assertNotIn("Board tracker [local/linear]", result.tty)
        self.assert_jobs(installed=True)
        self.assert_no_linear(result)

    def test_board_tracker_with_control_characters_is_not_recognized(self):
        self.assert_ok(self.fresh("--clients", "claude", "--no-docker"))
        before = self.config().read_text()
        for tracker in (r"invalid\tlocal", r"local\n", r"linear\n"):
            with self.subTest(tracker=tracker):
                text = before + f'\n[orchestrator]\ntracker = "{tracker}"\n'
                self.config().write_text(text)
                result = self.again("--board", "local", "--launchd", answers=[(WRITE_Q, "n")])
                self.assert_ok(result)
                self.assertEqual(self.config().read_text(), text)
                self.assertIn("skip: local tracker not configured", result.stderr)
                self.assertFalse(Path(self.ensemble, "board").exists())
                self.assertNotIn("board-dir\t", self.ledger())
                self.assert_jobs(installed=False)
                self.assert_no_linear(result)

    def test_board_custom_workflow_roles(self):
        self.linear(
            labels=[*LINEAR["labels"], label(46, "foreign-task", 40)],
        )
        Path(self.home, "wf.toml").write_text(
            "".join(
                f'[[stage]]\nrole = "{role}"\ninput = "build"\n\n'
                for role in ("researcher", "engineer", "reviewer")
            )
        )
        slots = ("harness", "manager", "researcher", "engineer", "reviewer")
        board = [(PROJECT_Q, ""), (WRITE_Q, "y"), (WRITE_Q, "y")]  # no document role: no docs repo
        result = self.configured(
            '[orchestrator]\nworkflow = "~/wf.toml"\n', then=[*self.secrets(*slots), *board]
        )
        self.assert_ok(result)
        self.assertEqual(
            (self.fakes / "keychain/linear-app-reviewer-secret").read_text(), "k-SENTINEL-reviewer"
        )
        self.assertFalse([c for c in self.calls() if "linear-app-pm" in c])
        roles = self.board_config()["roles"]
        self.assertEqual(sorted(roles), ["engineer", "researcher", "reviewer"])
        self.assertEqual(roles["reviewer"], app_config("reviewer"))
        self.assertEqual(
            self.board_config()["task_labels"], {"build": uuid(41), "light-build": uuid(42)}
        )

    def test_board_tasks_from_team_dirs_charter(self):
        self.linear(
            labels=[*LINEAR["labels"], label(46, "triage", 40)],
        )
        team = Path(self.home, "custom-team")
        (team / "roles").mkdir(parents=True)
        (team / "roles/reviewer.md").write_text(
            "# Reviewer\n\n## Tasks\n\n- `triage`: Review.\n\n## Rules\n- `build`: ignored.\n"
        )
        Path(self.home, "wf.toml").write_text('[[stage]]\nrole = "reviewer"\ninput = "build"\n')
        slots = ("harness", "manager", "reviewer")
        result = self.configured(
            '[delegate]\nteam_dirs = ["~/custom-team"]\n[orchestrator]\nworkflow = "~/wf.toml"\n',
            then=[*self.secrets(*slots), (PROJECT_Q, ""), (WRITE_Q, "y"), (WRITE_Q, "y")],
        )
        self.assert_ok(result)
        self.assertEqual(self.board_config()["task_labels"], {"triage": uuid(46)})

    def test_board_duplicate_charter_is_rejected(self):
        self.linear()
        team = Path(self.home, "custom-team")
        (team / "roles").mkdir(parents=True)
        (team / "roles/engineer.md").write_text("# Engineer\n\n## Tasks\n- `triage`: Review.\n")
        result = self.configured(
            '[delegate]\nteam_dirs = ["~/custom-team"]\n',
            then=self.secrets(*SLOTS),
        )
        self.assertNotEqual(result.code, 0)
        self.assertIn("charter engineer is defined more than once", result.stderr)
        self.assertNotIn("[orchestrator", self.config().read_text())
        self.assertFalse([c for c in self.calls() if c.startswith("launchctl")])

    def board_config(self):
        return tomllib.loads(self.config().read_text())["orchestrator"]["linear"]

    def assert_board_stopped(self, result, said):
        self.assertNotEqual(result.code, 0)
        self.assertIn(f"install.sh: {said}", result.stderr)
        self.assertNotIn("[orchestrator", self.config().read_text())
        self.assertFalse([c for c in self.calls() if c.startswith("launchctl")])
        self.assert_secret_free("k-SENTINEL", "t-SENTINEL", result=result)

    def test_board_fresh_install(self):
        """Secrets, the config from Linear (shown before writing) and the jobs; a rerun redoes none of it."""
        self.linear(labels=[*LINEAR["labels"], label(46, "design", 40)])
        result = self.fresh("--board", "linear", "--launchd", then=[*self.secrets(*SLOTS), *BOARD])
        self.assert_ok(result)
        data = tomllib.loads(self.config().read_text())
        self.assertEqual(data["orchestrator"]["tracker"], "linear")
        linear = data["orchestrator"]["linear"]
        self.assertEqual(linear["team"], uuid(1))
        self.assertEqual(linear["states"], {k: uuid(10 + i) for i, k in enumerate(STATE_KEYS)})
        self.assertEqual(linear["task_label_group"], uuid(40))
        self.assertEqual(linear["task_labels"], {"build": uuid(41), "light-build": uuid(42)})
        self.assertEqual(linear["agent_label_group"], uuid(43))
        self.assertEqual(linear["agent_labels"], {"claude": uuid(44)})
        self.assertEqual(linear["harness"], app_config("harness"))
        self.assertEqual(linear["manager"], app_config("manager"))
        self.assertLessEqual(set(linear), orchestrator_config.LINEAR_KEYS)
        self.assertEqual(linear["human_members"], ["me@example.com"])
        self.assertEqual(
            linear["roles"],
            {r: app_config(r) for r in SLOTS[2:]},
        )
        self.assertEqual(linear["project_repos"], {uuid(30): "acme/widget"})
        # the config gates of router --dry-run, promote --dry-run and issue list
        cfg = orchestrator_config.load()
        orchestrator_config.linear_settings(cfg, "router")
        orchestrator_config.linear_settings(cfg, "promote")
        orchestrator_config.manager_app(cfg.linear)
        self.assertIn(
            "    Tasks label design: not a task of the board's roles; left out", result.stderr
        )
        outputs = {r: data["delegate"]["roles"][r]["output"] for r in ("researcher", "pm")}
        docs = {"type": "github", "repo": "acme/docs", "branch": "main"}
        self.assertEqual(
            outputs,
            {"researcher": {**docs, "dir": "Research/"}, "pm": {**docs, "dir": "Product Design/"}},
        )
        orchestrator_config.load()  # the router's config load accepts it (HOME is this test's)
        shown = result.tty.index('[orchestrator.linear] team = "' + uuid(1) + '"')
        self.assertLess(shown, result.tty.index("Library/LaunchAgents:"))
        agents = Path(self.home, "Library/LaunchAgents")
        for job in ("router", "promote"):
            self.assertTrue((agents / f"com.windroc.ensemble.{job}.plist").is_file())
            self.assertIn(
                f"launchctl bootstrap gui/501 {agents}/com.windroc.ensemble.{job}.plist",
                self.calls(),
            )
        self.assert_secret_free("k-SENTINEL", "t-SENTINEL", result=result)
        self.assertNotIn("k-SENTINEL", result.tty)
        self.assertNotIn("t-SENTINEL", result.tty)
        config = self.config().read_text()
        result = self.again()
        self.assert_ok(result)
        for slot in SLOTS:
            self.assertIn(f"skip: linear-app-{slot}-secret in the keychain", result.stderr)
        for said in ("has the Linear board configuration", "router installed", "promote installed"):
            self.assertIn(f"skip: {said}", result.stderr)
        changing = ("curl -q", "security add", "launchctl")
        self.assertFalse([c for c in self.calls() if c.startswith(changing)])
        self.assertEqual(self.config().read_text(), config)

    def test_board_config_declined(self):
        self.linear()
        board = [*self.secrets(*SLOTS), (PROJECT_Q, ""), *DOCS, (WRITE_Q, "n")]
        result = self.fresh("--board", "linear", "--launchd", then=board)
        self.assert_ok(result)
        self.assertIn("skip: declined", result.stderr)
        self.assertNotIn("[orchestrator", self.config().read_text())
        self.assertIn(
            "==> board launchd jobs\n    skip: board tracker not configured", result.stderr
        )
        self.assertFalse([c for c in self.calls() if c.startswith("launchctl")])

    def test_board_fills_only_missing(self):
        self.linear()
        mine = (
            f'[orchestrator.linear]\nteam = "{uuid(1)}"\n\n'
            '[orchestrator.linear.harness]\nsecret_service = "my-harness"\n\n'
            f'[orchestrator.linear.task_labels]\nbuild = "{uuid(99)}"\n'
        )
        result = self.configured(mine, then=[*self.secrets(*SLOTS), *BOARD])
        self.assert_ok(result)
        self.assertEqual((self.fakes / "keychain/my-harness").read_text(), "k-SENTINEL-harness")
        linear = self.board_config()
        self.assertEqual(
            (linear["team"], linear["harness"]["secret_service"]), (uuid(1), "my-harness")
        )
        self.assertEqual(linear["task_labels"], {"build": uuid(99), "light-build": uuid(42)})
        self.assertFalse([c for c in self.calls() if "teams(" in c])

    def test_board_docs_repo_reused(self):
        self.linear()
        mine = '[delegate.roles.pm.output]\ntype = "github"\nrepo = "me/docs"\nbranch = "trunk"\ndir = "PRD/"\n'
        board = [(PROJECT_Q, ""), (WRITE_Q, "y"), (WRITE_Q, "y")]
        result = self.configured(mine, then=[*self.secrets(*SLOTS), *board])
        self.assert_ok(result)
        roles = tomllib.loads(self.config().read_text())["delegate"]["roles"]
        self.assertEqual(roles["pm"]["output"]["dir"], "PRD/")
        self.assertEqual(
            roles["researcher"]["output"],
            {"type": "github", "repo": "me/docs", "branch": "trunk", "dir": "Research/"},
        )
        self.assertNotRegex(result.tty, DOCS_Q)

    def test_board_team_asked_among_several(self):
        ops = {**LINEAR["teams"][0], "id": uuid(2), "key": "OPS", "projects": []}
        self.linear(teams=[LINEAR["teams"][0], ops])
        team_q = r"Linear team for the board \(ENG, OPS\):"
        board = [(team_q, "NOPE"), (team_q, "OPS"), *DOCS, (WRITE_Q, "y"), (WRITE_Q, "y")]
        result = self.fresh("--board", "linear", "--launchd", then=[*self.secrets(*SLOTS), *board])
        self.assert_ok(result)
        self.assertEqual(self.board_config()["team"], uuid(2))

    def test_board_missing_state_stops(self):
        team = LINEAR["teams"][0]
        states = [s for s in team["states"] if s["name"] != "Handoff"]
        self.linear(teams=[{**team, "states": states}])
        result = self.fresh("--board", "linear", "--launchd", then=self.secrets(*SLOTS))
        self.assert_board_stopped(
            result, "Linear team ENG lacks states: Handoff (docs/credentials.md#linear-presets)"
        )

    def test_board_without_tasks_group_stops(self):
        self.linear(labels=[x for x in LINEAR["labels"] if x["name"] != "Tasks"])
        result = self.fresh("--board", "linear", "--launchd", then=self.secrets(*SLOTS))
        self.assert_board_stopped(
            result,
            "Linear team ENG has no Tasks label group (docs/credentials.md#linear-presets)",
        )

    def test_board_same_app_stops(self):
        self.linear(
            apps={
                **LINEAR["apps"],
                "client-pm": {
                    **LINEAR["apps"]["client-pm"],
                    "user_id": app_config("researcher")["user_id"],
                },
            }
        )
        result = self.fresh("--board", "linear", "--launchd", then=self.secrets(*SLOTS[:-1]))
        self.assert_board_stopped(
            result, "Linear pm: the apps for researcher and pm are the same Linear app"
        )

    def test_board_existing_bad_secret_kept(self):
        (self.fakes / "keychain").mkdir()
        (self.fakes / "keychain/linear-app-harness-secret").write_text("k-SENTINEL-stale")
        self.linear()
        result = self.fresh(
            "--board",
            "linear",
            "--launchd",
            then=[("Linear OAuth Client ID for harness:", "client-harness")],
        )
        self.assert_board_stopped(
            result,
            "Linear harness: token request failed",
        )
        self.assertEqual(
            (self.fakes / "keychain/linear-app-harness-secret").read_text(), "k-SENTINEL-stale"
        )

    def test_board_hostile_linear_names_stop(self):
        """Linear's strings are checked before they reach the terminal or the config."""
        team = LINEAR["teams"][0]
        hostile = "W\x1b]52;c;aGk=\x07"
        self.linear(teams=[{**team, "projects": [{"id": uuid(30), "name": hostile}]}])
        result = self.fresh("--board", "linear", "--launchd", then=self.secrets(*SLOTS))
        self.assert_board_stopped(result, f"Linear returned an invalid project name ({uuid(30)})")
        members = [*team["members"], {"email": "x\x1b[2J@example.com", "active": True}]
        self.linear(teams=[{**team, "members": members}])
        again = self.again(
            answers=[(f"Linear OAuth Client ID for {slot}:", f"client-{slot}") for slot in SLOTS]
        )
        self.assert_board_stopped(again, "Linear returned an invalid member email")
        for text in (result.stdout, result.stderr, result.tty, again.stderr, again.tty):
            self.assertNotIn("\x1b]52", text)
            self.assertNotIn("\x1b[2J@", text)

    def test_board_quoted_label_written(self):
        quoted, astral = 'a" = "b', "\U0001f916 bot"
        labels = [*LINEAR["labels"], label(46, quoted, 43), label(47, astral, 43)]
        self.linear(labels=labels)
        result = self.fresh("--board", "linear", "--launchd", then=[*self.secrets(*SLOTS), *BOARD])
        self.assert_ok(result)
        agents = self.board_config()["agent_labels"]
        self.assertEqual((agents[quoted], agents[astral]), (uuid(46), uuid(47)))

    def test_board_unreadable_workflow_stops(self):
        result = self.configured('[orchestrator]\nworkflow = "~/missing.toml"\n')
        self.assertNotEqual(result.code, 0)
        self.assertIn(
            f"install.sh: {self.home}/missing.toml: cannot read its [[stage]] roles", result.stderr
        )
        self.assertFalse([c for c in self.calls() if c.startswith(("security add", "launchctl"))])

    def ledger(self):
        path = Path(self.ensemble, "install/added")
        return path.read_text().splitlines() if path.exists() else []

    def full(self):
        return self.fresh(
            clients="claude,codex,opencode",
            docker="y",
            opencode=PROVIDER_ANSWERS,
            logins=[(TOKEN_Q, TOKEN), (PEM_Q, ""), (OPENCODE_LOGIN_Q, "lan"), (r"API key:", KEY)],
        )

    def test_ledger_of_a_fresh_install(self):
        shutil.rmtree(self.ensemble)
        self.assert_ok(self.fresh("--no-docker"))
        h = self.home
        self.assertEqual(
            self.ledger(),
            [
                f"dir\t{h}/.ensemble",
                f"agent-tree\t{h}/.ensemble",
                f"dir\t{h}/.homebrew",
                f"file\t{h}/.homebrew/trust.json",
                f"dir\t{h}/.cache/gh",
                f"dir\t{h}/.local/state/gh",
                f"dir\t{h}/.cache/uv",
                f"homebrew\t{self.root}/opt/homebrew",
                "clt",
                f"profile-file\t{h}/.zprofile",
                f'profile\t{h}/.zprofile\teval "$({self.root}/opt/homebrew/bin/brew shellenv)"',
                "formula\tgh",
                "formula\ttmux",
                "formula\tgit",
                f"dir\t{h}/.config",
                f"dir\t{h}/.config/iterm2",
                *(f"dir\t{h}/Library/{path}" for path in ITERM_DIRS),
                *(f"file\t{h}/Library/{path}" for path in ITERM_FILES),
                "cask\titerm2",
                "config\tcreated",
                f"dir\t{h}/.config/gh",
                f"dir\t{h}/.claude",
                f"file\t{h}/.claude.json",
                f"dir\t{h}/.codex",
                "cask\tclaude-code",
                "cask\tcodex",
                "login\tgh",
                "login\tclaude",
                "login\tcodex",
                "claude-marketplace\twindroc",
                "claude-plugin\tensemble@windroc",
                "claude-plugin\tupdraft@windroc",
                "codex-marketplace\twindroc",
                "codex-plugin\tensemble@windroc",
                "codex-plugin\tupdraft@windroc",
                f"dir\t{h}/.ensemble/bin",
                f'profile\t{h}/.zprofile\texport PATH="$HOME/.ensemble/bin:$PATH"',
            ],
        )

    def test_ledger_before_python_works(self):
        """Before the Command Line Tools, macOS's python3 is a shim that fails: tool state is still recorded."""
        (self.bin / "python3").unlink()
        clt = Path(self.root, "Library/Developer/CommandLineTools")
        executable(
            self.bin / "python3",
            f'#!/bin/sh\n[ -d "{clt}" ] || exit 1\nexec {sys.executable} "$@"\n',
        )
        self.assert_ok(self.fresh("--no-docker"))
        h = self.home
        for record in (
            f"dir\t{h}/.homebrew",
            f"file\t{h}/.homebrew/trust.json",
            f"dir\t{h}/.cache/uv",
        ):
            self.assertIn(record, self.ledger())

    def test_relative_tool_path_not_recorded(self):
        """A relative path from the environment cannot be confirmed missing: one line, no record."""
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.home)
        self.env["UV_CACHE_DIR"] = "uv-cache"
        result = self.fresh("--no-docker")
        self.assert_ok(result)
        self.assertEqual(
            result.stderr.count(f"{NOT_TOLD}uv-cache: cannot tell whether it is there"), 1
        )
        self.assertFalse([record for record in self.ledger() if "uv-cache" in record])

    def test_ledger_with_docker_and_opencode(self):
        self.assert_ok(self.full())
        h, ledger = self.home, self.ledger()
        for record in (
            f"dir\t{h}/.ensemble/bin",
            f"dir\t{h}/.ensemble/docker",
            f"dir\t{h}/.ensemble/opencode",
            f"dir\t{h}/.local/share/opencode",
            f"dir\t{h}/.docker",
            f"dir\t{h}/Library/Group Containers/group.com.docker",
            "formula\tnode",
            "cask\tdocker-desktop",
            "broker",
            "keychain\tensemble-claude\tdefault",
            "keychain\tensemble-codex\tauth",
            "keychain\tensemble-opencode\tlan",
        ):
            self.assertIn(record, ledger)
        self.assertNotIn(f"dir\t{h}/.ensemble", ledger)
        self.assertNotIn("keychain\tensemble-github-users\tensemble-bot", ledger)

    def test_rerun_skips_everything(self):
        self.assert_ok(self.full())
        before = self.ledger()
        files = {
            p: p.read_bytes()
            for p in Path(self.home).rglob("*")
            if p.is_file() and "fakes" not in p.parts
        }
        result = self.again()
        self.assert_ok(result)
        self.assertNotIn("[y/N]", result.tty)
        self.assertNotRegex(result.tty, r"\? |name:|URL|model:|App ID|Paste")
        changing = (
            "brew install",
            "plugin install",
            "plugin add",
            "marketplace add",
            "broker-install",
        )
        changing += ("security add", "launchctl", "npm install", "setup-token", "-codex ")
        changing += ("auth login", "login codex", "login claude", "login opencode", "codex login")
        changed = [c for c in self.calls() if any(w in c for w in changing)]
        self.assertEqual([c for c in changed if not c.endswith(" status")], [])
        self.assertEqual(self.ledger(), before)
        # the egress step always runs the pinned start.sh, which changes nothing here
        exempt = {"sudo", "configuration", "plugin directory", "egress proxy"}
        steps = re.findall(r"==> (.+)\n(?!    skip:)", result.stderr)
        self.assertEqual([s for s in steps if s not in exempt], [])
        for path, data in files.items():
            if path.name not in ("stdout", "stderr", "calls.log", "env.log"):
                self.assertEqual(path.read_bytes(), data, path)

    def test_failure_then_rerun_completes(self):
        (self.fakes / "fail").write_text("brew install --cask codex")
        result = self.run_install(
            "--no-docker",
            answers=[(SUDO_Q, PASSWORD), (WRITE_Q, "y"), (MENU_Q, ""), (WRITE_Q, "y")],
        )
        self.assertEqual(result.code, 1)
        self.assertTrue(
            result.stderr.endswith(
                "install.sh: apps failed; rerun the same command to continue\n" + RELOAD + "\n"
            ),
            result.stderr,
        )
        self.assertIn("cask\tcodex", self.ledger())
        self.assertNotIn("codex plugin", "\n".join(self.calls()))
        result = self.again(answers=[(WRITE_Q, "y")])
        self.assert_ok(result)
        calls = self.calls()
        self.assertIn("brew install --cask codex", calls)
        self.assertNotIn("brew install --cask claude-code", calls)
        self.assertIn("codex plugin add ensemble@windroc", calls)
        self.assertEqual(self.ledger().count("cask\tcodex"), 1)

    def test_not_recorded_outside_home(self):
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        self.env["CODEX_HOME"] = str(outside / "codex")
        result = self.fresh("--no-docker")
        self.assert_ok(result)
        self.assertIn(f"not recorded for cleanup: dir {outside}/codex", result.stderr)
        self.assertFalse([r for r in self.ledger() if str(outside) in r])

    def test_created_config_is_not_backed_up(self):
        self.assert_ok(self.fresh("--no-docker"))
        self.assert_ok(
            self.again("--docker", answers=[(WRITE_Q, "y"), (TOKEN_Q, TOKEN), (PEM_Q, "")])
        )
        self.assertIn('default = "docker"', self.config().read_text())
        self.assertFalse(Path(self.ensemble, "install/config.local.toml.orig").exists())
        self.assertNotIn("config\tbackup", self.ledger())

    def test_existing_config_is_backed_up_once(self):
        mine = '# mine\n[session.hosts]\ndefault = "tmux"\n'
        self.config().write_text(mine)
        self.assert_ok(self.fresh("--docker", "--clients", "claude"))
        self.assert_ok(self.again("--no-docker", answers=[(WRITE_Q, "y")]))
        self.assertEqual(Path(self.ensemble, "install/config.local.toml.orig").read_text(), mine)
        self.assertEqual(self.ledger().count("config\tbackup"), 1)

    def cleanup(self, *args, answers=(), stdin=None, ctty=None):
        """Without --password-stdin, --yes runs answer sudo's prompt."""
        if stdin is None and "--yes" in args:
            answers = [(SUDO_Q, PASSWORD), *answers]
        return self.run_install(*args, answers=answers, stdin=stdin, script=CLEANUP, ctty=ctty)

    def snapshot(self):
        skip = {"fakes", "bin"}
        logs = {"stdout", "stderr", "calls.log", "env.log"}
        return {
            p: p.read_bytes()
            for p in Path(self.home).rglob("*")
            if p.is_file() and not skip & set(p.relative_to(self.home).parts) and p.name not in logs
        }

    def test_cleanup_usage_and_nothing_recorded(self):
        result = self.run_install("--help", tty=False, script=CLEANUP)
        self.assertEqual(result.code, 0)
        self.assertIn("usage: install-cleanup.sh [--yes] [--password-stdin]", result.stdout)
        result = self.run_install("--bogus", tty=False, script=CLEANUP)
        self.assertEqual(result.code, 2)
        tty = Tty()
        try:
            with open(tty.path) as stdin:
                proc = subprocess.run(
                    ["bash", str(CLEANUP), "--password-stdin"],
                    env=self.env,
                    stdin=stdin,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
        finally:
            tty.close()
        self.assertEqual(proc.returncode, 2)
        result = self.cleanup()
        self.assert_ok(result)
        self.assertIn("nothing recorded by install.sh; nothing to remove", result.stderr)
        self.assertNotIn("sudo", "\n".join(self.calls()))

    def test_cleanup_asks_and_declines(self):
        self.assert_ok(self.fresh("--no-docker"))
        before = self.snapshot()
        result = self.cleanup(answers=[(REMOVE_Q, "n")])
        self.assertEqual(result.code, 1)
        self.assertIn("install-cleanup.sh: nothing removed", result.stderr)
        self.assertIn("  formula gh", result.tty)
        self.assertIn("  login claude", result.tty)
        self.assertFalse([c for c in self.calls() if c.startswith("sudo")])
        self.assertEqual(self.snapshot(), before)

    def test_cleanup_rejects_bad_records(self):
        self.assert_ok(self.fresh("--no-docker"))
        ledger = Path(self.ensemble, "install/added")
        good = ledger.read_text()
        bad = (
            "dir\t/Applications",
            f"dir\t{self.home}",
            f"dir\t{self.home}/x/../..",
            "board-dir\t/Applications",
            f"board-dir\t{self.home}",
            f"board-dir\t{self.home}/board/../outside",
            f"board-dir\t{self.home}/./board",
            "formula\t--force",
            "homebrew\t/usr/local",
            "keychain\t-x\ty",
            "rm\t-rf",
        )
        for record in bad:
            ledger.write_text(good + record + "\n")
            result = self.run_install("--yes", script=CLEANUP)
            self.assertEqual(result.code, 1, record)
            self.assertIn("not a record install.sh writes; nothing removed", result.stderr)
            self.assertFalse([c for c in self.calls() if c.startswith("sudo")], record)
            self.assertEqual(ledger.read_text(), good + record + "\n")

    def test_cleanup_logs_claude_out_on_a_kqueue_refused_tty(self):
        """claude auth logout dies on /dev/tty, as macOS's kqueue refuses it; the terminal's own device works."""
        self.assert_ok(self.fresh("--no-docker", clients="claude"))
        self.assertTrue((self.fakes / "login/claude").exists())
        result = self.cleanup("--yes", ctty=self.refused_tty())
        self.assert_ok(result)
        self.assertIn("claude auth logout", self.calls())
        self.assertNotIn("claude is still logged in", result.stderr)
        self.assertFalse((self.fakes / "login/claude").exists())
        self.assertIn("Removed what install.sh added.", result.stderr)

    def test_cleanup_round_trip(self):
        before = self.snapshot()
        first = self.full()
        self.assert_ok(first)
        calls, ledger, files = self.run_calls(), self.ledger(), self.snapshot()
        shared = {p: b for p, b in files.items() if ".egress" in p.relative_to(self.home).parts}
        result = self.cleanup("--yes", "--password-stdin", stdin=PASSWORD + "\n")
        self.assert_ok(result)
        self.assertNotIn("docker-uninstall", self.calls())
        self.assertIn("brew uninstall --cask docker-desktop", self.calls())
        self.assertEqual(self.snapshot(), before | shared)
        self.assertEqual(list((self.fakes / "keychain").iterdir()), [])
        self.assertEqual(list((self.fakes / "login").iterdir()), [])
        self.assertFalse(self.broker_path().parent.exists())
        self.assertFalse(Path(self.root, "opt/homebrew/bin/brew").exists())
        self.assert_secret_free(PASSWORD, KEY, TOKEN, result=result)
        second = self.full()
        self.assert_ok(second)
        fetch = "gh api repos/windroc/egress-proxy/tarball/"
        self.assertEqual(self.run_calls(), [c for c in calls if not c.startswith(fetch)])
        self.assertEqual(self.ledger(), ledger)
        self.assertEqual(second.tty, first.tty)
        self.assertEqual(self.snapshot(), files)

    def test_cleanup_round_trip_leaves_no_docker_files(self):
        """Docker Desktop's and iTerm2's own files go; a shell rc there before keeps its own lines."""
        bashrc = Path(self.home, ".bashrc")
        bashrc.write_text("export EDITOR=vim\n")
        self.assert_ok(self.fresh("--docker", logins=[(TOKEN_Q, TOKEN), (PEM_Q, "")]))
        library = Path(self.home, "Library")
        written = [
            library / "Application Support/Docker Desktop/state.json",
            library / "Preferences/com.docker.docker.plist",
            Path(self.home, ".zshrc"),
        ]
        self.assertTrue(all(path.exists() for path in written))
        self.assertIn("by Docker Desktop", bashrc.read_text())
        for path in ("Preferences/com.googlecode.iterm2.plist", "Application Support/iTerm2/x"):
            (library / path).parent.mkdir(parents=True, exist_ok=True)
            (library / path).write_text("iTerm2")
        self.assertIn(f"profile-file\t{self.home}/.zshrc", self.ledger())
        self.assertIn(f"docker-block\t{bashrc}", self.ledger())
        result = self.cleanup("--yes")
        self.assert_ok(result)
        for path in [*written, library / "Preferences/com.googlecode.iterm2.plist"]:
            self.assertFalse(path.exists(), path)
        self.assertFalse((library / "Application Support/Docker Desktop").exists())
        self.assertFalse((library / "Application Support/iTerm2").exists())
        self.assertEqual(bashrc.read_text(), "export EDITOR=vim\n")
        self.assertIn("defaults delete com.docker.docker", self.calls())
        self.assertIn("defaults delete com.googlecode.iterm2", self.calls())

    def test_cleanup_stops_docker_processes(self):
        """What still runs from Docker.app after its cask is gone ends; nothing else is signalled."""
        self.assert_ok(self.fresh("--docker", logins=[(TOKEN_Q, TOKEN), (PEM_Q, "")]))
        agent = (
            f"{self.root}/Applications/Docker.app/Contents/Resources/cli-plugins/docker-agent serve"
        )
        others = [f"{self.root}/Applications/DockerXapp/Contents/MacOS/x", f"/bin/sh {agent}"]
        processes = self.fakes / "processes"
        self.assertEqual(processes.read_text().splitlines(), [agent])
        processes.write_text("".join(f"{line}\n" for line in [agent, *others]))
        result = self.cleanup("--yes")
        self.assert_ok(result)
        self.assertEqual(processes.read_text().splitlines(), others)
        calls = self.calls()
        kill = next(c for c in calls if c.startswith("pkill -TERM -f "))
        self.assertLess(calls.index("brew uninstall --cask docker-desktop"), calls.index(kill))
        self.assertNotIn("pkill -KILL", "\n".join(calls))

    def test_cleanup_fails_on_an_unkillable_docker_process(self):
        self.assert_ok(self.fresh("--docker", logins=[(TOKEN_Q, TOKEN), (PEM_Q, "")]))
        (self.fakes / "unkillable").write_text((self.fakes / "processes").read_text())
        result = self.cleanup("--yes")
        self.assertEqual(result.code, 1, result.stderr)
        self.assertIn(
            "install-cleanup.sh: Docker Desktop processes still running: 1\n", result.stderr
        )
        self.assertIn("pkill -KILL", "\n".join(self.calls()))
        self.assertIn("cask\tdocker-desktop", self.ledger())

    def test_cleanup_needs_full_disk_access_for_a_container(self):
        """macOS lets only Full Disk Access remove an app container: without it nothing is removed."""
        self.assert_ok(self.fresh("--docker", logins=[(TOKEN_Q, TOKEN), (PEM_Q, "")]))
        self.tcc.unlink()
        container = Path(self.home, "Library/Containers/com.docker.docker")
        group = Path(self.home, "Library/Group Containers/group.com.docker")
        (container / "locked").mkdir(parents=True)
        (container / "locked/data").write_text("")
        ledger = Path(self.ensemble, "install/added")
        recorded = ledger.read_bytes()
        result = self.cleanup()
        self.assertEqual(result.code, 1, result.stderr)
        self.assertEqual(
            result.stderr,
            f"install-cleanup.sh: macOS lets only Full Disk Access remove {container} {group}: give the"
            " terminal Full Disk Access (System Settings › Privacy & Security), reopen it, then"
            " rerun; nothing removed (take the access back once cleanup has finished)\n",
        )
        self.assertNotRegex(result.tty, REMOVE_Q)
        self.assertEqual(ledger.read_bytes(), recorded)
        self.assertTrue((container / "locked/data").exists())
        self.assertEqual([c for c in self.calls() if not c.startswith(("uname", "ps "))], [])
        self.tcc.write_text("granted")
        locked = container / "locked"
        locked.chmod(0o500)
        self.addCleanup(lambda: locked.exists() and locked.chmod(0o700))
        result = self.cleanup(answers=[(REMOVE_Q, "y"), (SUDO_Q, PASSWORD)])
        self.assertEqual(result.code, 1, result.stderr)
        self.assertIn(
            f"install-cleanup.sh: cannot remove {container}; its record stays", result.stderr
        )
        self.assertIn(f"dir\t{container}", ledger.read_text().splitlines())
        self.assertIn("docker-uninstall", self.calls())
        self.assertLess(
            self.calls().index("docker-uninstall"),
            self.calls().index("brew uninstall --cask docker-desktop"),
        )
        locked.chmod(0o700)
        result = self.cleanup(answers=[(REMOVE_Q, "y"), (SUDO_Q, PASSWORD)])
        self.assert_ok(result)
        self.assertTrue(result.stderr.endswith("Removed what install.sh added.\n"), result.stderr)
        self.assertFalse(container.exists())
        self.assertFalse(Path(self.ensemble, "install").exists())
        self.assertFalse(Path(self.home, ".zprofile").exists())

    def test_cleanup_needs_full_disk_access_for_a_group_container(self):
        """macOS 27 denies a terminal Docker's Group Container too: without the access nothing is removed."""
        self.assert_ok(self.fresh("--docker", logins=[(TOKEN_Q, TOKEN), (PEM_Q, "")]))
        group = Path(self.home, "Library/Group Containers/group.com.docker")
        self.assertTrue((group / "settings-store.json").exists())
        self.tcc.unlink()
        ledger = Path(self.ensemble, "install/added")
        recorded = ledger.read_bytes()
        result = self.cleanup()
        self.assertEqual(result.code, 1, result.stderr)
        self.assertIn(
            f"install-cleanup.sh: macOS lets only Full Disk Access remove {group}:", result.stderr
        )
        self.assertNotRegex(result.tty, REMOVE_Q)
        self.assertEqual(ledger.read_bytes(), recorded)
        self.assertTrue((group / "settings-store.json").exists())
        group.parent.chmod(0o600)
        self.addCleanup(lambda: group.parent.exists() and group.parent.chmod(0o700))
        result = self.cleanup()
        self.assertEqual(result.code, 1, result.stderr)
        self.assertIn(f"remove {group}:", result.stderr)
        self.assertEqual(ledger.read_bytes(), recorded)
        group.parent.chmod(0o700)
        self.tcc.write_text("granted")
        self.assert_ok(self.cleanup("--yes"))
        self.assertFalse(group.exists())

    def preinstalled_homebrew(self):
        """Homebrew with gh and the Command Line Tools, there before install.sh; PATH lacks its bin."""
        prefix = Path(self.root, "opt/homebrew/bin")
        prefix.mkdir(parents=True)
        for app in ("brew", "gh"):
            shutil.copy(self.fakes / "apps" / app, prefix / app)
        (self.fakes / "brew").mkdir()
        (self.fakes / "brew/gh").touch()
        Path(self.root, "Library/Developer/CommandLineTools").mkdir(parents=True)

    def test_cleanup_removes_only_what_was_added(self):
        self.preinstalled_homebrew()
        profile = Path(self.home, ".zprofile")
        profile.write_text("export EDITOR=vim\n")
        mine = '# mine\n[session.hosts]\ndefault = "tmux"\n'
        self.config().write_text(mine)
        (self.fakes / "keychain").mkdir()
        (self.fakes / "keychain/ensemble-claude").write_text("theirs")
        Path(self.home, ".docker").mkdir()
        Path(self.home, ".docker/config.json").write_text("{}")
        Path(self.home, ".zshrc").write_text(
            "# The following lines have been added by Docker Desktop to enable Docker CLI"
            " completions.\n# End of Docker Desktop section.\n"
        )
        before = self.snapshot()
        self.assert_ok(self.fresh("--docker", logins=[(PEM_Q, "")]))
        shared = {
            p: b for p, b in self.snapshot().items() if ".egress" in p.relative_to(self.home).parts
        }
        self.assertFalse(
            [r for r in self.ledger() if r.startswith(("homebrew", "clt", "formula\tgh"))]
        )
        result = self.cleanup("--yes")
        self.assert_ok(result)
        self.assertEqual(self.snapshot(), before | shared)
        self.assertTrue((self.fakes / "brew/gh").exists())
        self.assertTrue((self.fakes / "keychain/ensemble-claude").exists())
        self.assertNotIn("brew uninstall gh", self.calls())

    def test_cleanup_without_brew_on_path(self):
        self.preinstalled_homebrew()
        self.assert_ok(self.fresh("--no-docker"))
        self.assertNotIn(str(self.root), self.env["PATH"])
        result = self.cleanup("--yes")
        self.assert_ok(result)
        calls = self.calls()
        for done in (
            "codex plugin remove ensemble@windroc",
            "claude plugin uninstall ensemble@windroc",
            "claude plugin marketplace remove windroc",
            "gh auth logout --hostname github.com",
            "claude auth logout",
            "codex logout",
            "brew uninstall --cask codex",
            "brew uninstall --cask claude-code",
            "brew uninstall tmux",
        ):
            self.assertIn(done, calls)
        self.assertEqual(list((self.fakes / "login").iterdir()), [])

    def test_cleanup_keeps_a_failed_removal(self):
        self.assert_ok(self.fresh("--no-docker"))
        (self.fakes / "fail").write_text("claude plugin marketplace remove windroc")
        result = self.cleanup("--yes")
        self.assertEqual(result.code, 1)
        self.assertIn("removing claude-marketplace windroc failed", result.stderr)
        self.assertIn("claude-marketplace\twindroc", self.ledger())
        self.assertNotIn("codex-plugin\tensemble@windroc", self.ledger())
        self.assert_ok(self.cleanup("--yes"))
        self.assertFalse(Path(self.ensemble, "install").exists())

    def board_install(self):
        """A fresh install, then the Linear board set up and the installer run again for its apps and jobs."""
        self.assert_ok(self.fresh("--no-docker"))
        first = self.run_calls()
        linear = '\n[orchestrator]\ntracker = "linear"\n\n[orchestrator.linear]\n'
        self.config().write_text(self.config().read_text() + linear)
        self.linear()
        board = [*self.secrets(*SLOTS), (PROJECT_Q, ""), *DOCS, (WRITE_Q, "y"), (WRITE_Q, "y")]
        self.assert_ok(self.again("--board", "linear", "--launchd", answers=board))
        return first + self.run_calls()

    def test_cleanup_round_trip_with_board(self):
        before = self.snapshot()
        calls = self.board_install()
        files = self.snapshot()
        result = self.cleanup("--yes", "--password-stdin", stdin=PASSWORD + "\n")
        self.assert_ok(result)
        uid_calls = [c for c in self.calls() if c.startswith("launchctl bootout")]
        self.assertEqual(len(uid_calls), 2)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(list((self.fakes / "keychain").iterdir()), [])
        self.assertEqual(self.board_install(), calls)
        self.assertEqual(self.snapshot(), files)

    def test_cleanup_round_trip_opencode_tmux(self):
        def install():
            result = self.fresh(
                "--no-docker",
                clients="claude,opencode",
                opencode=PROVIDER_ANSWERS,
                logins=[(OPENCODE_LOGIN_Q, "lan"), (r"API key:", KEY)],
            )
            self.assert_ok(result)
            return self.run_calls()

        before = self.snapshot()
        calls = install()
        files = self.snapshot()
        self.assert_ok(self.cleanup("--yes", "--password-stdin", stdin=PASSWORD + "\n"))
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.auth_json().exists())
        self.assertEqual(install(), calls)
        self.assertEqual(self.snapshot(), files)

    def test_cleanup_keeps_user_profile_lines(self):
        self.assert_ok(self.fresh("--no-docker"))
        profile = Path(self.home, ".zprofile")
        profile.write_text(profile.read_text() + "export EDITOR=vim\n")
        self.assert_ok(self.cleanup("--yes"))
        self.assertEqual(profile.read_text(), "export EDITOR=vim\n")

    def test_cleanup_failure_then_rerun(self):
        self.assert_ok(self.fresh("--no-docker"))
        (self.fakes / "fail").write_text("codex plugin remove ensemble@windroc")
        result = self.cleanup("--yes")
        self.assertEqual(result.code, 1)
        self.assertIn("install-cleanup.sh: plugins failed; rerun the same command", result.stderr)
        self.assertIn("codex-plugin\tensemble@windroc", self.ledger())
        self.assertIn("login\tgh", self.ledger())
        result = self.cleanup("--yes")
        self.assert_ok(result)
        self.assertFalse(Path(self.ensemble, "install").exists())

    def test_cleanup_missing_tool_keeps_record(self):
        self.assert_ok(self.fresh("--no-docker"))
        Path(self.root, "opt/homebrew/bin/codex").unlink()
        result = self.cleanup("--yes")
        self.assertEqual(result.code, 1)
        self.assertIn("codex not found on PATH; its records stay for a rerun", result.stderr)
        self.assertIn("codex-plugin\tensemble@windroc", self.ledger())
        self.assertIn("cask\tcodex", self.ledger())

    def test_cleanup_handles_every_kind(self):
        install, cleanup = SCRIPT.read_text(), CLEANUP.read_text()
        written = set(re.findall(r"ledger_add ([a-z-]+)", install)) | {"dir", "keychain"}
        listed = re.search(r"for kind in (.*?); do", cleanup, re.S).group(1)
        handled = set(listed.replace("\\\n", " ").split())
        self.assertEqual(written - handled, set())

    def test_cleanup_helpers_match(self):
        install, cleanup = SCRIPT.read_text(), CLEANUP.read_text()
        pattern_line = re.compile(r"^name_pattern=.*$", re.M)
        self.assertEqual(
            pattern_line.search(cleanup).group(0), pattern_line.search(install).group(0)
        )
        for name in SHARED:
            pattern = rf"^{name}\(\) \{{(?:[^\n]*\}}$|\n.*?^\}})"
            ours = re.search(pattern, install, re.S | re.M)
            theirs = re.search(pattern, cleanup, re.S | re.M)
            self.assertIsNotNone(theirs, name)
            self.assertEqual(theirs.group(0), ours.group(0), name)

    def test_profile_without_a_final_newline(self):
        profile = Path(self.home, ".zprofile")
        profile.write_text("export EDITOR=vim")
        self.assert_ok(self.fresh("--no-docker"))
        lines = profile.read_text().splitlines()
        self.assertEqual(lines[0], "export EDITOR=vim")
        self.assertTrue(lines[1].startswith("eval "), lines)
        self.assert_ok(self.again())
        self.assertEqual(len(profile.read_text().splitlines()), 3)
