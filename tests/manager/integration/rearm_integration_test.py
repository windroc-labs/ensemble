"""The SessionStart hook run as hooks/hooks.json runs it: real sh and python3, a temp HOME.
RearmHookTest's hook runs under this test process, its stand-in hold; HoldTreeTest's under a real
ensemble-crew hold.
"""

import json
import os
import select
import shlex
import subprocess
import sys
import time
from pathlib import Path

from ensemble.manager import Roster
from tests.manager import session_records
from tests.manager.case import PY, REPO, Case, child_env


def _command(*, manual: bool = False) -> str:
    with open(
        os.path.join(REPO, ".codex-plugin" if manual else ".claude-plugin", "plugin.json"),
        encoding="utf-8",
    ) as f:
        config = json.load(f)["hooks"]
    with open(os.path.join(REPO, config), encoding="utf-8") as f:
        (entry,) = json.load(f)["hooks"]["SessionStart"]
    return entry["hooks"][0]["command"]


def _start(source: object) -> bytes:
    return json.dumps({"hook_event_name": "SessionStart", "source": source}).encode()


class RearmHookTest(Case):
    def setUp(self):
        super().setUp()
        roster = Roster(str(Path(self.ensemble, "managers", "mgr")))
        lease = self.enterContext(roster.hold("mgr"))
        self.manager = {
            "ENSEMBLE_MANAGER": "mgr",
            "ENSEMBLE_HOLDER_ID": lease.id,
            "ENSEMBLE_HOLDER_GENERATION": str(lease.generation),
        }

    def run_hook(self, extra: dict[str, str], stdin: bytes, prefix: str = "", *, manual=False):
        env = {
            **os.environ,
            "CLAUDE_PLUGIN_ROOT": REPO,
            "PLUGIN_ROOT": REPO,
            "ENSEMBLE_HOLDER_PID": str(os.getpid()),
            **extra,
        }
        started = time.monotonic()
        done = subprocess.run(
            ("sh", "-c", prefix + _command(manual=manual)),
            input=stdin,
            capture_output=True,
            env=env,
            timeout=10,
            check=False,
        )
        self.assertLess(time.monotonic() - started, 1)
        return done

    def test_manual_plugin_command(self):
        root = Path(self.home, "plugin root with spaces")
        root.symlink_to(REPO, target_is_directory=True)
        for source in ("startup", "resume", "compact", "clear"):
            with self.subTest(source):
                done = self.run_hook(
                    {**self.manager, "PLUGIN_ROOT": str(root)}, _start(source), manual=True
                )
                self.assertEqual((done.returncode, done.stderr), (0, b""))
                context = json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"]
                self.assertIn(f"`{source}` in manager `mgr`", context)
                self.assertIn("Manual monitoring; no idle notifications", context)
                self.assertNotIn("Monitor", context)
        for extra in ({}, {**self.manager, "ENSEMBLE_HOLDER_PID": "1"}):
            with self.subTest(extra=extra):
                done = self.run_hook(extra, _start("startup"), manual=True)
                self.assertEqual((done.returncode, done.stdout, done.stderr), (0, b"", b""))

    def test_hooks_json_command(self):
        silent = {
            "no manager": ({"ENSEMBLE_HOLDER_ID": "h"}, _start("startup")),
            "hold not an ancestor": (
                {**self.manager, "ENSEMBLE_HOLDER_PID": "1"},
                _start("startup"),
            ),
            "non-JSON": (self.manager, b"startup"),
        }
        for label, (extra, stdin) in silent.items():
            with self.subTest(label):
                done = self.run_hook(extra, stdin)
                self.assertEqual((done.returncode, done.stdout, done.stderr), (0, b"", b""))
        for source in ("startup", "resume", "compact", "clear"):
            with self.subTest(source):
                done = self.run_hook(self.manager, _start(source))
                self.assertEqual((done.returncode, done.stderr), (0, b""))
                context = json.loads(done.stdout)["hookSpecificOutput"]
                self.assertEqual(context["hookEventName"], "SessionStart")
                self.assertIn(f"`{source}` in manager `mgr`", context["additionalContext"])
        for label, prefix in (("closed stdout", "exec >&-; "), ("closed stdin", "exec <&-; ")):
            with self.subTest(label):
                done = self.run_hook(self.manager, _start("resume"), prefix)
                self.assertEqual((done.returncode, done.stdout, done.stderr), (0, b"", b""))

    def test_each_hook_command_writes_its_record(self):
        roster = Path(self.ensemble, "managers", "mgr")
        before = set(roster.iterdir())
        for client, manual in (("claude", False), ("codex", True)):
            call = session_records.CALLS[client]
            with self.subTest(client):
                done = self.run_hook(self.manager, json.dumps(call.stdin).encode(), manual=manual)
                self.assertEqual((done.returncode, done.stderr), (0, b""))
                context = json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"]
                self.assertIn("`startup` in manager `mgr`", context)
                # sh -c's $PPID: this process, as the client is the hook shell's parent.
                expected = {**session_records.VALID[client], "pid": os.getpid()}
                self.assertEqual(json.loads((roster / "session.json").read_text()), expected)
        cleared = {**session_records.CALLS["claude"].stdin, "source": "clear", "session_id": "s2"}
        done = self.run_hook(self.manager, json.dumps(cleared).encode())
        self.assertEqual((done.returncode, done.stderr), (0, b""))
        self.assertEqual(json.loads((roster / "session.json").read_text())["sid"], "s2")
        self.assertEqual(set(roster.iterdir()), before | {roster / "session.json"})

    def test_broken_pipe(self):
        read, write = os.pipe()
        os.close(read)
        try:
            done = subprocess.run(
                ("sh", "-c", _command()),
                input=_start("clear"),
                stdout=write,
                stderr=subprocess.PIPE,
                env={
                    **os.environ,
                    "CLAUDE_PLUGIN_ROOT": REPO,
                    "ENSEMBLE_HOLDER_PID": str(os.getpid()),
                    **self.manager,
                },
                timeout=10,
                check=False,
            )
        finally:
            os.close(write)
        self.assertEqual((done.returncode, done.stderr), (0, b""))

    def test_leaves_state_untouched(self):
        roster = Path(self.ensemble, "managers", "mgr")
        (roster / "events").write_text("1 12:00:00 w1 done\n")
        (roster / "handled").write_text("0 1\n")
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in roster.iterdir()}
        tree = sorted(Path(self.home).rglob("*"))
        for manual in (False, True):
            done = self.run_hook(self.manager, _start("compact"), manual=manual)
            self.assertEqual(done.returncode, 0)
        self.assertEqual(
            {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in roster.iterdir()}, before
        )
        self.assertEqual(sorted(Path(self.home).rglob("*")), tree)


