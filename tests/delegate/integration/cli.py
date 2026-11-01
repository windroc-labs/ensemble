"""Public CLI runs and answers as subprocesses over the fake claude, codex and opencode."""

import contextlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time

from ensemble.utils import SID_ENV, Record, Status
from tests.delegate.case import PY, REPO, Case, RunReads, child_env
from tests.delegate.plans import TEAM_FILES, write_team
from tests.delegate.workdir import input_first
from tests.session import harnesses, live_tmux
from tests.session.integration.clients.claude import fake_claude
from tests.session.integration.clients.codex import fake_codex
from tests.session.integration.clients.opencode import fake_opencode
from tests.utils.case import local_section
from tests.utils.sandbox import owned_directory, reap

RUN = (*PY, "-m", "ensemble.delegate.cli", "run")
SID = "0f8fad5b-d9cb-469f-a165-70867728950e"
WAIT = 30  # seconds
RESETS = 1760000000
DONE = {"status": "done", "title": "hello", "summary": "Hello.", "deliverable": "Hello.\n"}
ASKING = {
    "status": "needs_input",
    "title": "hello",
    "summary": "Asking.",
    "questions": ["Which greeting?"],
}
UNSAVED = {k: v for k, v in DONE.items() if k != "deliverable"}
UNSAVED_ERROR = "done with a local destination must carry the deliverable"
TEAMS = os.path.join(REPO, "work")


def owned_team(case, files=TEAM_FILES, *, root=TEAMS, prefix="team-") -> str:
    """A team dir of `files` under `root`, removed after `case`'s test."""
    team = owned_directory(case, root, prefix=prefix)
    os.chmod(team, 0o755)
    write_team(team, files)
    return team


