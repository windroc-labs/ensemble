"""A guarded Docker worker over a fake `docker exec -i` that relays to the real guard: SIGKILL of the
harness or of the docker CLI ends the container-side client, and a container hook reaches the
channel as one WorkerHook.
"""

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from unittest import mock

from ensemble.session.engine.types import WorkerHook
from ensemble.session.harness import guard
from ensemble.session.hosts.docker import stub
from ensemble.session.hosts.docker.maintenance import Maintenance
from ensemble.session.hosts.docker.worker import DockerWorker
from tests.session import docker_exec_fixture, guard_fixture, live_tmux
from tests.session.case import Case, child_env


class Broker:
    def maintain(self, container, manifest, delivered, *, root):
        return None

    def close(self, container):
        return None


class GuardedDockerTest(Case):
    def setUp(self):
        self.run = os.path.join(self.home, "run")
        self.env = {**child_env(), "ENSEMBLE_FAKE_RUN": self.run}
        self.addCleanup(self.kill_tree)

    def kill_tree(self):
        for name in ("client", "child"):
            path = Path(self.home, name + ".pid")
            if path.exists():
                with contextlib.suppress(ProcessLookupError):
                    os.kill(int(path.read_text()), signal.SIGKILL)

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
                docker_exec_fixture.__file__,
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

    def exited(self):
        path = Path(self.run, "g1", "state.json")
        return path.exists() and json.loads(path.read_text()).get("state") == "exited"

    def test_sigkill_of_the_harness_ends_the_container_client(self):
        proc, _, client = self.started_harness()
        proc.kill()
        proc.wait(timeout=10)
        live_tmux.wait(lambda: not self.alive(client), what="container client ended")
        live_tmux.wait(self.exited, what="guard state exited")

    def test_sigkill_of_the_docker_cli_ends_the_container_client(self):
        _, value, client = self.started_harness()
        os.kill(value["docker"], signal.SIGKILL)
        live_tmux.wait(lambda: not self.alive(client), what="container client ended")
        live_tmux.wait(self.exited, what="guard state exited")

    def test_a_container_hook_reaches_the_channel_as_one_worker_hook(self):
        proc = subprocess.Popen(
            [
                sys.executable,
                "-I",
                docker_exec_fixture.__file__,
                "docker",
                *("exec", "-i", "c1", "python", "-I", "guard"),
                *("--hook-socket", "/run/ensemble/guard/g1/hook"),
                *("--lease", "/run/ensemble/guard/g1/lease", "--", *self.client()),
            ],
            env=self.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            start_new_session=True,
            bufsize=0,
        )
        assert proc.stdin is not None
        os.set_blocking(proc.stdin.fileno(), False)
        record = {"generation": "g1"}
        worker = DockerWorker(
            "w1",
            "g1",
            proc,
            state=self.home,
            events=None,
            hooks=None,
            remove=lambda: None,
            owner=os.open(os.path.join(self.home, "guard.lock"), os.O_RDWR | os.O_CREAT, 0o600),
            maintenance=lambda close: Maintenance(
                "w1", self.home, "g1", Broker(), report=lambda *_: None, root="/", close=close
            ),
            load=lambda: record,
            store=record.update,
            terminal=False,
        )
        self.addCleanup(worker.close)
        worker.await_start()
        socket_path = os.path.join(self.run, "g1", "hook")
        with mock.patch.dict(os.environ, {"ENSEMBLE_GUARD_HOOK_SOCKET": socket_path}):
            payload = b'{"sid": "s1", "transcript_path": "/Users/x/secret"}'
            self.assertEqual(stub.main(["hook", "/tui/w1", "Stop"], io.BytesIO(payload)), 0)
        found = []

        def hooked():
            found.extend(m for m in worker.read(0.1) if isinstance(m, WorkerHook))
            return found

        live_tmux.wait(hooked, what="the hook on the channel")
        [hook] = found
        self.assertEqual((hook.label, hook.payload), ("Stop", {"sid": "s1"}))
        self.assertRegex(hook.delivery_id, r"\A[0-9a-f]{32}\Z")
