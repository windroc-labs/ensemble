"""Real anchored groups retain descendants and retire their control generation."""

import contextlib
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

from ensemble.utils import ProcessControl
from ensemble.utils.process_control import STATE, TIMEOUT
from tests.utils.case import Case
from tests.utils.sandbox import clean_env


class ProcessControlTest(Case):
    def test_requested_signals_wait_for_the_programs_handler_even_immediately_after_start(self):
        signals = (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)
        control, process = _start(
            self,
            [
                sys.executable,
                "-I",
                "-c",
                "import signal, time\n"
                "print(sorted(signal.pthread_sigmask(signal.SIG_BLOCK, ())), flush=True)\n"
                "time.sleep(0.3)\n"
                "def stopped(signum, frame):\n"
                "    print('stopped', signum, flush=True)\n"
                "    raise SystemExit(128 + signum)\n"
                "signal.signal(signal.SIGTERM, stopped)\n"
                "signal.pthread_sigmask(signal.SIG_UNBLOCK, (signal.SIGTERM,))\n"
                "time.sleep(60)\n",
            ],
            blocked_signals=signals,
        )
        control.terminate()
        out, err = process.communicate(timeout=10)
        self.assertEqual(
            (process.returncode, out.decode().splitlines(), err),
            (143, [str(sorted(signals)), f"stopped {signal.SIGTERM}"], b""),
        )

    def test_cancel_before_controller_start_prevents_a_late_program_launch(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder, "must-not-start")
            control = ProcessControl.prepare(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    "import pathlib, sys; pathlib.Path(sys.argv[1]).touch()",
                    str(marker),
                ],
                os.getcwd(),
            )
            command = control.command()
            control.cancel()
            result = subprocess.run(
                command, capture_output=True, timeout=10, check=False, env=clean_env()
            )
            self.assertEqual(result.returncode, 1)
            self.assertFalse(marker.exists())
            self.assertFalse(os.path.exists(control.path))

    def test_cancel_a_started_controller_ends_its_group_and_removes_its_directory(self):
        control, process = _start(self, [sys.executable, "-I", "-c", "import time; time.sleep(60)"])
        control.cancel()
        process.communicate(timeout=10)
        self.assertFalse(control.live())
        self.assertFalse(os.path.exists(control.path))

    def test_program_environment_is_explicit_with_only_requested_controller_values(self):
        for env, inherit_env, controller_env, expected in (
            ({"ONE": "a b", "TWO": ""}, (), None, {"ONE": "a b", "TWO": ""}),
            (
                {"ONE": "kept", "INHERITED": "caller", "ABSENT": "caller"},
                ("INHERITED", "ABSENT"),
                {"INHERITED": "controller", "UNRELATED": "excluded"},
                {"ONE": "kept", "INHERITED": "controller"},
            ),
        ):
            with self.subTest(inherit_env=inherit_env):
                _, process = _start(
                    self,
                    ["/usr/bin/env"],
                    controller_env=controller_env,
                    env=env,
                    inherit_env=inherit_env,
                )
                out, err = process.communicate(timeout=10)
                self.assertEqual(
                    dict(line.split("=", 1) for line in out.decode().splitlines()), expected
                )
                self.assertEqual((process.returncode, err), (0, b""))

    def test_exec_failure_is_reported_without_a_startup_timeout(self):
        control, _ = _start(self, ["/no-such-task366-program"], wait=False)
        started = time.monotonic()
        with self.assertRaises(FileNotFoundError):
            control.wait_started()
        self.assertLess(time.monotonic() - started, 2)

    def test_natural_exit_retires_the_generation_without_an_earlier_poll(self):
        control, process = _start(
            self, [sys.executable, "-I", "-c", "print('exact output'); raise SystemExit(3)"]
        )
        self.assertEqual(process.communicate(timeout=10), (b"exact output\n", b""))
        self.assertEqual(process.returncode, 3)
        control.kill()
        self.assertFalse(control.live())

    def test_stop_allows_cleanup_longer_than_five_seconds(self):
        with tempfile.TemporaryDirectory() as folder:
            ready = Path(folder, "ready")
            control, process = _start(
                self,
                [
                    sys.executable,
                    "-I",
                    "-c",
                    "import pathlib, signal, sys, time\n"
                    "def stop(signum, frame):\n"
                    "    time.sleep(6)\n"
                    "    print('cleanup complete', flush=True)\n"
                    "    raise SystemExit(0)\n"
                    "signal.signal(signal.SIGTERM, stop)\n"
                    "pathlib.Path(sys.argv[1]).touch()\n"
                    "time.sleep(60)\n",
                    str(ready),
                ],
            )
            _wait_for(self, ready.exists)
            process.send_signal(signal.SIGTERM)
            self.assertEqual(process.communicate(timeout=10), (b"cleanup complete\n", b""))
            self.assertEqual(process.returncode, 0)
            self.assertFalse(control.live())
            _wait_for(self, lambda: not os.path.exists(control.path))

    def test_the_controller_keeps_ownership_after_the_program_leaves_a_descendant(self):
        with tempfile.TemporaryDirectory() as folder:
            pids = Path(folder, "child")
            control, process = _start(
                self,
                [
                    sys.executable,
                    "-I",
                    "-c",
                    "import os, sys, time; child = os.fork(); "
                    "open(sys.argv[1], 'w').write(str(child)) if child else time.sleep(60)",
                    str(pids),
                ],
            )
            _wait_for(self, lambda: pids.exists() and pids.read_text())
            child = int(pids.read_text())
            time.sleep(0.2)
            self.assertEqual(process.wait(timeout=10), 0)
            self.assertTrue(control.live())
            os.kill(child, 0)
            control.kill()
            process.communicate(timeout=10)
            self.assertFalse(control.live())
            _wait_for(self, lambda: not os.path.exists(control.path))


