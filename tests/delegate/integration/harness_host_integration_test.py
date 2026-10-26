"""The harness's Host and import roots with real processes: the harness on a FakeHost
(tests/session/fakes/host.py, outside the three src roots), injected or configured, the agent on a
private tmux server (tests/session/live_tmux.py) running fake_claude; an extension client's detached
run, its package only on the caller's PYTHONPATH.
"""

import contextlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from ensemble.delegate.engine.types import Layout
from ensemble.session import SessionError
from ensemble.utils import Record
from tests.delegate.case import PY, PYTHONPATH, REPO, Case
from tests.delegate.integration.cli import DONE, RUN, SID
from tests.delegate.replies import block, completed_steps, mark
from tests.delegate.unit.runs import said
from tests.session import fake_client, live_tmux
from tests.session.fakes.host import FakeHost
from tests.session.integration.clients.claude import fake_claude

ROLE = "dummy-tester"
TUI = f"{ROLE}-{SID[:8]}"
HARNESS = f"{TUI}-drive"
START = mark("start", "echoing")
WAIT = 60  # seconds: a run
KEPT = f'implementation = "{__name__}:KeptLaunch"\n'
# HarnessDelegate configures the shared session classes: in a child, so no test does in-process.
INJECTED = """import sys
from ensemble.delegate import HarnessDelegate, Spec
from ensemble.delegate.engine.config import read_config
from ensemble.session import Session
role, work, sid, events, cwd = sys.argv[1:]
Session.configure_from(read_config())
spec = Spec("Hello.", work, mode="tui", sid=sid, events=events, cwd=cwd)
print(HarnessDelegate(role, spec, harness_host=Session.host("fake")).detach().to_line())
"""


class KeptLaunch(FakeHost):
    """A FakeHost keeping each detach's launch environment beside its sessions."""

    def detach(self, name, launch, **kw):
        os.makedirs(self.root(), exist_ok=True)
        with open(os.path.join(self.root(), f"{name}.env.json"), "w", encoding="utf-8") as file:
            json.dump(dict(launch.env), file)
        return super().detach(name, launch, **kw)


class HarnessHosts(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.realpath(tmp.name)
        self.server = live_tmux.Server(self, self.home)
        self.server.start("mgr")
        self.addCleanup(self.end_fake_harness)  # before tmp's cleanup: it writes into tmp
        self.proj, self.work = (os.path.join(self.dir, d) for d in ("proj", "work"))
        for directory in (self.proj, self.work):
            os.mkdir(directory)
        self.out = os.path.join(self.work, "out.md")
        self.events, self.scenario = (
            os.path.join(self.dir, f) for f in ("events", "scenario.json")
        )

    def end_fake_harness(self) -> None:
        with contextlib.suppress(SessionError):
            FakeHost("fake").attach(HARNESS).kill()

    def configure(self, text: str) -> None:
        with open(os.path.join(self.ensemble, "config.local.toml"), "w", encoding="utf-8") as file:
            file.write(text)

    def steps(self, steps, turns=()) -> None:
        with open(self.scenario, "w", encoding="utf-8") as file:
            json.dump({"steps": steps, "turns": list(turns)}, file)

    def env(self, pythonpath: str = PYTHONPATH) -> dict[str, str]:
        return self.server.env(
            PYTHONPATH=pythonpath,
            CLAUDE_CONFIG_DIR=os.path.join(self.dir, "config"),
            **{fake_claude.ENV: self.scenario},
        )

    def detach(self, *extra: str, pythonpath: str = PYTHONPATH) -> Record:
        argv = [*RUN, "--role", ROLE, "--input", "Hello.", "--out", self.out]
        argv += ["--workdir", self.work, "--sid", SID, "--detach", "--events", self.events]
        res = subprocess.run(
            [*argv, *extra],
            cwd=self.proj,
            env=self.env(pythonpath),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=WAIT,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        return Record.parse(res.stdout)

    def live(self, name: str) -> bool:
        return self.server.tmux("has-session", "-t", f"={name}").returncode == 0

    def outcome(self, status: str = "done") -> None:
        """The harness's outcome, then its exit: its host logs `exited` into self.dir after it."""

        def logged(event):
            self.server.tmux("run-shell", "-b", "true")
            return any(
                line.endswith(f" {HARNESS} {event}") for line in live_tmux.events(self.events)
            )

        for event in (f"outcome {status}", "exited 0"):
            live_tmux.wait(lambda e=event: logged(e), WAIT, what=f"`{HARNESS} {event}`")

    def metadata(self) -> dict:
        with open(os.path.join(Layout(self.work).run, "delegate.json"), encoding="utf-8") as file:
            return json.load(file)

    def test_an_injected_fake_host_runs_the_harness_while_tmux_runs_the_agent(self):
        self.configure(f"[session.hosts.fake]\n{KEPT}")
        self.steps([START], turns=[[block(DONE)]])
        res = subprocess.run(
            [*PY, "-c", INJECTED, ROLE, self.work, SID, self.events, self.proj],
            cwd=self.proj,
            env=self.env(os.pathsep.join((PYTHONPATH, REPO))),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=WAIT,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        record = Record.parse(res.stdout)
        self.assertEqual(record.sessions, (HARNESS, TUI))
        self.assertTrue(self.live(TUI))
        self.assertFalse(self.live(HARNESS))
        self.assertTrue(
            os.path.isfile(os.path.join(self.ensemble, "fake-host", f"{HARNESS}.env.json"))
        )
        self.outcome()
        self.assertEqual(
            (self.metadata()["host"], self.metadata()["harness_host"]), ("tmux", "fake")
        )

    def test_a_configured_fake_harness_host_imports_and_routes_the_harness(self):
        self.configure(f'[session.hosts.fake]\n{KEPT}[delegate.delegates]\nharness_host = "fake"\n')
        self.steps([START, block(DONE)])
        record = self.detach("--mode", "headless", pythonpath=os.pathsep.join((PYTHONPATH, REPO)))
        self.assertEqual(record.sessions, (HARNESS,))
        with open(
            os.path.join(self.ensemble, "fake-host", f"{HARNESS}.env.json"), encoding="utf-8"
        ) as file:
            roots = json.load(file)["PYTHONPATH"].split(os.pathsep)
        self.assertIn(REPO, roots)
        self.outcome()
        with open(self.out, encoding="utf-8") as file:
            self.assertEqual(file.read(), DONE["deliverable"])
        self.assertEqual(
            (self.metadata()["host"], self.metadata()["harness_host"]), ("tmux", "fake")
        )

    def test_a_detached_extension_client_reaches_its_outcome(self):
        extensions = os.path.join(self.dir, "extensions")
        shutil.copytree(
            os.path.dirname(fake_client.__file__),
            os.path.join(extensions, "extension_client"),
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        with open(self.scenario, "w", encoding="utf-8") as file:
            json.dump(completed_steps([said(START, block(DONE))]), file)
        self.configure(
            "[session.clients.extension]\n"
            'implementation = "extension_client:FakeClient"\n'
            f"scenario = {json.dumps(self.scenario)}\n"
            f"log = {json.dumps(os.path.join(self.dir, 'fake.log'))}\n"
        )
        self.detach(
            "--mode",
            "headless",
            "--client",
            "extension",
            pythonpath=os.pathsep.join((PYTHONPATH, extensions)),
        )
        self.outcome()
        with open(self.out, encoding="utf-8") as file:
            self.assertEqual(file.read(), DONE["deliverable"])


if __name__ == "__main__":
    unittest.main()
