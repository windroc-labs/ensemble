"""Session CLI fixtures: private HOME, tmux and fake claude."""

import json
import os
import shlex
import subprocess
import sys

from ensemble.session.harness import guard
from ensemble.session.harness.channel import Channel
from ensemble.session.hosts.base import stored_record, worker_state
from ensemble.session.hosts.tmux import managed
from ensemble.utils import Record, Status
from tests.session import live_tmux
from tests.session.case import REPO, ROOTS, Case
from tests.session.integration.clients.claude import fake_claude
from tests.session.live_tmux import MANAGER, logged
from tests.utils.sandbox import scratch

COMMAND = os.path.join(REPO, "bin", "ensemble-session")
TIMEOUT = 60
FAKE_HOST = "tests.session.fakes.host:FakeHost"
CLAUDE_HOOKS = {
    "SessionStart",
    "Stop",
    "StopFailure",
    "PermissionRequest",
    "Notification",
    "PostToolUse",
    "UserPromptSubmit",
}
POLICY = ("--allowedTools", "--add-dir", "--dangerously-skip-permissions")


def guard_command(name: str) -> str:
    directory = managed.location("tmux", name)
    with open(os.path.join(directory, "current.json"), encoding="utf-8") as stream:
        generation = json.load(stream)["generation"]
    return shlex.join(
        (
            sys.executable,
            "-I",
            guard.__file__,
            "--attach",
            os.path.join(directory, generation, "display"),
        )
    )


class CliCase(live_tmux.Calls, Case):
    ATTACH = True

    def assert_early_exit(self, result, name: str, code: int) -> list[str]:
        """A failed start's protocol; its screen is optional and may include an opening command."""
        self.assertEqual((result.returncode, result.stdout), (1, ""), result.stderr)
        lines = result.stderr.splitlines()
        self.assertGreaterEqual(len(lines), 2, result.stderr)
        self.assertTrue(lines[0].startswith(f"session {name}: "), result.stderr)
        command = shlex.split(lines[0].removeprefix(f"session {name}: "))
        self.assertEqual(len(command), 5, result.stderr)
        self.assertEqual(
            (command[1], os.path.basename(command[2]), command[3]), ("-I", "guard.py", "--attach")
        )
        self.assertEqual(os.path.basename(command[4]), "display")
        head = f"ensemble-session: {name}: claude exited {code} while starting"
        self.assertEqual(lines[1], head + ("; its last lines:" if lines[2:] else ""), result.stderr)
        return lines[2:]

    def setUp(self):
        self.root = scratch(self, os.environ.get("ENSEMBLE_TEST_TMP"))
        self.cwd = os.path.join(self.root, "work")
        os.mkdir(self.cwd)
        self.events = os.path.join(self.root, "events")
        self.local_config("")
        self.server = live_tmux.Server(self, self.home)
        self.server.start(MANAGER)
        if self.ATTACH:
            self.server.attach(MANAGER)

    def owner(self, name: str) -> Channel:
        """`name`'s session.json owner."""
        path = os.path.join(self.ensemble, "tui", name, "session.json")
        with open(path, encoding="utf-8") as f:
            return Channel(json.load(f)["owner"])

    def settled(self, channel: Channel) -> dict:
        """Its harness.json once draining, exited or failed."""
        return live_tmux.wait(
            lambda: (
                (found := channel.status()).get("state") in ("draining", "exited", "failed")
                and found
            ),
            timeout=40,
            what=f"harness {channel.directory} to leave running",
        )

    def session(
        self,
        *argv: str,
        stdin: str | None = None,
        inside: str | None = MANAGER,
        cwd: str | None = None,
        module: bool = False,
        **extra: str | None,
    ) -> subprocess.CompletedProcess:
        env = self.env(inside=inside, **extra)
        if module:
            env["PYTHONPATH"] = os.pathsep.join((*ROOTS, REPO))
            command = [sys.executable, "-P", "-m", "ensemble.session.cli", *argv]
        else:
            command = [COMMAND, *argv]
        with self.poking():
            return subprocess.run(
                command,
                env=env,
                cwd=cwd or self.cwd,
                input="" if stdin is None else stdin,
                capture_output=True,
                text=True,
                timeout=TIMEOUT,
            )

    def calls(self, name: str) -> list[dict]:
        return logged(self.log(name))

    def commands(self, name: str) -> list[dict]:
        return logged(self.log(name, "commands"))

    def start(
        self,
        name: str,
        *args: str,
        steps=None,
        turns=(),
        prompt: str = "hi",
        dialog: str | None = None,
        **extra,
    ) -> subprocess.CompletedProcess:
        """A fake claude worker, its events self.events."""
        scene = self.scene(name, steps, turns, **({} if dialog is None else {"dialog": dialog}))
        env = {"ENSEMBLE_EVENTS": self.events, **scene, **extra}
        return self.session(
            "start",
            name,
            "--client",
            "claude",
            "--cwd",
            self.cwd,
            "--prompt",
            prompt,
            *args,
            **env,
        )

    def dir_record(self, name: str) -> Record | None:
        """`name`'s stored Record for its current generation."""
        found = worker_state(name)
        generation = None if found is None else found.generation
        return None if generation is None else stored_record(name, generation)

    def status(self, *names: str, stdin: str | None = None, **kw) -> list[Status]:
        result = self.ok(self.session("status", *names, stdin=stdin, **kw))
        return [Status.parse(line) for line in result.stdout.splitlines()]

    def local_config(self, text: str) -> None:
        """`text`, plus Claude account `test` in ~/.claude unless `text` registers its own."""
        if "session.clients.claude.accounts" not in text:
            text += fake_claude.account(os.path.join(self.home, ".claude"))
        with open(os.path.join(self.ensemble, "config.local.toml"), "w", encoding="utf-8") as f:
            f.write(text)

    def fake_host(self, default: str = "tmux") -> str:
        """Adds Host `fake` (not attachable); its root."""
        root = os.path.join(self.root, "fake-host")
        self.local_config(
            f'[session.hosts]\ndefault = "{default}"\n[session.hosts.fake]\n'
            f'implementation = "{FAKE_HOST}"\nroot = "{root}"\n'
        )
        return root


