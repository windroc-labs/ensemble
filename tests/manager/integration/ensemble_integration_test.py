"""ensemble (skills/manage/scripts/ensemble): its tmux calls through a fake tmux, its attach per
terminal, its new session on a private tmux server with the shell under its plugin's ensemble-crew
hold, and the launchers --install puts in ~/.ensemble/bin, run from a plain PATH before and after a
plugin upgrade.
"""

import concurrent.futures
import json
import os
import pty
import shutil
import signal
import stat
import subprocess
import sys
import uuid

from ensemble.manager.engine.roster import Roster
from ensemble.manager.engine.types import ActiveHolder, Held
from tests.manager.case import REPO, Case
from tests.session import live_tmux
from tests.utils.jsonl import read_jsonl

ENSEMBLE = os.path.join(REPO, "skills", "manage", "scripts", "ensemble")
CREW = os.path.join(REPO, "bin", "ensemble-crew")
LAUNCHER = os.path.join(REPO, "skills", "manage", "scripts", "launcher")
COMMANDS = (
    "ensemble",
    "ensemble-crew",
    "ensemble-session",
    "ensemble-delegate",
    "ensemble-orchestrator",
)
USAGE = "usage: ensemble <session-name> | ensemble list | ensemble --install"
FAKE = """#!{python} -I
import json, os, sys
with open(os.environ["FAKE_TMUX_LOG"], "a") as f:
    f.write(json.dumps([os.getcwd(), os.getpid(), *sys.argv[1:]]) + "\\n")
if sys.argv[1] == "list-panes":
    sys.stdout.write(os.environ.get("PANES", ""))
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
CLIENT = """#!/bin/sh
printf '["%s", "%s", "%s", "%s"]\\n' "$(pwd -P)" "$*" "$ENSEMBLE_MANAGER" "$ENSEMBLE_HOLDER_ID" >> "$0.log"
exec cat
"""
# HISTFILE empty: on hangup bash writes ~/.bash_history after the hold, which teardown waits for.
BASH = """#!/bin/sh
HISTFILE= exec /bin/bash --noprofile --norc
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


def held(name: str) -> list[str]:
    crew = os.path.join(REPO, "bin", "ensemble-crew")
    resume = ["/bin/sh", "-c", '"$@"; exec "${SHELL:-/bin/sh}" -l', "ensemble", crew]
    return [
        crew,
        "hold",
        f"--manager={name}",
        f"--session={name}",
        "--",
        *resume,
        "resume-commander",
    ]


def panes(command: str, name: str = "demo") -> str:
    """list-panes rows: the hold pane of session `name` running `command`, and a worker's pane
    running its attach command.
    """
    hold = f'"{REPO}/bin/ensemble-crew" hold --manager={name} --session={name} -- /bin/zsh -l'
    attach = "/usr/bin/python3 -I /x/guard.py --attach /tmp/eg-501/0123456789abcdef/g1/display"
    return f"%3 {command} {hold}\n%4 python3 {attach}\n"


LIST_PANES = [
    "list-panes",
    "-s",
    "-t",
    "=demo",
    "-F",
    "#{pane_id} #{pane_current_command} #{pane_start_command}",
]