# argv: a file to write this process's environment to, then one that ends the wait
WAIT = (
    "import json,os,pathlib,sys,time\n"
    "env,done=map(pathlib.Path,sys.argv[1:])\n"
    "env.with_suffix('.tmp').write_text(json.dumps(dict(os.environ)))\n"
    "env.with_suffix('.tmp').rename(env)\n"
    "while not done.exists(): time.sleep(.01)"
)


class HoldTreeTest(Case):
    """Mode `hold -- <client argv>` (the hold runs the hook's sh itself) and mode `ensemble` (the hold
    runs a shell that starts the hook, again after it exits); a sibling has the hold's environment
    but runs outside its process tree.
    """

    def hold(self, *argv: str, **options) -> subprocess.Popen:
        env = {**child_env(), "CLAUDE_PLUGIN_ROOT": REPO, "PLUGIN_ROOT": REPO}
        env.pop("ENSEMBLE_AGENT_SESSION", None)
        return subprocess.Popen(
            (*PY, "-m", "ensemble.manager.cli", "hold", "--manager", "mgr", "--", *argv),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **options,
        )

    def contexts(self, out: bytes) -> list[str]:
        return [
            json.loads(line)["hookSpecificOutput"]["additionalContext"]
            for line in out.decode().splitlines()
        ]

    def test_a_client_hold_runs_gets_the_prompt(self):
        for manual in (False, True):
            for source in ("startup", "resume", "compact", "clear"):
                with self.subTest(manual=manual, source=source):
                    process = self.hold("sh", "-c", _command(manual=manual), stdin=subprocess.PIPE)
                    out, err = process.communicate(_start(source), timeout=10)
                    self.assertEqual((process.returncode, err), (0, b""))
                    (context,) = self.contexts(out)
                    self.assertIn(f"`{source}` in manager `mgr`", context)
                    self.assertEqual("Manual monitoring" in context, manual)

    def test_a_client_the_hold_shell_starts_again_gets_the_prompt(self):
        for source in ("startup", "resume", "compact"):
            Path(self.home, source).write_bytes(_start(source))
        hook = _command()
        shell = (
            f"cd {self.home} && sh -c '{hook} < startup' && sh -c '{hook} < compact'"
            f" && sh -c '{hook} < resume'"
        )
        process = self.hold("sh", "-c", shell, stdin=subprocess.DEVNULL)
        out, err = process.communicate(timeout=10)
        self.assertEqual((process.returncode, err), (0, b""))
        contexts = self.contexts(out)
        self.assertEqual(len(contexts), 3)
        for context, source in zip(contexts, ("startup", "compact", "resume"), strict=True):
            self.assertIn(f"`{source}` in manager `mgr`", context)

    def test_a_sibling_with_the_holds_environment_gets_nothing(self):
        env, done = Path(self.home, "env.json"), Path(self.home, "done")
        process = self.hold(sys.executable, "-I", "-S", "-c", WAIT, str(env), str(done))
        self.addCleanup(process.wait, 10)
        self.addCleanup(done.touch)
        deadline = time.monotonic() + 10
        while not env.exists():
            if process.poll() is not None:
                self.fail(process.communicate()[1])
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        held = json.loads(env.read_text())
        self.assertEqual(held["ENSEMBLE_HOLDER_PID"], str(process.pid))
        for manual in (False, True):
            for source in ("startup", "resume", "compact", "clear"):
                with self.subTest(manual=manual, source=source):
                    sibling = subprocess.run(
                        ("sh", "-c", _command(manual=manual)),
                        input=_start(source),
                        capture_output=True,
                        env=held,
                        timeout=10,
                        check=False,
                    )
                    self.assertEqual(
                        (sibling.returncode, sibling.stdout, sibling.stderr), (0, b"", b"")
                    )
        self.assertIsNone(process.poll())


