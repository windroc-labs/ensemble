"""A guarded Docker worker over a fake `docker exec -i` that relays to the real guard: SIGKILL of the
harness or of the docker CLI ends the container-side client, a container hook reaches the channel as
one WorkerHook, and hooks journal the same records and events as on tmux.
"""

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ensemble.session.engine.types import (
    Activity,
    Blocked,
    ClientBound,
    TurnEnd,
    TurnStarted,
    WorkerHook,
)
from ensemble.session.harness import guard
from ensemble.session.hosts.docker import stub
from ensemble.session.hosts.docker.hooks import HookWorker
from ensemble.session.hosts.docker.maintenance import Maintenance
from ensemble.session.hosts.docker.worker import DockerWorker
from tests.session import docker_exec_fixture, guard_fixture, live_tmux
from tests.session.case import Case, child_env
from tests.session.client_adapters_support import DetailedTui
from tests.session.tui_hook_support import COMMAND, state, write_state

STEPS = (
    ("begin", {"session_id": "n1"}),
    ("work", {"session_id": "n1", "doing": "edit"}),
    ("wait", {"session_id": "n1", "why": "Bash"}),
    ("end", {"session_id": "n1", "last": "Done.", "tasks": []}),
)


class Broker:
    def maintain(self, container, manifest, delivered, *, root):
        return SimpleNamespace(next_check=None, deadline=None, results=[], notices=[])

    def close(self, container):
        return None


class GuardedDockerTest(Case):
    def setUp(self):
        self.run_dir = os.path.join(self.home, "run")
        self.env = {**child_env(), "ENSEMBLE_FAKE_RUN": self.run_dir}
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
        path = Path(self.run_dir, "g1", "state.json")
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
            maintenance=lambda close, report: Maintenance(
                "w1", self.home, "g1", Broker(), report=report, root="/", close=close
            ),
            report=lambda *_: None,
            load=lambda: record,
            store=record.update,
            terminal=False,
        )
        self.addCleanup(worker.close)
        worker.await_start()
        socket_path = os.path.join(self.run_dir, "g1", "hook")
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
        record = {"generation": "g1"}
        worker = DockerWorker(
            "w1",
            "g1",
            mock.Mock(),
            state=self.home,
            events=None,
            hooks=hooks,
            remove=lambda: None,
            owner=os.open(os.path.join(self.home, "guard.lock"), os.O_RDWR | os.O_CREAT, 0o600),
            maintenance=lambda close, report: Maintenance(
                "w1", self.home, "g1", Broker(), report=report, root="/", close=close
            ),
            report=lambda *_: None,
            load=lambda: record,
            store=record.update,
            start_reader=False,
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
