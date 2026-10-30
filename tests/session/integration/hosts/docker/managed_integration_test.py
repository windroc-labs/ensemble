"""A guarded Docker worker whose guard is its fake container's main process, over a fake `docker
attach` that, like Docker's, never closes the guard's stdin; its hooks journal as on tmux.
"""

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

from ensemble.session.engine.types import (
    Access,
    Activity,
    Blocked,
    ClientBound,
    SessionError,
    Spec,
    TurnEnd,
    TurnStarted,
    WorkerHook,
)
from ensemble.session.harness import guard
from ensemble.session.hosts.docker import stub
from ensemble.session.hosts.docker.hooks import HookWorker
from ensemble.session.hosts.docker.worker import DockerWorker
from ensemble.session.modes.headless import HeadlessMode
from tests.session import docker_attach_fixture, guard_fixture, live_tmux
from tests.session.case import Case, child_env
from tests.session.client_adapters_support import DetailedTui
from tests.session.fakes.terminal import MemoryHost
from tests.session.headless_support import SID, StubClient
from tests.session.tui_hook_support import COMMAND, state, write_state

STEPS = (
    ("begin", {"session_id": "n1"}),
    ("work", {"session_id": "n1", "doing": "edit"}),
    ("wait", {"session_id": "n1", "why": "Bash"}),
    ("end", {"session_id": "n1", "last": "Done.", "tasks": []}),
)
REAPED = 10.0  # s from a kill to the client's and the container's end (FR-4)