class SharedCodexTest(Case):
    def line(self, process):
        assert process.stdout is not None
        self.assertTrue(select.select([process.stdout], [], [], 15)[0], "client response timed out")
        line = process.stdout.readline()
        self.assertTrue(line, "client exited before its response")
        return line

    def close(self, process):
        if process.stdin is not None:
            process.stdin.close()
            process.stdin = None
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=10)

    def start(self, name, env, *, cwd=None):
        process = subprocess.Popen(
            (*PY, "-m", "ensemble.manager.cli", "hold", "--manager", name, "--", "/bin/sh"),
            env=env,
            cwd=cwd or self.home,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self.addCleanup(self.close, process)
        roster = Roster(str(Path(self.ensemble, "managers", name)))
        deadline = time.monotonic() + 10
        while roster.snapshot()[0] is None:
            self.assertIsNone(process.poll())
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        return process

    def send(self, process, line):
        assert process.stdin is not None
        process.stdin.write((line + "\n").encode())
        process.stdin.flush()

    def saved(self, name):
        return json.loads(Path(self.ensemble, "managers", name, "session.json").read_text())

    def environment(self):
        fake = os.path.join(REPO, "tests", "manager", "shared_codex.py")
        binary = Path(self.home, "bin")
        binary.mkdir()
        launchers = Path(self.home, "launchers")
        launchers.mkdir()
        entry = launchers / "codex"
        script = os.path.join(REPO, "skills", "manage", "scripts", "codex")
        entry.write_text(f'#!/bin/sh\nexec {shlex.quote(script)} --ensemble-launcher "$0" "$@"\n')
        entry.chmod(0o755)
        native = binary / "codex"
        native.write_text(
            f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -I {shlex.quote(fake)} "$@"\n'
        )
        native.chmod(0o755)
        path = str(Path(self.home, "daemon.sock"))
        env = {
            **child_env(),
            "PLUGIN_ROOT": REPO,
            "FAKE_DAEMON_SOCKET": path,
            "PATH": os.pathsep.join(
                (
                    str(launchers),
                    os.path.join(REPO, "bin"),
                    str(binary),
                    os.path.dirname(sys.executable),
                    os.defpath,
                )
            ),
        }
        env.pop("ENSEMBLE_AGENT_SESSION", None)
        env.pop("ENSEMBLE_MANAGER", None)
        return env

    def daemon(self, env, first):
        roster = json.loads(Path(self.ensemble, "managers", "first", "roster.json").read_text())
        daemon_env = {
            **env,
            "ENSEMBLE_MANAGER": "first",
            "ENSEMBLE_HOLDER_PID": str(first.pid),
            "ENSEMBLE_HOLDER_ID": roster["holder"]["id"],
            "ENSEMBLE_HOLDER_GENERATION": str(roster["lease_gen"]),
        }
        daemon = subprocess.Popen(
            (
                sys.executable,
                "-I",
                os.path.join(REPO, "tests", "manager", "shared_codex.py"),
                "--fake-daemon",
                env["FAKE_DAEMON_SOCKET"],
            ),
            env=daemon_env,
            cwd=self.home,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(self.close, daemon)
        self.assertEqual(self.line(daemon), b"ready\n")
        return daemon

    def shared(self, *, different_cwd=False):
        env = self.environment()
        other = Path(self.home, "other") if different_cwd else Path(self.home)
        other.mkdir(exist_ok=True)
        first, second = self.start("first", env), self.start("second", env, cwd=str(other))
        daemon = self.daemon(env, first)
        clients = []
        for name, process in (("first", first), ("second", second)):
            self.send(process, "codex")
            client = json.loads(self.line(process))
            clients.append(client)
            self.assertEqual(
                self.saved(name),
                {"client": "codex", **client, "cwd": self.home if name == "first" else str(other)},
            )
            self.send(process, "turn")
            answer = json.loads(self.line(process))
            context = answer["hook"]["hookSpecificOutput"]["additionalContext"]
            self.assertIn(f"`startup` in manager `{name}`", context)
            self.assertEqual(answer["daemon"], daemon.pid)
            self.assertEqual(answer["shell_env"]["ENSEMBLE_MANAGER"], name)
        self.assertNotEqual(clients[0]["sid"], clients[1]["sid"])
        self.send(first, "clear")
        cleared = json.loads(self.line(first))
        self.assertNotEqual(cleared["sid"], clients[0]["sid"])
        self.assertEqual(self.saved("first")["sid"], cleared["sid"])
        self.assertEqual(self.saved("second")["sid"], clients[1]["sid"])
        self.send(first, "quit")
        deadline = time.monotonic() + 10
        while True:
            listing = subprocess.run(
                (os.path.join(REPO, "bin", "ensemble-crew"), "list"),
                env=env,
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            if "first\thold-only" in listing.stdout:
                break
            self.assertLess(time.monotonic(), deadline, listing.stdout)
            time.sleep(0.01)
        self.assertIsNone(daemon.poll())
        self.send(
            first, shlex.quote(os.path.join(REPO, "bin", "ensemble-crew")) + " resume-commander"
        )
        resumed = json.loads(self.line(first))
        self.assertEqual(resumed["sid"], cleared["sid"])
        self.assertNotEqual(resumed["pid"], cleared["pid"])
        self.assertEqual(self.saved("first")["pid"], resumed["pid"])
        self.send(first, "turn")
        answer = json.loads(self.line(first))
        self.assertEqual(answer["daemon"], daemon.pid)
        self.assertIn(
            "`resume` in manager `first`", answer["hook"]["hookSpecificOutput"]["additionalContext"]
        )

    def test_two_holds_and_resume_share_the_same_detached_daemon(self):
        self.shared()

    def test_another_cwd_is_preserved_on_the_shared_daemon(self):
        self.shared(different_cwd=True)

    def test_non_manager_and_worker_entry_preserve_arguments_status_and_settings(self):
        env = self.environment()
        settings = Path(self.home, ".codex", "config.toml")
        settings.parent.mkdir(exist_ok=True)
        settings.write_bytes(b"user_setting = true\n")
        args = ("--model", "model with spaces", "a'b$c", "--", "tail")
        for extra in ({}, {"ENSEMBLE_MANAGER": "unused", "ENSEMBLE_AGENT_SESSION": "1"}):
            with self.subTest(extra=extra):
                done = subprocess.run(
                    (str(Path(self.home, "launchers", "codex")), *args),
                    env={**env, **extra, "FAKE_PASSTHROUGH": "1"},
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
                self.assertEqual((done.returncode, done.stderr), (23, ""))
                self.assertEqual(json.loads(done.stdout), list(args))
                self.assertEqual(settings.read_bytes(), b"user_setting = true\n")

    def test_two_holds_cannot_claim_the_same_native_session(self):
        env = self.environment()
        first, second = self.start("first", env), self.start("second", env)
        self.daemon(env, first)
        sid = "00000000-0000-4000-8000-000000000001"
        command = f"codex resume {sid}; printf 'client-exit:%s\\n' \"$?\""
        self.send(first, command)
        self.send(second, command)
        answers = [self.line(process) for process in (first, second)]
        winners = [index for index, answer in enumerate(answers) if answer.startswith(b"{")]
        self.assertEqual(len(winners), 1, answers)
        winner = winners[0]
        self.assertRegex(answers[1 - winner].decode(), r"^client-exit:[1-9][0-9]*\n$")
        name = ("first", "second")[winner]
        other = ("second", "first")[winner]
        self.assertEqual(self.saved(name)["sid"], sid)
        self.assertFalse(Path(self.ensemble, "managers", other, "session.json").exists())
        self.send((first, second)[winner], "turn")
        answer = json.loads(self.line((first, second)[winner]))
        self.assertIn(
            f"`resume` in manager `{name}`",
            answer["hook"]["hookSpecificOutput"]["additionalContext"],
        )
