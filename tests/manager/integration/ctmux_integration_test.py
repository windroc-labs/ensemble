"""ctmux (skills/manage/scripts/ctmux): its tmux calls through a fake tmux, its attach per
terminal, its new session on a private tmux server with the shell under its plugin's ensemble-crew
hold, and the launchers --install puts in ~/.ensemble/bin, run from a plain PATH before and after a
plugin upgrade.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import uuid

from ensemble.manager.engine.roster import Roster
from ensemble.manager.engine.types import Held
from tests.manager.case import REPO, Case
from tests.session import live_tmux

CTMUX = os.path.join(REPO, "skills", "manage", "scripts", "ctmux")
LAUNCHER = os.path.join(REPO, "skills", "manage", "scripts", "launcher")
COMMANDS = (
    "ctmux",
    "ensemble-crew",
    "ensemble-session",
    "ensemble-delegate",
    "ensemble-orchestrator",
)
USAGE = "usage: ctmux <session-name> | ctmux --install"
FAKE = """#!{python} -I
import json, os, sys
with open(os.environ["CTMUX_LOG"], "a") as f:
    f.write(json.dumps([os.getcwd(), os.getpid(), *sys.argv[1:]]) + "\\n")
rc = {{"has": "HAS_RC", "start-server": "START_RC"}}
code = rc.get(sys.argv[1], "ATTACH_RC" if sys.argv[1] in ("-CC", "attach", "switch-client") else "")
sys.exit(int(os.environ.get(code, "0")))
"""
PROXY = """#!{python} -I
import json, os, sys
if sys.argv[1:2] == ["-CC"]:
    with open({log!r}, "a") as f:
        f.write(json.dumps(sys.argv[1:]) + "\\n")
    sys.exit(0)
os.execv({program!r}, [{program!r}, "-f", os.devnull, *sys.argv[1:]])
"""
SHELL = """#!/bin/sh
printf '%s\\n' "$(pwd -P)" "$*" "$ENSEMBLE_MANAGER" "$ENSEMBLE_HOLDER_ID" \\
  "$ENSEMBLE_HOLDER_GENERATION" "${PATH%%:*}" > "$0.tmp" && mv "$0.tmp" "$0.log"
exec cat
"""
START = [
    "start-server",
    ";",
    "set",
    "-g",
    "mouse",
    "on",
    ";",
    "set",
    "-g",
    "history-limit",
    "50000",
    ";",
    "set",
    "-s",
    "extended-keys",
    "on",
    ";",
    "set",
    "-s",
    "terminal-features[99]",
    "xterm*:extkeys",
    ";",
    "new",
    "-d",
    "-s",
]
OPTIONS = (
    ("-g", "mouse"),
    ("-g", "history-limit"),
    ("-s", "extended-keys"),
    ("-s", "terminal-features"),
)


def created(name: str, cwd: str, shell: str, root: str = REPO) -> list[str]:
    crew = os.path.join(root, "bin", "ensemble-crew")
    hold = [crew, "hold", f"--manager={name}", f"--session={name}", "--", shell, "-l"]
    return [*START, name, "-c", cwd, *hold]


def plugin(root: str) -> str:
    """A plugin install at root: this checkout's source and skills linked in, its bin/ copied."""
    os.makedirs(root)
    for directory in ("src", "skills"):
        os.symlink(os.path.join(REPO, directory), os.path.join(root, directory))
    shutil.copytree(os.path.join(REPO, "bin"), os.path.join(root, "bin"))
    return root


def record(home: str, *roots: str | dict) -> None:
    """Claude Code's install record in home, listing roots (or raw entries) for ensemble@windroc."""
    path = os.path.join(home, ".claude", "plugins", "installed_plugins.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    entries = [r if isinstance(r, dict) else {"scope": "user", "installPath": r} for r in roots]
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"version": 2, "plugins": {"ensemble@windroc": entries}}, f)


def executable(path: str, text: str) -> str:
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, 0o755)
    return path