class Runs(RunReads, Case):
    """A CLI run's records in `work` and the fake client's calls in `log`."""

    def records(self) -> list[dict]:
        return input_first(super().records())

    def calls(self) -> list[dict]:
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [json.loads(line) for line in f]

    def read(self, path: str) -> str:
        with open(path) as f:
            return f.read()

    def control(self, *arguments, stdin="", sid=None):
        if not hasattr(self, "resume_server"):
            self.resume_server = live_tmux.Server(self, self.home)
            self.resume_server.start("resume-manager")
        environment = {
            **self.env,
            "PATH": self.env["PATH"] + os.pathsep + self.resume_server.path,
            "TMUX_TMPDIR": self.resume_server.root,
        }
        environment.pop(SID_ENV, None)
        if sid is not None:
            environment[SID_ENV] = sid
        return subprocess.run(
            (*PY, "-m", "ensemble.delegate.cli", *arguments),
            cwd=self.proj,
            env=environment,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def completed(self):
        def ended():
            result = self.control("status", self.work)
            self.assertEqual(result.returncode, 0, result.stderr)
            return not Status.parse(result.stdout).alive

        live_tmux.wait(ended, timeout=WAIT, what="the resumed run to complete")

    def answer(self, steps, *, input="Hello.", wait=True, **scenario):
        self.scene(steps, **scenario)
        result = self.control("resume", self.work, "--input", input)
        if result.returncode == 0:
            self.assertEqual(Record.parse(result.stdout).sid, SID)
            if wait:
                self.completed()
        return result


class Integration(Runs):
    """ROLE over the fake `claude`: own HOME, CLI cwd `proj`, workdir `work`, agent cwd `agent`."""

    ROLE = "researcher"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(harnesses.end, self.home)
        self.root = os.path.realpath(tmp.name)
        self.proj, self.work, bin_dir = (
            os.path.join(self.root, d) for d in ("proj", "work", "bin")
        )
        for d in (self.proj, self.work, bin_dir):
            os.mkdir(d)
        self.out = os.path.join(self.work, "out.md")
        self.agent = os.path.join(self.work, "agent")
        self.scenario, self.log = (
            os.path.join(self.root, "scenario.json"),
            os.path.join(self.root, "log.jsonl"),
        )
        self.config = os.path.join(self.root, "config")
        self.env = {
            **child_env({}),
            "PATH": fake_claude.install(bin_dir),
            "HOME": self.home,
            "CLAUDE_CONFIG_DIR": self.config,
            "PYTHONUTF8": "1",
            "TZ": "UTC",
            fake_claude.ENV: self.scenario,
        }
        self.transcript = os.path.join(
            self.config, "projects", re.sub(r"[^A-Za-z0-9]", "-", self.agent), f"{SID}.jsonl"
        )

    def argv(self, *extra) -> list[str]:
        return [
            *RUN,
            "--role",
            self.ROLE,
            "--input",
            "Hello.",
            "--out",
            self.out,
            "--workdir",
            self.work,
            "--sid",
            SID,
            *extra,
        ]

    def scene(self, steps, **scenario) -> None:
        with open(self.scenario, "w") as f:
            json.dump({"steps": steps, **scenario, "log": self.log}, f)

    def drive(
        self, steps, *extra, cwd=None, argv=None, fixes=None, **scenario
    ) -> subprocess.CompletedProcess:
        if fixes is not None:
            scenario["resume"] = fixes
        self.scene(steps, **scenario)
        return subprocess.run(
            argv or self.argv(*extra),
            cwd=cwd or self.proj,
            env=self.env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def users(self) -> list:
        with open(self.transcript) as f:
            return [
                line["message"]["content"] for line in map(json.loads, f) if line["type"] == "user"
            ]

    def popen(self, *extra) -> subprocess.Popen:
        # A suite started in the background inherits SIGINT ignored; the CLI installs no handler of
        # its own.
        proc = subprocess.Popen(
            self.argv(*extra),
            cwd=self.proj,
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL),
        )
        self.addCleanup(self.reap, proc)
        return proc

    def reap(self, proc: subprocess.Popen) -> None:
        # Check before poll/reap, while the leader still pins its process-group id.
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
        reap(proc)
        proc.communicate(timeout=10)

    def until(self, proc: subprocess.Popen, done, what: str) -> None:
        deadline = time.monotonic() + WAIT
        while not done():
            if proc.poll() is not None or time.monotonic() > deadline:
                self.fail(f"the CLI never {what} (rc {proc.poll()})")
            time.sleep(0.05)


NATIVE = fake_codex.NATIVE
CODEX_END = {"out": {"type": "turn.completed"}}


def codex_said(*texts) -> list[dict]:
    """Fake steps: an agent message per text, then the turn end."""
    return [*map(fake_codex.message, texts), CODEX_END]


class CodexIntegration(Runs):
    """ROLE's TASK, --client codex over fake_codex; HOME outside the temp dirs."""

    ROLE, TASK = "researcher", "light-research"
    CLIENT = "codex"

    def setUp(self):
        home = tempfile.TemporaryDirectory(dir=os.environ.get("ENSEMBLE_TEST_TMP", REPO))
        tmp = tempfile.TemporaryDirectory()
        for d in (home, tmp):
            self.addCleanup(d.cleanup)
        self.home, root = os.path.realpath(home.name), os.path.realpath(tmp.name)
        self.addCleanup(harnesses.end, self.home)
        self.root = root
        self.work, bin_dir = os.path.join(root, "work"), os.path.join(root, "bin")
        self.proj = self.work
        for d in (self.work, bin_dir):
            os.mkdir(d)
        codex = os.path.join(bin_dir, "codex")
        with open(codex, "w") as f:
            f.write(
                f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(fake_codex.__file__)} "$@"\n'
            )
        os.chmod(codex, 0o755)
        self.out = os.path.join(self.work, "out.md")
        self.scenario, self.log = (
            os.path.join(root, "scenario.json"),
            os.path.join(root, "exec.jsonl"),
        )
        self.env = {
            **child_env({}),
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HOME": self.home,
            "PYTHONUTF8": "1",
            "CODEX_HOME": os.path.join(self.home, ".codex"),
            "FAKE_LOG": self.log,
            "FAKE_SCENARIO": self.scenario,
        }

    def scene(self, steps, **scenario):
        with open(self.scenario, "w", encoding="utf-8") as stream:
            json.dump({"steps": steps, **scenario}, stream)

    def drive(self, steps, *extra, **scenario) -> subprocess.CompletedProcess:
        self.scene(steps, **scenario)
        argv = [
            *RUN,
            "--client",
            self.CLIENT,
            "--role",
            self.ROLE,
            "--task",
            self.TASK,
            "--input",
            "Hello.",
            "--out",
            self.out,
            "--workdir",
            self.work,
            "--sid",
            SID,
            *extra,
        ]
        return subprocess.run(
            argv,
            cwd=self.work,
            env=self.env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def texts(self, kind: str, key: str = "text") -> list:
        return [e[key] for e in self.records() if e["kind"] == kind]


class OpenCodeIntegration(CodexIntegration):
    """ROLE's TASK, --client opencode over fake_opencode on PATH; HOME outside the temp dirs; cwd
    `proj`, trusted.
    """

    CLIENT = "opencode"

    def setUp(self):
        super().setUp()
        opencode = os.path.join(self.root, "bin", "opencode")
        with open(opencode, "w") as f:
            f.write(
                f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(fake_opencode.__file__)} "$@"\n'
            )
        os.chmod(opencode, 0o755)
        self.proj = os.path.join(self.root, "project")
        os.mkdir(self.proj)
        os.makedirs(os.path.join(self.home, ".ensemble"), exist_ok=True)
        with open(os.path.join(self.home, ".ensemble", "config.local.toml"), "w") as f:
            f.write(local_section(f"trusted_dirs = [{json.dumps(self.proj)}]\n", "delegate"))

    def drive(self, steps, *extra, **scenario) -> subprocess.CompletedProcess:
        return super().drive(steps, "--cwd", self.proj, *extra, **scenario)
