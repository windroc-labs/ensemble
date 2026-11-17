"""hosts/docker/daemon.py: the capped run, over fake processes; the Docker target and its docker command line."""

import contextlib
import os
import re
import threading
from unittest import mock

from ensemble.session.engine.types import ConfigError, SessionError
from ensemble.session.hosts.docker import daemon
from ensemble.session.hosts.docker.daemon import OUT_MAX, Overflow, capped
from tests.session.case import Case
from tests.session.unit.hosts.docker.support import FakePopen, FakeProc, ssh_wrapper
from tests.utils.process_fixtures import DEVNULL, PIPE, TimeoutExpired


class CappedTest(Case):
    def test_returns_text_and_feeds_input_from_a_file(self):
        popen = FakePopen((b"out\xff", b"err", 3))
        with mock.patch("subprocess.Popen", popen):
            res = capped(["x"], {"A": "1"}, input="hi")
        self.assertEqual((res.returncode, res.stdout, res.stderr), (3, "out�", "err"))
        ((argv, kw, given),) = popen.calls
        self.assertEqual(
            (argv, kw["env"], given, kw["stdout"], kw["stderr"]),
            (["x"], {"A": "1"}, b"hi", PIPE, PIPE),
        )
        with mock.patch("subprocess.Popen", FakePopen((b"", b"", 0))) as popen:
            capped(["x"], {})
        self.assertEqual(popen.calls[0][2], DEVNULL)

    def test_a_silent_process_times_out_and_is_killed(self):
        proc = FakeProc(["x"], hang=True)
        with mock.patch("subprocess.Popen", return_value=proc), self.assertRaises(TimeoutExpired):
            capped(["x"], {}, timeout=0.1)
        self.assertTrue(proc.killed)

    def test_output_over_the_cap_is_killed(self):
        proc = FakeProc(["x"], hang=True)
        write = proc.fds[0]

        def flood():
            with contextlib.suppress(OSError):
                os.write(write, b"x" * (OUT_MAX + 1))

        thread = threading.Thread(target=flood)
        thread.start()
        with (
            mock.patch("subprocess.Popen", return_value=proc),
            self.assertRaisesRegex(Overflow, "output over"),
        ):
            capped(["x"], {}, timeout=30)
        thread.join()
        self.assertTrue(proc.killed)


class TargetTest(Case):
    def setUp(self):
        self.account = os.path.realpath(self.home)
        patcher = mock.patch("pwd.getpwuid", return_value=mock.Mock(pw_dir=self.account))
        patcher.start()
        self.addCleanup(patcher.stop)
        wrapper = ssh_wrapper()
        wrapper.__enter__()
        self.addCleanup(wrapper.__exit__, None, None, None)

    def target(self, content):
        path = os.path.join(self.account, daemon.TARGET_FILE)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        return path

    def test_no_target_file_is_the_local_docker_desktop(self):
        local = f"unix://{self.account}/.docker/run/docker.sock"
        self.assertEqual(daemon.endpoint(), local)
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin"}):
            argv, env = daemon.docker_call("/bin/docker", "ps")
        self.assertEqual((argv, env["PATH"]), (["/bin/docker", "--host", local, "ps"], "/usr/bin"))

    def test_remote_target_ignores_hostile_docker_environment(self):
        self.target('{"host": "ssh://u@pc:2222"}')
        hostile = {
            "DOCKER_HOST": "tcp://hostile:1234",
            "DOCKER_CONTEXT": "hostile",
            "DOCKER_CONFIG": self.home,
            "PATH": "/usr/bin",
        }
        with mock.patch.dict(os.environ, hostile):
            argv, env = daemon.docker_call("/bin/docker", "ps")
        self.assertEqual(argv, ["/bin/docker", "--host", "ssh://u@pc:2222", "ps"])
        self.assertFalse(set(daemon.CLEARED) & set(env))
        self.assertEqual(env["PATH"], f"{daemon.SSH_DIR}{os.pathsep}/usr/bin")
        self.assertEqual(daemon.endpoint(), "ssh://u@pc:2222")

    def test_invalid_target_file_is_a_config_error(self):
        for content in (
            "",
            "[]",
            "null",
            "{}",
            '{"host": 1}',
            '{"HOST": "ssh://pc"}',
            '{"host": "ssh://pc", "user": "u"}',
            '{"host": "ssh://pc"} {}',
            '{"host": "ssh://pc\\n"}',
            '{"host": "ssh://-oProxyCommand=x"}',
            '{"host": "tcp://pc:2375"}',
            '{"host": "ssh://pc/var/run/x"}',
            '{"host": "ssh://pc:0"}',
            '{"host": "ssh://pc:65536"}',
            "{" + " " * (64 << 10) + '"host": "ssh://pc"}',
        ):
            with self.subTest(content=content[:40]):
                path = self.target(content)
                with self.assertRaisesRegex(ConfigError, f"^Docker target {re.escape(path)}: "):
                    daemon.docker_call("/bin/docker", "ps")

    def test_remote_target_without_the_wrapper_is_refused(self):
        self.target('{"host": "ssh://pc"}')
        with (
            ssh_wrapper(present=False) as wrapper,
            self.assertRaisesRegex(
                SessionError, f"ssh wrapper unavailable: {re.escape(wrapper)}; reinstall the broker"
            ),
        ):
            daemon.docker_call("/bin/docker", "ps")

    def test_ssh_agent_passes_only_a_set_socket(self):
        with mock.patch.dict(os.environ, {"SSH_AUTH_SOCK": "/private/agent.sock"}):
            self.assertEqual(daemon.ssh_agent(), {"SSH_AUTH_SOCK": "/private/agent.sock"})
        with mock.patch.dict(os.environ, {"SSH_AUTH_SOCK": ""}):
            self.assertEqual(daemon.ssh_agent(), {})

    def test_unreachable_daemon_words(self):
        for stderr, want in (
            (
                "Cannot connect to the Docker daemon at unix:///x. Is the docker daemon running?",
                True,
            ),
            (
                "error during connect: stderr=ssh: connect to host pc port 22: Connection refused",
                True,
            ),
            ("Error: No such container: c", False),
        ):
            self.assertEqual(daemon.unreachable(stderr), want, stderr)
