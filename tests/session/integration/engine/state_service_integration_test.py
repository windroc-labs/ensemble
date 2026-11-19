"""The reader service as real processes: client processes reading through client_state share one
service and one reader per daemon, over a fake docker CLI (tests/session/state_docker.py).
"""

import contextlib
import os
import select
import signal
import subprocess
import sys
import time

from ensemble.session.engine import daemon, state_service
from ensemble.session.engine.types import Daemon, DaemonRecord
from tests.session import state_docker, state_reads
from tests.session.case import Case
from tests.session.state_volumes import started, stored

WAIT = 30


class StateServiceTest(Case):
    def setUp(self):
        self.daemon_id = "d" + os.path.basename(self.home)
        target = Daemon(
            "local", "unix:///fake.sock", remote=False, path=os.defpath, id=self.daemon_id
        )
        for session in ("s1", "v"):
            daemon.save_record(
                session, DaemonRecord(target, os.path.abspath(state_docker.__file__))
            )
            started(session)
            os.makedirs(stored(session, "app"))
            with open(os.path.join(stored(session, "app"), "t"), "w", encoding="utf-8") as f:
                f.write(session)
        self.addCleanup(self.end_services)

    def client(self) -> subprocess.Popen[str]:
        proc = subprocess.Popen(
            [sys.executable, "-P", "-s", os.path.abspath(state_reads.__file__)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self.addCleanup(self.end, proc)
        return proc

    def ask(self, proc: subprocess.Popen[str], session: str) -> str:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(f"{session} t\n")
        proc.stdin.flush()
        self.assertTrue(select.select([proc.stdout], [], [], WAIT)[0], "no answer")
        return proc.stdout.readline().strip()

    @staticmethod
    def end(proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.kill()
        proc.wait(WAIT)
        for stream in (proc.stdin, proc.stdout):
            if stream is not None:
                stream.close()

    def services(self) -> list[int]:
        listed = subprocess.run(
            ["ps", "-ww", "-eo", "pid=,args="], capture_output=True, text=True, check=True
        ).stdout
        marker = f"-m {state_service.__name__} {self.daemon_id} "
        return [int(line.split()[0]) for line in listed.splitlines() if marker in line]

    def runs(self) -> list[int]:
        path = os.path.join(self.home, "docker-runs")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [int(line) for line in f]

    def readers(self) -> list[int]:
        return [pid for pid in self.runs() if alive(pid)]

    def end_services(self) -> None:
        for pid in self.services():
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)

    def wait(self, check, what: str) -> None:
        deadline = time.monotonic() + WAIT
        while not check():
            self.assertLess(time.monotonic(), deadline, what)
            time.sleep(0.05)

    def test_two_client_processes_share_one_service_and_one_reader(self):
        first, second = self.client(), self.client()
        self.assertEqual(self.ask(first, "s1"), "s1")
        self.assertEqual(self.ask(second, "v"), "v")
        self.assertEqual(self.ask(first, "v"), "v")
        self.assertEqual(self.ask(second, "s1"), "s1")
        self.assertEqual(len(self.services()), 1)
        self.wait(lambda: len(self.readers()) == 1, "one reader")
        first.kill()
        first.wait(WAIT)
        self.assertEqual(self.ask(second, "s1"), "s1")
        self.assertEqual(len(self.services()), 1)
        self.assertEqual(len(self.readers()), 1)

    def test_a_killed_service_is_replaced_and_the_read_succeeds(self):
        proc = self.client()
        self.assertEqual(self.ask(proc, "s1"), "s1")
        (service,) = self.services()
        (reader,) = self.readers()
        os.kill(service, signal.SIGKILL)
        self.assertEqual(self.ask(proc, "s1"), "s1")
        self.wait(lambda: not alive(reader), "the old reader ends with its service")
        (replaced,) = self.services()
        self.assertNotEqual(replaced, service)

    def test_two_simultaneous_spawns_leave_one_service(self):
        path = state_service.socket_path(self.daemon_id)
        argv = [sys.executable, "-P", "-s", "-m", state_service.__name__, self.daemon_id, path]
        spawned = [subprocess.Popen(argv, stdin=subprocess.DEVNULL) for _ in range(2)]
        for proc in spawned:
            self.addCleanup(self.end, proc)
        self.wait(lambda: any(proc.poll() == 0 for proc in spawned), "the second one exits")
        self.assertEqual(len(self.services()), 1)
        self.assertEqual(self.ask(self.client(), "v"), "v")

    def test_a_terminated_service_removes_its_socket_and_its_reader(self):
        proc = self.client()
        self.assertEqual(self.ask(proc, "s1"), "s1")
        (service,) = self.services()
        (reader,) = self.readers()
        os.kill(service, signal.SIGTERM)
        self.wait(lambda: not self.services(), "the service exits")
        self.wait(lambda: not alive(reader), "its reader ends")
        self.assertFalse(os.path.lexists(state_service.socket_path(self.daemon_id)))


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    with contextlib.suppress(ChildProcessError):
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return False
    return True
