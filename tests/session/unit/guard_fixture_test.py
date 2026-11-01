"""The guard fixtures publish each file a test polls for whole: it exists only once complete."""

import io
import os
import signal
import sys
import tempfile
import time
from unittest import mock

from tests.session import docker_attach_fixture, guard_fixture
from tests.session.case import Case

FIXTURES = (guard_fixture, docker_attach_fixture)


class DockerMaintenanceTest(Case):
    def test_attach_fixture_constructs_and_ticks_maintenance(self):
        handle = docker_attach_fixture.docker_worker(self.home, mock.Mock(), start_reader=False)
        try:
            self.assertIsNone(handle.maintenance.tick(time.time()))
        finally:
            os.close(handle.owner)


class ReadinessTest(Case):
    def test_client_publishes_only_after_its_child_is_ready(self):
        for daemon in (False, True):
            with self.subTest(daemon=daemon), tempfile.TemporaryDirectory() as directory:
                process = mock.Mock()

                def ready():
                    self.assertFalse(os.path.exists(directory + "/client.pid"))
                    guard_fixture._publish(directory + "/child.pid", "4242")
                    return b"ready\n"

                process.stdout.readline.side_effect = ready
                with (
                    mock.patch.object(guard_fixture.subprocess, "Popen", return_value=process),
                    mock.patch.object(sys, "stdin", io.StringIO()),
                ):
                    guard_fixture.client(directory, daemon=daemon)
                self.assertTrue(os.path.exists(directory + "/child.pid"))
                with open(directory + "/child.pid") as stream:
                    self.assertEqual(stream.read(), "4242")
                with open(directory + "/client.pid") as stream:
                    self.assertEqual(stream.read(), str(os.getpid()))
                process.stdout.close.assert_called_once()

    def test_child_failure_does_not_publish_client_readiness(self):
        process = mock.Mock()
        process.stdout.readline.return_value = b""
        with (
            mock.patch.object(guard_fixture.subprocess, "Popen", return_value=process),
            mock.patch.object(sys, "stdin", io.StringIO()),
            self.assertRaisesRegex(RuntimeError, "child.*ready"),
        ):
            guard_fixture.client(self.home, daemon=False)
        self.assertFalse(os.path.exists(self.home + "/client.pid"))
        process.stdout.close.assert_called_once()

    def test_child_confirms_after_publishing_its_pid_and_installing_handlers(self):
        for daemon in (False, True):
            with self.subTest(daemon=daemon):
                output = io.StringIO()

                def waiting(_seconds, output=output):
                    self.assertEqual(output.getvalue(), "ready\n")
                    with open(self.home + "/child.pid") as stream:
                        self.assertEqual(stream.read(), str(os.getpid()))
                    handlers.assert_any_call(signal.SIGHUP, signal.SIG_IGN)
                    handlers.assert_any_call(signal.SIGTERM, signal.SIG_IGN)
                    raise RuntimeError("waiting")

                with (
                    mock.patch.object(os, "fork", return_value=0),
                    mock.patch.object(os, "setsid"),
                    mock.patch.object(signal, "signal") as handlers,
                    mock.patch.object(sys, "stdout", output),
                    mock.patch.object(guard_fixture.time, "sleep", side_effect=waiting),
                    self.assertRaisesRegex(RuntimeError, "waiting"),
                ):
                    guard_fixture.child(self.home, daemon=daemon)


def published(fixture, path: str, text: str) -> list[tuple[bool, str]]:
    """fixture._publish(path, text); at each os.replace, whether path existed and the source."""
    seen, replace = [], os.replace

    def recorded(source, target):
        with open(source) as stream:
            seen.append((os.path.exists(target), stream.read()))
        replace(source, target)

    with mock.patch("os.replace", side_effect=recorded):
        fixture._publish(path, text)
    return seen


class PublishTest(Case):
    def test_a_published_file_appears_complete_and_leaves_no_temp_file(self):
        for fixture in FIXTURES:
            with self.subTest(fixture.__name__), tempfile.TemporaryDirectory() as directory:
                path = os.path.join(directory, "client.pid")
                self.assertEqual(published(fixture, path, "4242"), [(False, "4242")])
                with open(path) as stream:
                    self.assertEqual(stream.read(), "4242")
                self.assertEqual(os.listdir(directory), ["client.pid"])

    def test_a_republished_file_is_replaced_whole(self):
        for fixture in FIXTURES:
            with self.subTest(fixture.__name__), tempfile.TemporaryDirectory() as directory:
                path = os.path.join(directory, "harness.json")
                fixture._publish(path, '{"guard": 1, "generation": "long"}')
                self.assertEqual(published(fixture, path, "{}"), [(True, "{}")])
                with open(path) as stream:
                    self.assertEqual(stream.read(), "{}")
                self.assertEqual(os.listdir(directory), ["harness.json"])