class ControllerRetirementTest(Case):
    """A controller whose client never confirms retires once its group ends."""

    def launch(self, argv):
        control = ProcessControl.prepare(argv, os.getcwd(), root=self.home)
        process = subprocess.Popen(
            control.command(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env=clean_env(),
        )
        self.addCleanup(control.remove)
        self.addCleanup(process.communicate, timeout=10)

        def stop():
            with contextlib.suppress(OSError):
                control.kill()

        self.addCleanup(stop)
        deadline = time.monotonic() + 10
        while True:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                    connection.connect(os.path.join(control.path, "control"))
                    connection.sendall(b"start\n")
                    connection.recv(64)
                break
            except (FileNotFoundError, ConnectionRefusedError):
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.02)
        return control, process

    def assert_retires(self, control, within):
        deadline = time.monotonic() + within
        while os.path.exists(control.path) or _named(control.path):
            self.assertLess(time.monotonic(), deadline, f"controller {control.path} remains")
            time.sleep(0.1)

    def test_a_missing_caller_or_a_client_lost_before_started_leaves_no_controller(self):
        marker = Path(self.home, "must-not-start")
        began = time.monotonic()
        missing, waiting = _start(
            self,
            [
                sys.executable,
                "-I",
                "-c",
                "import pathlib, sys; pathlib.Path(sys.argv[1]).touch()",
                str(marker),
            ],
            wait=False,
        )
        _wait_for(
            self,
            lambda: (
                self.assertIsNone(waiting.poll())
                or os.path.exists(os.path.join(missing.path, "control"))
            ),
            within=5,
        )
        self.assertIsNone(waiting.poll())
        self.assertFalse(marker.exists())
        control, process = self.launch([sys.executable, "-I", "-c", "raise SystemExit(3)"])
        self.assertEqual(process.wait(timeout=10), 3)
        ended = time.monotonic()
        self.assertTrue(os.path.isdir(control.path))
        output = waiting.communicate(timeout=15)
        self.assertEqual(waiting.returncode, 1, output)
        self.assertLess(time.monotonic() - began, 15)
        self.assertGreaterEqual(time.monotonic() - began, 10)
        self.assertFalse(marker.exists())
        self.assertFalse(os.path.exists(missing.path))
        self.assert_retires(control, within=TIMEOUT + 5)
        self.assertGreaterEqual(time.monotonic() - ended, TIMEOUT - 1)

    def test_a_deleted_directory_retires_the_controller_once_its_group_ends(self):
        release = Path(self.home, "release")
        control, process = self.launch(
            [
                sys.executable,
                "-I",
                "-c",
                "import pathlib, sys, time\n"
                "while not pathlib.Path(sys.argv[1]).exists():\n"
                "    time.sleep(0.02)\n"
                "raise SystemExit(5)\n",
                str(release),
            ]
        )
        self.addCleanup(release.touch)
        _wait_for(self, lambda: os.path.exists(os.path.join(control.path, STATE)))
        shutil.rmtree(control.path)
        time.sleep(0.5)
        self.assertIsNone(process.poll())
        release.touch()
        self.assertEqual(process.wait(timeout=10), 5)
        self.assert_retires(control, within=2)


def _start(case, argv, *, wait=True, controller_env=None, **prepare):
    control = ProcessControl.prepare(argv, os.getcwd(), **prepare)
    process = subprocess.Popen(
        control.command(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env=clean_env(controller_env),
    )

    def cleanup():
        with contextlib.suppress(OSError):
            control.kill()
        process.communicate(timeout=10)
        control.remove()

    case.addCleanup(cleanup)
    if wait:
        control.wait_started()
    return control, process


def _wait_for(case, ready, within=10):
    deadline = time.monotonic() + within
    while not ready():
        case.assertLess(time.monotonic(), deadline)
        time.sleep(0.02)


def _named(path):
    listing = subprocess.run(
        ["ps", "-A", "-ww", "-o", "args="], capture_output=True, text=True, check=True
    )
    return any(path in line for line in listing.stdout.splitlines())


class ControllerAttachmentTest(Case):
    def test_attach_during_teardown_reports_ended_without_contacting_a_socket(self):
        control = ProcessControl.prepare(["program"], "/")
        self.addCleanup(control.remove)
        for removed in (False, True):
            with self.subTest(directory_removed=removed):
                if removed:
                    control.remove()
                attached = ProcessControl.attach(control.path)
                with mock.patch("socket.socket") as socket:
                    self.assertFalse(attached.running())
                    self.assertFalse(attached.live())
                    attached.kill()
                    socket.assert_not_called()