ENV_DUMP = (
    "import json, os, sys; open(sys.argv[1], 'a')"
    ".write(json.dumps({'argv': sys.argv[2:], 'env': dict(os.environ)}) + '\\n')"
)


class WorkerCase(CliCase):
    def dumping(self) -> str:
        """A `claude` logging argv and env before the fake; its log."""
        path = os.path.join(self.root, "claude.env.jsonl")
        q = shlex.quote
        with open(os.path.join(self.server.bin, "claude"), "w", encoding="utf-8") as f:
            f.write(
                f'#!/bin/sh\n{q(sys.executable)} -I -c {q(ENV_DUMP)} {q(path)} "$@"\n'
                f'exec {q(sys.executable)} -I {q(fake_claude.__file__)} "$@"\n'
            )
        return path

    def claude_argv(self, argv: list[str], sid: str, name: str, *tail: str) -> None:
        """A worker's argv on a non-isolated Host, then `tail`."""
        self.assertEqual(
            argv[:9] + argv[10:],
            [
                "--session-id",
                sid,
                "--name",
                name,
                "--permission-mode",
                "auto",
                "--setting-sources",
                "user,project,local",
                "--settings",
                *tail,
            ],
        )
        settings = json.loads(argv[9])
        self.assertEqual(set(settings), {"enabledPlugins", "hooks"})
        self.assertEqual(settings["enabledPlugins"], {"ensemble@windroc": False})
        self.assertEqual(set(settings["hooks"]), CLAUDE_HOOKS)
        self.assertFalse([word for word in argv if word.startswith(POLICY)], argv)

    def harness_files(self, name: str) -> str:
        """Its harness's log, status and events, for a failure message."""
        try:
            with open(
                os.path.join(self.ensemble, "tui", name, "session.json"), encoding="utf-8"
            ) as f:
                owner = json.load(f).get("owner")
        except (OSError, ValueError):
            return f"{name}: no session.json"
        found = []
        for file in ("harness.log", "harness.json", "events.jsonl"):
            try:
                with open(os.path.join(owner, file), encoding="utf-8", errors="replace") as f:
                    found.append(f"--- {file}\n{f.read()[-4000:]}")
            except (OSError, TypeError):
                found.append(f"--- {file}: none")
        return "\n".join(found)

    def live(self, name: str) -> bool:
        """Whether worker `name`'s guard runs."""
        return live_tmux.running(name)

    def pane(self, name: str, fmt: str) -> str:
        """fmt of worker `name`'s newest live worker pane."""
        pane = self.server.pane_of(name)
        self.assertTrue(pane, f"{name}: no worker pane")
        res = self.server.tmux("display-message", "-p", "-t", pane, fmt)
        self.assertEqual(res.returncode, 0, res.stderr)
        return res.stdout.removesuffix("\n")

    def words(self) -> list[list[str]]:
        return [line.split(" ")[1:] for line in live_tmux.events(self.events)]