class GuardedDockerTest(Case):
    def setUp(self):
        self.run_dir = os.path.join(self.home, "run")
        self.env = {**child_env(), "ENSEMBLE_FAKE_RUN": self.run_dir}
        self.addCleanup(self.kill_tree)

    def kill_tree(self):
        pids = [
            int(path.read_text())
            for name in ("client", "child")
            if (path := Path(self.home, name + ".pid")).exists()
        ]
        if (found := Path(self.home, "harness.json")).exists():
            pids += [json.loads(found.read_text())[key] for key in ("guard", "container", "docker")]
        for pid in pids:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)

    def alive(self, pid):
        info = guard.process_info(pid)
        return info is not None and not info[2]

    def client(self):
        return [sys.executable, "-I", guard_fixture.__file__, "client", self.home]

    def end(self, proc):
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
        if proc.stderr is not None:
            proc.stderr.close()

    def started_harness(self):
        proc = subprocess.Popen(
            [
                sys.executable,
                "-P",
                "-s",
                docker_attach_fixture.__file__,
                "harness",
                self.home,
                *self.client(),
            ],
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self.addCleanup(self.end, proc)

        def started():
            if proc.poll() is not None:
                assert proc.stderr is not None
                self.fail(f"harness exited {proc.returncode}: {proc.stderr.read().decode()}")
            return (
                Path(self.home, "harness.json").exists() and Path(self.home, "client.pid").exists()
            )

        live_tmux.wait(started, what="guarded client start")
        value = json.loads(Path(self.home, "harness.json").read_text())
        return proc, value, int(Path(self.home, "client.pid").read_text())

    def reaped(self, value, client, *, guard_exits=True):
        """The client and the container end within REAPED s; the guard confirmed its exit."""
        began = time.monotonic()
        live_tmux.wait(lambda: not self.alive(client), what="container client ended")
        live_tmux.wait(lambda: not self.alive(value["container"]), what="container ended")
        self.assertLess(time.monotonic() - began, REAPED)
        if guard_exits:
            path = Path(self.run_dir, docker_attach_fixture.GEN, "state.json")
            self.assertEqual(json.loads(path.read_text())["state"], "exited")

    def test_sigkill_of_the_harness_ends_the_container_and_its_client(self):
        proc, value, client = self.started_harness()
        proc.kill()
        proc.wait(timeout=10)
        self.reaped(value, client)

    def test_sigkill_of_the_attach_ends_the_container_and_its_client(self):
        _, value, client = self.started_harness()
        os.kill(value["docker"], signal.SIGKILL)
        self.reaped(value, client)

    def test_sigkill_of_the_guard_ends_the_container_and_its_client(self):
        _, value, client = self.started_harness()
        os.kill(value["guard"], signal.SIGKILL)
        self.reaped(value, client, guard_exits=False)

    def test_sigkill_of_harness_and_guard_ends_the_container_and_its_client(self):
        proc, value, client = self.started_harness()
        proc.kill()
        os.kill(value["guard"], signal.SIGKILL)
        self.reaped(value, client, guard_exits=False)

    def test_an_idle_worker_outlives_the_heartbeat_limit(self):
        _, value, client = self.started_harness()
        time.sleep(guard.HEARTBEAT + 2)
        self.assertTrue(self.alive(client) and self.alive(value["guard"]))

    def worker(self, remove=lambda: None) -> DockerWorker:
        """A started DockerWorker over a fake container and attach, its guard running self.client()."""
        with mock.patch.dict(os.environ, self.env):
            container, _ = docker_attach_fixture.start_container()
            self.addCleanup(self.end, container)
            proc = docker_attach_fixture.attach()
            self.addCleanup(self.end, proc)
            worker = docker_attach_fixture.worker(self.home, proc, self.client(), remove=remove)
        self.addCleanup(worker.close)
        return worker

    def test_a_headless_worker_whose_removal_fails_is_removed_by_the_next_close(self):
        removals = []

        def remove():
            removals.append(len(removals))
            if len(removals) == 1:
                raise SessionError("docker rm: daemon busy")

        test = self

        class Host(MemoryHost):
            def headless_worker(self, name, launch, metadata):
                return test.worker(remove)

        mode = HeadlessMode.start(
            Spec(workdir=self.home, access=Access(cwd=self.home), sid=SID, client="stub"),
            StubClient(),
            registry=self.session_cls._config().registry,
            record=lambda _line: None,
            host=Host(),
            generation="f" * 32,
        )
        pid = Path(self.home, "client.pid")
        live_tmux.wait(lambda: pid.exists() and pid.read_text(), what="guarded client start")
        client = int(pid.read_text())
        with self.assertRaisesRegex(SessionError, "daemon busy"):
            mode.stop()
        self.assertEqual(removals, [0])
        mode.stop()
        self.assertEqual(removals, [0, 1])
        live_tmux.wait(lambda: not self.alive(client), what="container client ended")

    def test_a_container_hook_reaches_the_channel_as_one_worker_hook(self):
        worker = self.worker()
        socket_path = os.path.join(self.run_dir, docker_attach_fixture.GEN, "hook")
        payload = b'{"sid": "s1", "transcript_path": "/Users/x/secret"}'
        oversize = json.dumps({"sid": "s1", "x": "y" * guard.HOOK_MAX}).encode()
        with (
            mock.patch.dict(os.environ, {"ENSEMBLE_GUARD_HOOK_SOCKET": socket_path}),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            # a Codex tui's turn start, a forged label and an oversize payload (FR-36, its stub
            # retrying for stub.WAIT until it gives up), then its turn end
            for label, body in (
                ("UserPromptSubmit", payload),
                ("Stop/../x", payload),
                ("Stop", oversize),
                ("Stop", payload),
            ):
                self.assertEqual(stub.main(["hook", "/tui/w1", label], io.BytesIO(body)), 0)
        found = []

        def hooked():
            found.extend(m for m in worker.read(0.1) if isinstance(m, WorkerHook))
            return len(found) >= 2

        live_tmux.wait(hooked, what="the hooks on the channel")
        self.assertEqual(
            [(hook.label, hook.payload) for hook in found],
            [("UserPromptSubmit", {"sid": "s1"}), ("Stop", {"sid": "s1"})],
        )
        self.assertRegex(found[0].delivery_id, r"\A[0-9a-f]{32}\Z")
        with open(os.path.join(self.home, "rejected.jsonl")) as f:
            self.assertEqual([json.loads(line)["reason"] for line in f], ["label"])


class SameEventsTest(Case):
    def folder(self, name):
        folder = os.path.join(self.home, name)
        os.mkdir(folder)
        saved = state(self.home, None)
        saved["words"] = {"begin": "working", "work": "working", "wait": "blocked", "end": "done"}
        write_state(folder, saved)
        return folder

    def journal(self, folder):
        with open(os.path.join(folder, "hooks.jsonl")) as f:
            records = [json.loads(line) for line in f]
        found = [(r["label"], r["payload"]) for r in records]
        run = DetailedTui()
        return found, [
            event for label, payload in found for event in run.hook_events(label, payload)
        ]

    def test_guarded_and_tmux_hook_paths_journal_the_same_records_and_events(self):
        local, guarded = self.folder("tmux"), self.folder("guarded")
        for label, payload in STEPS:
            subprocess.run(
                [COMMAND, "hook", local, label],
                input=json.dumps(payload).encode(),
                env=child_env(),
                check=True,
                capture_output=True,
            )
        self.enterContext(mock.patch.dict(os.environ, child_env(), clear=True))
        hooks = HookWorker([COMMAND, "hook", "--serve", guarded])
        self.addCleanup(hooks.stop)
        worker = docker_attach_fixture.docker_worker(
            self.home, mock.Mock(), hooks=hooks, start_reader=False
        )
        worker.accept(b"s", b'{"pid": 1}')
        frames = [
            json.dumps({"label": label, "payload": payload, "delivery_id": f"{n:032x}"}).encode()
            for n, (label, payload) in enumerate(STEPS)
        ]
        for frame in (*frames[:2], frames[1], *frames[2:]):
            worker.accept(b"h", frame)
        worker.begin()
        with worker.condition:
            self.assertTrue(worker.condition.wait_for(lambda: not worker._hooks_pending(), 30))
        self.assertEqual(self.journal(guarded), self.journal(local))
        self.assertEqual(
            self.journal(guarded)[1],
            [
                ClientBound("n1"),
                TurnStarted(),
                Activity("edit"),
                Blocked("Bash"),
                TurnEnd("Done.", 0),
            ],
        )