def on_terminal(cwd: str, env: dict, answer: bytes) -> tuple[int, str]:
    """ensemble list with stdin and stdout on a terminal, answering its prompt: exit, stderr."""
    master, slave = pty.openpty()
    with os.fdopen(master, "r+b", buffering=0) as terminal:
        process = subprocess.Popen(
            (ENSEMBLE, "list"),
            cwd=cwd,
            env=env,
            stdin=slave,
            stdout=slave,
            stderr=subprocess.PIPE,
            text=True,
        )
        os.close(slave)
        terminal.write(answer)
        err = process.communicate(timeout=30)[1]
    return process.returncode, err


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
        self.dst = os.path.join(self.installed, "ensemble")
        self.codex = os.path.join(self.home, ".codex", "plugins", "cache", "windroc", "ensemble")
        self.opencode = os.path.join(self.home, ".cache", "opencode", "packages")
        self.env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": self.home,
            "SHELL": "/bin/zsh",
            "TERM_PROGRAM": "iTerm.app",
            "TMUX_TMPDIR": os.path.join(self.home, "tmux"),
            "FAKE_TMUX_LOG": self.log,
        }

    def run_ensemble(self, *args, script=ENSEMBLE, has_rc=1, **codes):
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
                process, _, err, calls = self.run_ensemble(*args)
                self.assertNotEqual(process.returncode, 0)
                self.assertIn(USAGE, err)
                self.assertEqual(calls, [])

    def test_an_existing_session_is_only_attached(self):
        _, _, _, calls = self.run_ensemble("s1", has_rc=0)
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
                    process, _, _, calls = self.run_ensemble("s1", has_rc=0, ATTACH_RC=5)
                finally:
                    self.env = base
                self.assertEqual(process.returncode, 5)
                self.assertEqual(calls[-1][1], process.pid)
                self.assertEqual(
                    [call[2:] for call in calls], [["has", "-t", "=s1"], [*attach, "-t", "=s1"]]
                )

    def test_an_absent_session_starts_from_home_in_the_callers_directory_with_its_shell_held(self):
        process, _, err, calls = self.run_ensemble("s1")
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
        _, _, _, calls = self.run_ensemble("s1")
        self.assertEqual(calls[1][2:], created("s1", self.cwd, "/bin/sh"))

    def test_only_the_first_argument_names_the_session_as_one_word(self):
        for args, name in ((("m a'n$x",), "m a'n$x"), (("s1", "extra", "--install"), "s1")):
            with self.subTest(args=args):
                _, _, _, calls = self.run_ensemble(*args)
                self.assertEqual(
                    [call[2:] for call in calls],
                    [
                        ["has", "-t", f"={name}"],
                        created(name, self.cwd, "/bin/zsh"),
                        ["-CC", "attach", "-t", f"={name}"],
                    ],
                )

    def test_a_failed_create_still_attaches_and_the_attach_status_wins(self):
        process, _, _, calls = self.run_ensemble("s1", START_RC=1, ATTACH_RC=7)
        self.assertEqual(process.returncode, 7)
        self.assertEqual([call[2] for call in calls], ["has", "start-server", "-CC"])

    def test_the_attach_replaces_ensemble_and_its_status_is_ensemble_s(self):
        process, _, _, calls = self.run_ensemble("s1", has_rc=0, ATTACH_RC=5)
        self.assertEqual(process.returncode, 5)
        self.assertEqual(calls[-1][1], process.pid)

    def test_the_script_names_no_client(self):
        with open(ENSEMBLE, "rb") as f:
            text = f.read().lower()
        self.assertNotIn(b"claude", text)
        self.assertNotIn(b"codex", text)

    def test_list_prints_without_tmux(self):
        os.makedirs(os.path.join(self.ensemble, "managers", "demo"))
        process = subprocess.run(
            (ENSEMBLE, "list"),
            cwd=self.cwd,
            env=self.env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(
            (process.returncode, process.stdout, process.stderr),
            (0, "demo\tnot-held\t-\t-\n  -\n", ""),
        )
        self.assertFalse(os.path.exists(self.log))

    def demo(self, **record) -> Roster:
        roster = Roster(os.path.join(self.ensemble, "managers", "demo"))
        os.makedirs(roster.directory)
        if record:
            fields = {"client": "kind-a", "sid": "s-1", "pid": 1 << 40, "cwd": self.cwd, **record}
            with open(os.path.join(roster.directory, "session.json"), "w", encoding="utf-8") as f:
                json.dump(fields, f)
        return roster

    def select(self, answer=b"1\n", has_rc=1, **env):
        """ensemble list with stdin and stdout on a terminal, answering its prompt."""
        if os.path.exists(self.log):
            os.unlink(self.log)
        values = {"HAS_RC": has_rc, **env}
        result, err = on_terminal(
            self.cwd, {**self.env, **{k: str(v) for k, v in values.items()}}, answer
        )
        calls = []
        if os.path.exists(self.log):
            with open(self.log, encoding="utf-8") as f:
                calls = [json.loads(line)[2:] for line in f]
        return result, err, calls

    def test_list_on_a_terminal_cancels_without_tmux(self):
        self.demo()
        result, err, calls = self.select(b"\n")
        self.assertEqual((result, calls), (0, []))
        self.assertIn("1) demo\tnot-held\t-\t-\n", err)

    def test_ctrl_c_at_the_prompt_cancels_with_exit_0(self):
        self.demo()
        master, slave = pty.openpty()
        errors, error_end = os.pipe()
        process = subprocess.Popen(
            (ENSEMBLE, "list"),
            cwd=self.cwd,
            env=self.env,
            stdin=slave,
            stdout=slave,
            stderr=error_end,
            start_new_session=True,
        )
        os.close(slave)
        os.close(error_end)
        try:
            err = b""
            while not err.endswith(b"Enter cancels: "):
                chunk = os.read(errors, 4096)
                self.assertTrue(chunk, err)
                err += chunk
            os.killpg(process.pid, signal.SIGINT)  # what the terminal's Ctrl-C sends
            result = process.wait(timeout=30)
            while chunk := os.read(errors, 4096):
                err += chunk
            self.assertEqual(result, 0, err.decode(errors="replace"))
        finally:
            os.close(master)
            os.close(errors)
        self.assertFalse(os.path.exists(self.log))

    def test_list_with_stdout_piped_prints_without_a_prompt(self):
        self.demo()
        master, slave = pty.openpty()
        try:
            done = subprocess.run(
                (ENSEMBLE, "list"),
                cwd=self.cwd,
                env=self.env,
                stdin=slave,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        finally:
            os.close(slave)
            os.close(master)
        self.assertEqual(
            (done.returncode, done.stdout, done.stderr), (0, "demo\tnot-held\t-\t-\n  -\n", "")
        )
        self.assertFalse(os.path.exists(self.log))

    def test_an_unheld_manager_without_a_record_opens_like_ensemble_name(self):
        self.demo()
        result, _, calls = self.select()
        self.assertEqual(result, 0)
        self.assertEqual(
            calls,
            [
                ["has", "-t", "=demo"],
                created("demo", self.cwd, "/bin/zsh"),
                ["-CC", "attach", "-t", "=demo"],
            ],
        )

    def test_an_unheld_record_resumes_under_a_new_hold(self):
        self.demo(cwd="/home/user/project")
        result, _, calls = self.select()
        self.assertEqual(result, 0)
        self.assertEqual(
            calls,
            [
                ["has", "-t", "=demo"],
                [*START, "demo", "-c", self.cwd, *held("demo")],
                ["-CC", "attach", "-t", "=demo"],
            ],
        )
        result, _, calls = self.select(has_rc=0)
        self.assertEqual(
            calls,
            [
                ["has", "-t", "=demo"],
                ["new-window", "-d", "-t", "=demo:", "-c", self.cwd, *held("demo")],
                ["-CC", "attach", "-t", "=demo"],
            ],
        )

    def test_a_lost_create_race_still_attaches(self):
        self.demo(cwd="/home/user/project")
        result, _, calls = self.select(START_RC=1, ATTACH_RC=7)
        self.assertEqual(result, 7)
        self.assertEqual([call[0] for call in calls], ["has", "start-server", "-CC"])

    def test_a_hash_in_the_callers_directory_reaches_tmux_doubled(self):
        self.cwd = os.path.join(self.cwd, "x#{l:t}")
        os.mkdir(self.cwd)
        doubled = self.cwd.replace("#", "##")
        _, _, _, calls = self.run_ensemble("s1")
        self.assertEqual(calls[1][2:], created("s1", doubled, "/bin/zsh"))
        self.demo()
        self.assertEqual(self.select()[2][1], created("demo", doubled, "/bin/zsh"))

    def test_a_running_client_under_its_own_hold_is_attached(self):
        roster = self.demo(pid=os.getpid())
        with roster.hold("demo"):
            result, _, calls = self.select(PANES=panes("node"))
        self.assertEqual((result, calls), (0, [LIST_PANES, ["-CC", "attach", "-t", "=demo"]]))

    def test_hold_only_types_the_resume_command_into_its_idle_shell(self):
        roster = self.demo(cwd="/home/user/project")
        with roster.hold("demo"):
            result, err, calls = self.select(PANES=panes("zsh"))
        self.assertEqual((result, err.endswith(": "), calls[0]), (0, True, LIST_PANES))
        self.assertEqual(
            calls[1:],
            [
                [
                    *("send-keys", "-t", "%3", "C-u", ";"),
                    *("send-keys", "-t", "%3", "-l", f"'{CREW}' resume-commander", ";"),
                    *("send-keys", "-t", "%3", "Enter"),
                ],
                ["-CC", "attach", "-t", "=demo"],
            ],
        )

    def test_a_busy_hold_pane_is_attached_without_typing(self):
        roster = self.demo(cwd="/home/user/project")
        with roster.hold("demo"):
            result, err, calls = self.select(PANES=panes("vim"))
        self.assertEqual((result, calls), (0, [LIST_PANES, ["-CC", "attach", "-t", "=demo"]]))
        self.assertIn(
            f"ensemble: demo's pane runs vim; resume there with: '{CREW}' resume-commander\n",
            err,
        )

    def test_its_own_hold_outside_its_tmux_session_opens_nothing(self):
        roster = self.demo()
        for pane_rows in ("", panes("zsh", "other"), panes("zsh") * 2):
            with self.subTest(pane_rows=pane_rows), roster.hold("demo"):
                result, err, calls = self.select(PANES=pane_rows)
                self.assertEqual((result, calls), (1, [LIST_PANES]))
                self.assertTrue(
                    err.endswith(
                        "ensemble: manager demo is held by demo outside tmux session demo;"
                        " nothing opened\n"
                    )
                )

    def test_a_manager_held_elsewhere_opens_nothing(self):
        roster = self.demo()
        with roster.hold("work"):
            result, err, calls = self.select()
        self.assertEqual((result, calls), (1, []))
        self.assertTrue(err.endswith("crew: manager demo: held by work; nothing opened\n"))

    def install(self, script=ENSEMBLE):
        process, out, err, calls = self.run_ensemble("--install", script=script)
        paths = [os.path.join(self.installed, name) for name in COMMANDS]
        self.assertEqual(
            (process.returncode, out, err, calls), (0, "".join(f"{p}\n" for p in paths), "", [])
        )

    def legacy_install(self):
        """Exercise discovery for launchers copied before source selection was recorded."""
        self.install()
        os.unlink(os.path.join(self.ensemble, "plugin-root"))

    def test_install_pins_the_source_despite_other_clients_and_equal_version_packages(self):
        selected = self.opencode_install("0.5.0", spec="selected ref with spaces")
        self.install(script=os.path.join(selected, "skills", "manage", "scripts", "ensemble"))
        record(self.home, plugin(os.path.join(self.home, "cache", "9.0.0")))
        plugin(os.path.join(self.codex, "9.0.0"))
        self.opencode_install("9.0.0")
        self.opencode_install("0.5.0", spec="equal-version-leftover")
        for name in COMMANDS:
            with self.subTest(name=name):
                if name == "ensemble":
                    process, _, err, calls = self.run_ensemble("s1", script=self.dst)
                    self.assertEqual((process.returncode, err), (0, ""))
                    self.assertEqual(calls[1][2:], created("s1", self.cwd, "/bin/zsh", selected))
                else:
                    target = os.path.join(selected, "bin", name)
                    executable(target, '#!/bin/sh\nprintf "%s\\n" "$0"\n')
                    process, out, err, _ = self.run_ensemble(
                        script=os.path.join(self.installed, name)
                    )
                    self.assertEqual((process.returncode, out, err), (0, target + "\n", ""))

    def test_reinstall_switches_source_even_when_launcher_bytes_match(self):
        self.install()
        selected = self.opencode_install("0.5.0")
        self.install(script=os.path.join(selected, "skills", "manage", "scripts", "ensemble"))
        process, _, err, calls = self.run_ensemble("s1", script=self.dst)
        self.assertEqual((process.returncode, err), (0, ""))
        self.assertEqual(calls[1][2:], created("s1", self.cwd, "/bin/zsh", selected))

    def test_malformed_selection_records_fail_without_running_a_plugin(self):
        self.install()
        selection = os.path.join(self.ensemble, "plugin-root")
        valid = {"root": REPO, "client": "", "source": ""}
        malformed = [REPO + "\n", json.dumps(REPO), "null", "[]", "{}"]
        for field in valid:
            malformed.append(json.dumps({k: v for k, v in valid.items() if k != field}))
            malformed.append(json.dumps({**valid, field: None}))
        for text in malformed:
            with self.subTest(record=text):
                with open(selection, "w", encoding="utf-8") as f:
                    f.write(text)
                process, out, err, calls = self.run_ensemble("s1", script=self.dst)
                self.assertNotEqual(process.returncode, 0)
                self.assertEqual((out, calls), ("", []))
                self.assertIn(f"ensemble: cannot read {selection}:", err)
                self.assertIn("rerun install.sh or the plugin's ensemble --install", err)
                self.assertNotIn("Traceback", err)
                with open(selection, encoding="utf-8") as f:
                    self.assertEqual(f.read(), text)

    def test_a_missing_selected_command_fails_without_using_another_plugin(self):
        selected = self.opencode_install("0.5.0")
        self.install(script=os.path.join(selected, "skills", "manage", "scripts", "ensemble"))
        record(self.home, REPO)
        os.unlink(os.path.join(selected, "bin", "ensemble-crew"))
        process, out, err, calls = self.run_ensemble(
            script=os.path.join(self.installed, "ensemble-crew")
        )
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual((out, calls), ("", []))
        self.assertIn(selected, err)
        self.assertIn("--install", err)

    def test_missing_selected_path_recovers_only_from_the_same_client(self):
        for client in ("claude", "codex", "opencode"):
            with self.subTest(client=client):
                if client == "claude":
                    selected = plugin(os.path.join(self.home, "cache", "0.5.0"))
                    record(self.home, selected)
                    newer = plugin(os.path.join(self.home, "cache", "0.10.0"))
                elif client == "codex":
                    selected = plugin(os.path.join(self.codex, "0.5.0"))
                    newer = plugin(os.path.join(self.codex, "0.10.0"))
                else:
                    selected = self.opencode_install("0.5.0")
                    # The same ref under another spec spelling: only that ref's copies stand in.
                    spec = "ensemble@git+ssh:/git@github.com/windroc-labs/ensemble.git#v0.5.0"
                    newer = self.opencode_install("0.10.0", spec=spec)
                self.install(script=os.path.join(selected, "skills/manage/scripts/ensemble"))
                shutil.rmtree(selected)
                if client == "claude":
                    record(self.home, newer)
                for attempt in range(2):
                    process, _, err, calls = self.run_ensemble("s1", script=self.dst)
                    self.assertEqual(process.returncode, 0, err)
                    self.assertEqual(calls[1][2:], created("s1", self.cwd, "/bin/zsh", newer))
                    self.assertEqual(
                        err, f"ensemble: re-selected plugin {newer}\n" if attempt == 0 else ""
                    )
                with open(os.path.join(self.ensemble, "plugin-root"), encoding="utf-8") as f:
                    self.assertEqual(json.load(f)["root"], newer)
                shutil.rmtree(newer)

    def test_missing_selected_path_with_only_another_client_or_no_copy_fails(self):
        for client in ("claude", "codex", "opencode"):
            for other in (False, True):
                with self.subTest(client=client, other=other):
                    if client == "claude":
                        selected = plugin(os.path.join(self.home, "cache", "0.5.0"))
                        record(self.home, selected)
                    elif client == "codex":
                        selected = plugin(os.path.join(self.codex, "0.5.0"))
                    else:
                        selected = self.opencode_install("0.5.0")
                    self.install(script=os.path.join(selected, "skills/manage/scripts/ensemble"))
                    shutil.rmtree(selected)
                    leftover = None
                    if other:
                        leftover = (
                            self.opencode_install("99.0.0")
                            if client != "opencode"
                            else plugin(os.path.join(self.codex, "99.0.0"))
                        )
                    process, out, err, calls = self.run_ensemble("s1", script=self.dst)
                    self.assertNotEqual(process.returncode, 0)
                    self.assertEqual((out, calls), ("", []))
                    self.assertEqual(
                        err,
                        f"ensemble: selected plugin {selected!r} has no executable skills/manage/scripts/ensemble; "
                        "rerun install.sh or the plugin's ensemble --install\n",
                    )
                    if leftover:
                        shutil.rmtree(leftover)

    def test_x1_missing_selected_path_refuses_another_ref(self):
        selected = self.opencode_install(
            "0.5.0", spec="ensemble@git+https:/github.com/x/e.git#WDRC-241"
        )
        self.install(script=os.path.join(selected, "skills/manage/scripts/ensemble"))
        for spec in (
            "ensemble@git+https:/github.com/x/e.git#main",
            "ensemble@git+https:/github.com/x/e.git",
        ):
            self.opencode_install("0.10.0", spec=spec)
        shutil.rmtree(selected)
        selection = os.path.join(self.ensemble, "plugin-root")
        with open(selection, encoding="utf-8") as f:
            text = f.read()
        process, out, err, calls = self.run_ensemble("s1", script=self.dst)
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual((out, calls), ("", []))
        self.assertEqual(
            err,
            f"ensemble: selected plugin {selected!r} has no executable skills/manage/scripts/ensemble; "
            "rerun install.sh or the plugin's ensemble --install\n",
        )
        with open(selection, encoding="utf-8") as f:
            self.assertEqual(f.read(), text)

    def test_legacy_equal_versions_use_path_order_instead_of_creation_order(self):
        self.legacy_install()
        for spec in ("z-last", "a-first"):
            self.opencode_install("0.5.0", spec=spec)
        for _ in range(2):
            process, _, err, calls = self.run_ensemble("s1", script=self.dst)
            self.assertEqual((process.returncode, err), (0, ""))
            expected = os.path.join(self.opencode, "a-first", "node_modules", "ensemble")
            self.assertEqual(calls[1][2:], created("s1", self.cwd, "/bin/zsh", expected))
            # Reverse creation order without changing either candidate's version or path.
            shutil.rmtree(os.path.join(self.opencode, "z-last"))
            self.opencode_install("0.5.0", spec="z-last")

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

    def test_the_installed_ensemble_runs_the_recorded_plugins_and_follows_an_upgrade(self):
        self.legacy_install()
        for version in ("0.5.0", "0.6.0"):
            with self.subTest(version=version):
                root = plugin(os.path.join(self.home, "cache", version))
                record(self.home, root)
                process, _, err, calls = self.run_ensemble("s1", script=self.dst)
                self.assertEqual((process.returncode, err), (0, ""))
                self.assertEqual(calls[1][2:], created("s1", self.cwd, "/bin/zsh", root))

    def test_legacy_claude_discovery_keeps_record_order_across_versions(self):
        self.legacy_install()
        first = plugin(os.path.join(self.home, "cache", "0.5.0"))
        newer = plugin(os.path.join(self.home, "cache", "0.10.0"))
        record(self.home, first, newer)
        process, _, err, calls = self.run_ensemble("s1", script=self.dst)
        self.assertEqual((process.returncode, err), (0, ""))
        self.assertEqual(calls[1][2:], created("s1", self.cwd, "/bin/zsh", first))

    def test_without_a_claude_record_the_newest_codex_install_runs(self):
        self.legacy_install()
        for version in ("0.9.0", "0.10.0"):
            plugin(os.path.join(self.codex, version))
        _, _, err, calls = self.run_ensemble("s1", script=self.dst)
        self.assertEqual(err, "")
        self.assertEqual(
            calls[1][2:], created("s1", self.cwd, "/bin/zsh", os.path.join(self.codex, "0.10.0"))
        )

    def test_a_claude_record_wins_over_a_codex_install(self):
        self.legacy_install()
        plugin(os.path.join(self.codex, "9.0.0"))
        record(self.home, root := plugin(os.path.join(self.home, "cache", "0.5.0")))
        _, _, _, calls = self.run_ensemble("s1", script=self.dst)
        self.assertEqual(calls[1][2:], created("s1", self.cwd, "/bin/zsh", root))

    def opencode_install(self, version: str, spec: str = "") -> str:
        """OpenCode's install of the git spec pinned to `version` (or `spec`): its package dir."""
        spec = spec or f"ensemble@git+https:/github.com/windroc-labs/ensemble.git#v{version}"
        root = plugin(os.path.join(self.opencode, spec, "node_modules", "ensemble"))
        with open(os.path.join(root, "package.json"), "w", encoding="utf-8") as f:
            json.dump({"name": "ensemble", "version": version}, f)
        return root

    def test_without_claude_or_codex_the_newest_opencode_install_runs(self):
        self.legacy_install()
        for version in ("0.9.0", "0.10.0"):
            self.opencode_install(version)
        newest = os.path.join(
            self.opencode, "ensemble@git+https:/github.com/windroc-labs/ensemble.git#v0.10.0"
        )
        _, _, err, calls = self.run_ensemble("s1", script=self.dst)
        self.assertEqual(err, "")
        self.assertEqual(
            calls[1][2:],
            created("s1", self.cwd, "/bin/zsh", os.path.join(newest, "node_modules", "ensemble")),
        )
        cache = os.path.join(self.home, "xdg cache")
        self.opencode = os.path.join(cache, "opencode", "packages")
        root = self.opencode_install("0.1.0", spec="ensemble")
        _, _, err, calls = self.run_ensemble("s1", script=self.dst, XDG_CACHE_HOME=cache)
        self.assertEqual(err, "")
        self.assertEqual(calls[1][2:], created("s1", self.cwd, "/bin/zsh", root))

    def test_claude_and_codex_installs_win_over_an_opencode_install(self):
        self.legacy_install()
        self.opencode_install("9.0.0")
        plugin(os.path.join(self.codex, "0.5.0"))
        _, _, _, calls = self.run_ensemble("s1", script=self.dst)
        self.assertEqual(
            calls[1][2:], created("s1", self.cwd, "/bin/zsh", os.path.join(self.codex, "0.5.0"))
        )
        record(self.home, root := plugin(os.path.join(self.home, "cache", "0.4.0")))
        _, _, _, calls = self.run_ensemble("s1", script=self.dst)
        self.assertEqual(calls[1][2:], created("s1", self.cwd, "/bin/zsh", root))

    def test_a_launcher_passes_its_arguments_stdin_and_status(self):
        self.legacy_install()
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
        self.legacy_install()
        record(
            self.home, {"scope": "user"}, root := plugin(os.path.join(self.home, "cache", "0.5.0"))
        )
        _, _, err, calls = self.run_ensemble("s1", script=self.dst)
        self.assertEqual(err, "")
        self.assertEqual(calls[1][2:], created("s1", self.cwd, "/bin/zsh", root))

    def test_an_ensemble_away_from_its_plugin_fails_before_creating_a_session(self):
        away = os.path.join(self.home, "away")
        os.mkdir(away)
        copy = shutil.copy2(ENSEMBLE, os.path.join(away, "ensemble"))
        process, out, err, calls = self.run_ensemble("s1", script=copy)
        self.assertEqual((process.returncode, out), (1, ""))
        self.assertRegex(err, r"\Aensemble: no /.*/bin/ensemble-crew\n\Z")
        self.assertEqual([call[2] for call in calls], ["has"])

    def test_with_no_install_a_launcher_fails_naming_where_it_looked(self):
        self.legacy_install()
        process, out, err, calls = self.run_ensemble("s1", script=self.dst)
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual((out, calls), ("", []))
        self.assertIn(os.path.join(self.home, ".claude", "plugins", "installed_plugins.json"), err)
        self.assertIn(self.codex, err)
        self.assertIn(self.opencode, err)

    def test_an_install_failure_exits_nonzero_without_a_path(self):
        os.rmdir(self.ensemble)
        with open(self.ensemble, "w", encoding="utf-8"):
            pass
        process, out, _, calls = self.run_ensemble("--install")
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

    def run_ensemble(self, *args, script=ENSEMBLE):
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
        return read_jsonl(self.attach_log)

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
        done = self.run_ensemble("s1")
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
        done = self.run_ensemble("s1")
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertEqual(self.attaches(), [["-CC", "attach", "-t", "=s1"]])
        self.assertEqual(self.state(), before)
        self.assertFalse(os.path.exists(os.path.join(self.ensemble, "managers")))
        done = self.run_ensemble("s1-")
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertEqual(self.attaches()[-1], ["-CC", "attach", "-t", "=s1-"])
        self.assertEqual(
            live_tmux.wait(self.shell_report, what="the held shell's report")[2], "s1-"
        )
        names = self.server.tmux("list-sessions", "-F", "#{session_name}").stdout.split()
        self.assertEqual(sorted(names), ["s1", "s1-", "s1-x"])

    def test_installed_launchers_run_off_path_and_switch_source_on_reinstall(self):
        installed = os.path.join(self.ensemble, "bin")
        record(self.home, REPO)
        done = self.run_ensemble("--install")
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        upgraded = plugin(os.path.join(self.home, "cache", "0.6.0"))
        for name, root in (("s1", REPO), ("s2", upgraded)):
            with self.subTest(root=root):
                record(self.home, root)
                done = self.run_ensemble(
                    "--install", script=os.path.join(root, "skills/manage/scripts/ensemble")
                )
                self.assertEqual((done.returncode, done.stderr), (0, ""))
                if os.path.exists(self.shell + ".log"):
                    os.unlink(self.shell + ".log")
                done = self.run_ensemble(name, script=os.path.join(installed, "ensemble"))
                self.assertEqual((done.returncode, done.stderr), (0, ""))
                self.assertEqual(self.attaches()[-1], ["-CC", "attach", "-t", f"={name}"])
                report = live_tmux.wait(self.shell_report, what="the held shell's report")
                self.assertEqual((report[2], report[5]), (name, os.path.join(root, "bin")))
                with self.assertRaisesRegex(Held, f"held by {name}"), self.roster(name).hold(None):
                    pass
                done = self.run_ensemble(
                    "status", "--manager", name, script=os.path.join(installed, "ensemble-crew")
                )
                self.assertEqual((done.returncode, done.stderr), (0, ""))
                self.server.tmux("send-keys", "-t", f"={name}:", "C-d")
                live_tmux.wait(lambda n=name: self.free(self.roster(n)), what="the lease to end")

    def client(self):
        """A fake client on PATH, under the recorded kind's executable name, logging each start."""
        executable(os.path.join(self.home, "tools", "codex"), CLIENT)
        work = os.path.realpath(os.path.join(self.home, "work dir"))
        os.mkdir(work)
        directory = self.roster("demo").directory
        os.makedirs(directory, exist_ok=True)
        record = {"client": "codex", "sid": "t-1", "pid": 1 << 40, "cwd": work}
        with open(os.path.join(directory, "session.json"), "w", encoding="utf-8") as f:
            json.dump(record, f)
        return work

    def starts(self):
        return read_jsonl(os.path.join(self.home, "tools", "codex.log"), missing_ok=True)

    def test_the_hold_pane_is_matched_by_its_start_command(self):
        self.env["SHELL"] = "/bin/sh"
        self.assertEqual(self.run_ensemble("demo").returncode, 0)
        rows = self.server.tmux("list-panes", "-s", "-t", "=demo", "-F", "#{pane_start_command}")
        self.assertIn(" hold --manager=demo --session=demo -- ", rows.stdout)

    def test_hold_only_resumes_the_recorded_client_under_the_same_hold(self):
        work = self.client()
        # macOS runs /bin/sh as bash; a profile may reset PATH, losing the fake client. Off PATH, so
        # it never stands in for env bash.
        os.mkdir(os.path.join(self.home, "login"))
        self.env["SHELL"] = executable(os.path.join(self.home, "login", "bash"), BASH)
        self.assertEqual(self.run_ensemble("demo").returncode, 0)
        command = ("display-message", "-p", "-t", "=demo:", "#{pane_current_command}")
        live_tmux.wait(
            lambda: self.server.tmux(*command).stdout.strip() == "bash", what="idle bash"
        )
        result, err = on_terminal(self.cwd, self.env, b"1\n")
        self.assertEqual(result, 0, err)
        starts = live_tmux.wait(self.starts, what="the resumed client")
        self.assertEqual(starts[0][:3], [work, "resume t-1", "demo"])
        self.assertEqual(self.roster("demo").snapshot()[0], ActiveHolder(starts[0][3], "demo"))
        with self.assertRaisesRegex(Held, "held by demo"), self.roster("demo").hold(None):
            pass
        sessions = self.server.tmux("list-sessions", "-F", "#{session_name}").stdout.split()
        self.assertEqual(sessions, ["demo"])

    def test_concurrent_selections_of_an_unheld_manager_start_one_client(self):
        work = self.client()
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            done = list(pool.map(lambda _: on_terminal(self.cwd, self.env, b"1\n"), range(2)))
        self.assertEqual([result for result, _ in done], [0, 0], done)
        starts = live_tmux.wait(self.starts, what="the resumed client")
        live_tmux.wait(
            lambda: self.server.tmux("list-panes", "-s", "-t", "=demo").stdout.count("\n") == 1,
            what="one pane",
        )
        self.assertEqual([start[:3] for start in self.starts()], [[work, "resume t-1", "demo"]])
        self.assertEqual(len(starts), 1)
        self.assertEqual(self.roster("demo").snapshot()[0], ActiveHolder(starts[0][3], "demo"))
        sessions = self.server.tmux("list-sessions", "-F", "#{session_name}").stdout.split()
        self.assertEqual(sessions, ["demo"])
        with self.assertRaisesRegex(Held, "held by demo"), self.roster("demo").hold(None):
            pass

    @staticmethod
    def free(roster):
        try:
            with roster.hold(None):
                return True
        except Held:
            return False