class FakeTmux(Case):
    def setUp(self):
        self.bin = os.path.join(self.home, "bin")
        self.cwd = os.path.realpath(os.path.join(self.home, "a b'c$d"))
        self.log = os.path.join(self.home, "tmux.log")
        os.mkdir(self.bin)
        os.mkdir(self.cwd)
        executable(os.path.join(self.bin, "tmux"), FAKE.format(python=sys.executable))
        os.symlink(sys.executable, os.path.join(self.bin, "python3"))
        self.installed = os.path.join(self.ensemble, "bin")
        self.dst = os.path.join(self.installed, "ctmux")
        self.codex = os.path.join(self.home, ".codex", "plugins", "cache", "windroc", "ensemble")
        self.opencode = os.path.join(self.home, ".cache", "opencode", "packages")
        self.env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": self.home,
            "SHELL": "/bin/zsh",
            "TERM_PROGRAM": "iTerm.app",
            "TMUX_TMPDIR": os.path.join(self.home, "tmux"),
            "CTMUX_LOG": self.log,
        }

    def ctmux(self, *args, script=CTMUX, has_rc=1, **codes):
        if os.path.exists(self.log):
            os.unlink(self.log)
        env = {**self.env, "HAS_RC": str(has_rc), **{k: str(v) for k, v in codes.items()}}
        process = subprocess.Popen(
            (script, *args),
            cwd=self.cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        out, err = process.communicate(timeout=30)
        calls = []
        if os.path.exists(self.log):
            with open(self.log, encoding="utf-8") as f:
                calls = [json.loads(line) for line in f]
        return process, out, err, calls

    def test_a_missing_or_empty_name_is_a_usage_error_without_tmux(self):
        for args in ((), ("",)):
            with self.subTest(args=args):
                process, _, err, calls = self.ctmux(*args)
                self.assertNotEqual(process.returncode, 0)
                self.assertIn(USAGE, err)
                self.assertEqual(calls, [])

    def test_an_existing_session_is_only_attached(self):
        _, _, _, calls = self.ctmux("s1", has_rc=0)
        self.assertEqual(
            [call[2:] for call in calls], [["has", "-t", "=s1"], ["-CC", "attach", "-t", "=s1"]]
        )

    def test_the_attach_fits_the_terminal(self):
        del self.env["TERM_PROGRAM"]
        for env, attach in (
            ({"TMUX": "/tmp/t,1,0", "TERM_PROGRAM": "iTerm.app"}, ["switch-client"]),
            ({"TERM_PROGRAM": "iTerm.app"}, ["-CC", "attach"]),
            ({"LC_TERMINAL": "iTerm2"}, ["-CC", "attach"]),
            ({"TERM_PROGRAM": "Apple_Terminal"}, ["attach"]),
            ({}, ["attach"]),
        ):
            with self.subTest(env=env):
                base = dict(self.env)
                self.env.update(env)
                try:
                    process, _, _, calls = self.ctmux("s1", has_rc=0, ATTACH_RC=5)
                finally:
                    self.env = base
                self.assertEqual(process.returncode, 5)
                self.assertEqual(calls[-1][1], process.pid)
                self.assertEqual(
                    [call[2:] for call in calls], [["has", "-t", "=s1"], [*attach, "-t", "=s1"]]
                )

    def test_an_absent_session_starts_from_home_in_the_callers_directory_with_its_shell_held(self):
        process, _, err, calls = self.ctmux("s1")
        self.assertEqual((process.returncode, err), (0, ""))
        self.assertEqual(
            [call[2:] for call in calls],
            [
                ["has", "-t", "=s1"],
                created("s1", self.cwd, "/bin/zsh"),
                ["-CC", "attach", "-t", "=s1"],
            ],
        )
        self.assertEqual(calls[1][0], self.home)

    def test_with_an_empty_shell_the_held_command_is_bin_sh(self):
        self.env["SHELL"] = ""
        _, _, _, calls = self.ctmux("s1")
        self.assertEqual(calls[1][2:], created("s1", self.cwd, "/bin/sh"))

    def test_only_the_first_argument_names_the_session_as_one_word(self):
        for args, name in ((("m a'n$x",), "m a'n$x"), (("s1", "extra", "--install"), "s1")):
            with self.subTest(args=args):
                _, _, _, calls = self.ctmux(*args)
                self.assertEqual(
                    [call[2:] for call in calls],
                    [
                        ["has", "-t", f"={name}"],
                        created(name, self.cwd, "/bin/zsh"),
                        ["-CC", "attach", "-t", f"={name}"],
                    ],
                )

    def test_a_failed_create_still_attaches_and_the_attach_status_wins(self):
        process, _, _, calls = self.ctmux("s1", START_RC=1, ATTACH_RC=7)
        self.assertEqual(process.returncode, 7)
        self.assertEqual([call[2] for call in calls], ["has", "start-server", "-CC"])

    def test_the_attach_replaces_ctmux_and_its_status_is_ctmux_s(self):
        process, _, _, calls = self.ctmux("s1", has_rc=0, ATTACH_RC=5)
        self.assertEqual(process.returncode, 5)
        self.assertEqual(calls[-1][1], process.pid)

    def test_the_script_names_no_client(self):
        with open(CTMUX, "rb") as f:
            text = f.read().lower()
        self.assertNotIn(b"claude", text)
        self.assertNotIn(b"codex", text)

    def install(self, script=CTMUX):
        process, out, err, calls = self.ctmux("--install", script=script)
        paths = [os.path.join(self.installed, name) for name in COMMANDS]
        self.assertEqual(
            (process.returncode, out, err, calls), (0, "".join(f"{p}\n" for p in paths), "", [])
        )

    def test_install_writes_each_launcher_exact_mode_755_and_a_rerun_refreshes_them(self):
        with open(LAUNCHER, "rb") as f:
            source = f.read()
        record(self.home, REPO)
        self.install()
        with open(os.path.join(self.installed, "ensemble-crew"), "w", encoding="utf-8") as f:
            f.write("stale")
        self.install(script=self.dst)
        self.assertEqual(sorted(os.listdir(self.installed)), sorted(COMMANDS))
        for name in COMMANDS:
            path = os.path.join(self.installed, name)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o755, name)
            with open(path, "rb") as f:
                self.assertEqual(f.read(), source, name)

    def test_the_installed_ctmux_runs_the_recorded_plugins_and_follows_an_upgrade(self):
        self.install()
        for version in ("0.5.0", "0.6.0"):
            with self.subTest(version=version):
                root = plugin(os.path.join(self.home, "cache", version))
                record(self.home, root)
                process, _, err, calls = self.ctmux("s1", script=self.dst)
                self.assertEqual((process.returncode, err), (0, ""))
                self.assertEqual(calls[1][2:], created("s1", self.cwd, "/bin/zsh", root))

    def test_without_a_claude_record_the_newest_codex_install_runs(self):
        self.install()
        for version in ("0.9.0", "0.10.0"):
            plugin(os.path.join(self.codex, version))
        _, _, err, calls = self.ctmux("s1", script=self.dst)
        self.assertEqual(err, "")
        self.assertEqual(
            calls[1][2:], created("s1", self.cwd, "/bin/zsh", os.path.join(self.codex, "0.10.0"))
        )

    def test_a_claude_record_wins_over_a_codex_install(self):
        self.install()
        plugin(os.path.join(self.codex, "9.0.0"))
        record(self.home, root := plugin(os.path.join(self.home, "cache", "0.5.0")))
        _, _, _, calls = self.ctmux("s1", script=self.dst)
        self.assertEqual(calls[1][2:], created("s1", self.cwd, "/bin/zsh", root))

    def opencode_install(self, version: str, spec: str = "") -> str:
        """OpenCode's install of the git spec pinned to `version` (or `spec`): its package dir."""
        spec = spec or f"ensemble@git+https:/github.com/windroc-labs/ensemble.git#v{version}"
        root = plugin(os.path.join(self.opencode, spec, "node_modules", "ensemble"))
        with open(os.path.join(root, "package.json"), "w", encoding="utf-8") as f:
            json.dump({"name": "ensemble", "version": version}, f)
        return root

    def test_without_claude_or_codex_the_newest_opencode_install_runs(self):
        self.install()
        for version in ("0.9.0", "0.10.0"):
            self.opencode_install(version)
        newest = os.path.join(
            self.opencode, "ensemble@git+https:/github.com/windroc-labs/ensemble.git#v0.10.0"
        )
        _, _, err, calls = self.ctmux("s1", script=self.dst)
        self.assertEqual(err, "")
        self.assertEqual(
            calls[1][2:],
            created("s1", self.cwd, "/bin/zsh", os.path.join(newest, "node_modules", "ensemble")),
        )
        cache = os.path.join(self.home, "xdg cache")
        self.opencode = os.path.join(cache, "opencode", "packages")
        root = self.opencode_install("0.1.0", spec="ensemble")
        _, _, err, calls = self.ctmux("s1", script=self.dst, XDG_CACHE_HOME=cache)
        self.assertEqual(err, "")
        self.assertEqual(calls[1][2:], created("s1", self.cwd, "/bin/zsh", root))

    def test_claude_and_codex_installs_win_over_an_opencode_install(self):
        self.install()
        self.opencode_install("9.0.0")
        plugin(os.path.join(self.codex, "0.5.0"))
        _, _, _, calls = self.ctmux("s1", script=self.dst)
        self.assertEqual(
            calls[1][2:], created("s1", self.cwd, "/bin/zsh", os.path.join(self.codex, "0.5.0"))
        )
        record(self.home, root := plugin(os.path.join(self.home, "cache", "0.4.0")))
        _, _, _, calls = self.ctmux("s1", script=self.dst)
        self.assertEqual(calls[1][2:], created("s1", self.cwd, "/bin/zsh", root))

    def test_a_launcher_passes_its_arguments_stdin_and_status(self):
        self.install()
        root = os.path.join(self.home, "cache", "0.5.0")
        os.makedirs(os.path.join(root, "bin"))
        executable(
            os.path.join(root, "bin", "ensemble-crew"),
            '#!/bin/sh\nprintf "%s|" "$0" "$@"; cat; exit 3\n',
        )
        record(self.home, root)
        done = subprocess.run(
            (os.path.join(self.installed, "ensemble-crew"), "status", "--manager", "a b"),
            input="in",
            env=self.env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        crew = os.path.join(root, "bin", "ensemble-crew")
        self.assertEqual((done.returncode, done.stdout), (3, f"{crew}|status|--manager|a b|in"))

    def test_a_claude_record_entry_without_an_install_path_is_skipped(self):
        self.install()
        record(
            self.home, {"scope": "user"}, root := plugin(os.path.join(self.home, "cache", "0.5.0"))
        )
        _, _, err, calls = self.ctmux("s1", script=self.dst)
        self.assertEqual(err, "")
        self.assertEqual(calls[1][2:], created("s1", self.cwd, "/bin/zsh", root))

    def test_a_ctmux_away_from_its_plugin_fails_before_creating_a_session(self):
        away = os.path.join(self.home, "away")
        os.mkdir(away)
        copy = shutil.copy2(CTMUX, os.path.join(away, "ctmux"))
        process, out, err, calls = self.ctmux("s1", script=copy)
        self.assertEqual((process.returncode, out), (1, ""))
        self.assertRegex(err, r"\Actmux: no /.*/bin/ensemble-crew\n\Z")
        self.assertEqual([call[2] for call in calls], ["has"])

    def test_with_no_install_a_launcher_fails_naming_where_it_looked(self):
        self.install()
        process, out, err, calls = self.ctmux("s1", script=self.dst)
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual((out, calls), ("", []))
        self.assertIn(os.path.join(self.home, ".claude", "plugins", "installed_plugins.json"), err)
        self.assertIn(self.codex, err)
        self.assertIn(self.opencode, err)

    def test_an_install_failure_exits_nonzero_without_a_path(self):
        os.rmdir(self.ensemble)
        with open(self.ensemble, "w", encoding="utf-8"):
            pass
        process, out, _, calls = self.ctmux("--install")
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual((out, calls), ("", []))


class PrivateServer(Case):
    def setUp(self):
        self.server = live_tmux.Server(self, self.home)
        tools = os.path.join(self.home, "tools")
        python = os.path.join(self.home, "python")
        self.cwd = os.path.realpath(os.path.join(self.home, "caller dir"))
        for directory in (tools, python, self.cwd):
            os.mkdir(directory)
        os.symlink(sys.executable, os.path.join(python, "python3"))
        self.attach_log = os.path.join(self.home, "attach.log")
        self.proxy = executable(
            os.path.join(tools, "tmux"),
            PROXY.format(python=sys.executable, log=self.attach_log, program=self.server.program),
        )
        self.shell = executable(os.path.join(self.home, "shell"), SHELL)
        self.env = {
            "PATH": os.pathsep.join((tools, python, "/usr/bin", "/bin")),
            "HOME": self.home,
            "SHELL": self.shell,
            "TERM_PROGRAM": "iTerm.app",
            "TMUX_TMPDIR": self.server.root,
        }

    def ctmux(self, *args, script=CTMUX):
        return subprocess.run(
            (script, *args),
            cwd=self.cwd,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def attaches(self):
        with open(self.attach_log, encoding="utf-8") as f:
            return [json.loads(line) for line in f]

    def shell_report(self):
        try:
            with open(self.shell + ".log", encoding="utf-8") as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return None

    def roster(self, name):
        return Roster(os.path.join(self.ensemble, "managers", name))

    def show(self, scope, option):
        return self.server.tmux("show-options", scope + "v", option).stdout.strip()

    def state(self):
        sessions = self.server.tmux("list-sessions", "-F", "#{session_name} #{session_path}")
        options = [self.show(scope, option) for scope, option in OPTIONS]
        return sorted(sessions.stdout.splitlines()), options

    def test_a_new_session_holds_the_lease_for_its_shell_in_the_callers_directory(self):
        done = self.ctmux("s1")
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertEqual(self.attaches(), [["-CC", "attach", "-t", "=s1"]])
        self.assertEqual(
            [self.show(scope, option) for scope, option in OPTIONS[:3]], ["on", "50000", "on"]
        )
        self.assertIn("xterm*:extkeys", self.show("-s", "terminal-features"))
        cwd, args, manager, holder, generation, first = live_tmux.wait(
            self.shell_report, what="the held shell's report"
        )
        self.assertEqual((cwd, args, manager, generation), (self.cwd, "-l", "s1", "1"))
        self.assertEqual(first, os.path.join(REPO, "bin"))
        self.assertEqual((str(uuid.UUID(holder)), uuid.UUID(holder).version), (holder, 4))
        with self.assertRaisesRegex(Held, "held by s1"), self.roster("s1").hold(None):
            pass
        self.server.tmux("send-keys", "-t", "=s1:", "C-d")
        live_tmux.wait(lambda: self.free(self.roster("s1")), what="the lease to end with the shell")

    def test_an_existing_session_is_attached_untouched_and_names_match_exactly(self):
        for name in ("s1", "s1-x"):
            subprocess.run(
                (self.proxy, "new-session", "-d", "-s", name, *live_tmux.IDLE),
                cwd=self.home,
                env=self.env,
                timeout=30,
                check=True,
            )
        before = self.state()
        done = self.ctmux("s1")
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertEqual(self.attaches(), [["-CC", "attach", "-t", "=s1"]])
        self.assertEqual(self.state(), before)
        self.assertFalse(os.path.exists(os.path.join(self.ensemble, "managers")))
        done = self.ctmux("s1-")
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertEqual(self.attaches()[-1], ["-CC", "attach", "-t", "=s1-"])
        self.assertEqual(
            live_tmux.wait(self.shell_report, what="the held shell's report")[2], "s1-"
        )
        names = self.server.tmux("list-sessions", "-F", "#{session_name}").stdout.split()
        self.assertEqual(sorted(names), ["s1", "s1-", "s1-x"])

    def test_installed_launchers_run_off_path_and_follow_an_upgrade_without_a_reinstall(self):
        installed = os.path.join(self.ensemble, "bin")
        record(self.home, REPO)
        done = self.ctmux("--install")
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        upgraded = plugin(os.path.join(self.home, "cache", "0.6.0"))
        for name, root in (("s1", REPO), ("s2", upgraded)):
            with self.subTest(root=root):
                record(self.home, root)
                if os.path.exists(self.shell + ".log"):
                    os.unlink(self.shell + ".log")
                done = self.ctmux(name, script=os.path.join(installed, "ctmux"))
                self.assertEqual((done.returncode, done.stderr), (0, ""))
                self.assertEqual(self.attaches()[-1], ["-CC", "attach", "-t", f"={name}"])
                report = live_tmux.wait(self.shell_report, what="the held shell's report")
                self.assertEqual((report[2], report[5]), (name, os.path.join(root, "bin")))
                with self.assertRaisesRegex(Held, f"held by {name}"), self.roster(name).hold(None):
                    pass
                done = self.ctmux(
                    "status", "--manager", name, script=os.path.join(installed, "ensemble-crew")
                )
                self.assertEqual((done.returncode, done.stderr), (0, ""))
                self.server.tmux("send-keys", "-t", f"={name}:", "C-d")
                live_tmux.wait(lambda n=name: self.free(self.roster(n)), what="the lease to end")

    @staticmethod
    def free(roster):
        try:
            with roster.hold(None):
                return True
        except Held:
            return False
