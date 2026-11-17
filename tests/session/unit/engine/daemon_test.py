"""engine/daemon.py: the capped run, over fake processes; the Docker target and its docker command line."""

import contextlib
import errno
import json
import os
import re
import threading
from types import SimpleNamespace
from unittest import mock

from ensemble.session.engine import daemon
from ensemble.session.engine.daemon import OUT_MAX, Overflow, capped
from ensemble.session.engine.types import ConfigError, DockerAccess, SessionError
from ensemble.utils import DOCKER_STATE
from tests.session.case import REPO, Case
from tests.session.unit.hosts.docker.support import FakePopen, FakeProc, ssh_wrapper
from tests.utils.process_fixtures import DEVNULL, PIPE, TimeoutExpired


def _done(returncode, stdout, stderr):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


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
        self.target('{"host": "ssh://u@pc:2222", "local_secrets": []}')
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

    def test_target_files_shared_with_the_broker(self):
        with open(
            os.path.join(REPO, "src/broker/protocol/docker-broker/cases/target_files.json")
        ) as f:
            cases = json.load(f)
        for case in cases["valid"]:
            with self.subTest(content=case["content"][:60]):
                self.target(case["content"])
                self.assertEqual(
                    daemon.target(), daemon.Target(case["host"], tuple(case["local_secrets"]))
                )
        for content in cases["invalid"]:
            with self.subTest(content=content[:60]):
                path = self.target(content)
                with self.assertRaisesRegex(ConfigError, f"^Docker target {re.escape(path)}: "):
                    daemon.target()

    def test_no_target_file_has_no_local_secrets(self):
        self.assertEqual(daemon.target(), daemon.Target(daemon.local_endpoint(), ()))

    def test_remote_target_without_the_wrapper_is_refused(self):
        self.target('{"host": "ssh://pc", "local_secrets": []}')
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


class DaemonIdentityTest(Case):
    def setUp(self):
        wrapper = ssh_wrapper()
        wrapper.__enter__()
        self.addCleanup(wrapper.__exit__, None, None, None)
        self.calls = []
        self.result = _done(0, '"8f1c2d3e-0a4b-4c5d-9e6f-a1b2c3d4e5f6"\n', "")

    def run_docker(self, argv, env, *, timeout):
        self.calls.append(argv)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result

    def test_daemon_id_is_docker_info_s_id(self):
        found = daemon.daemon_id("/bin/docker", "unix:///d.sock", run=self.run_docker)
        self.assertEqual(found, "8f1c2d3e-0a4b-4c5d-9e6f-a1b2c3d4e5f6")
        self.assertEqual(
            self.calls,
            [["/bin/docker", "--host", "unix:///d.sock", "info", "--format", "{{json .ID}}"]],
        )

    def test_an_old_style_id_is_usable(self):
        self.result = _done(0, '"ABCD:EFGH:IJKL:MNOP"\n', "")
        self.assertEqual(
            daemon.daemon_id("/bin/docker", "ssh://pc", run=self.run_docker), "ABCD:EFGH:IJKL:MNOP"
        )

    def test_daemon_id_failures_name_the_target(self):
        for result, why in (
            (
                _done(1, "", "error during connect: refused\n"),
                "unreachable",
            ),
            (_done(1, "", "permission denied\n"), "permission denied"),
            (_done(0, "ABCD\n", ""), "no usable daemon ID"),
            (_done(0, '"../x"\n', ""), "no usable daemon ID"),
            (_done(0, '""\n', ""), "no usable daemon ID"),
            (
                _done(0, '"' + "a" * 129 + '"\n', ""),
                "no usable daemon ID",
            ),
            (TimeoutExpired(["docker"], 10), "timed out"),
            (OSError("no docker"), "no docker"),
        ):
            with self.subTest(why=why):
                self.result = result
                with self.assertRaisesRegex(SessionError, rf"^Docker target ssh://pc: .*{why}"):
                    daemon.daemon_id("/bin/docker", "ssh://pc", run=self.run_docker)

    def test_access_record_round_trips_its_daemon(self):
        access = DockerAccess("/bin/docker", "ssh://u@pc", "ABCD:EFGH")
        daemon.save_access("s1", access)
        self.assertEqual(daemon.load_access("s1"), access)
        self.assertIsNone(daemon.load_access("s2"))

    def test_an_access_record_without_a_usable_daemon_is_malformed(self):
        path = os.path.join(os.path.expanduser(DOCKER_STATE), "s1", daemon.ACCESS_FILE)
        os.makedirs(os.path.dirname(path))
        for record in (
            {"docker": "/bin/docker", "host": "ssh://pc"},
            {"docker": "/bin/docker", "host": "ssh://pc", "daemon": "../x"},
            {"docker": "/bin/docker", "host": "ssh://pc", "daemon": 1},
            {"docker": "/bin/docker", "host": "ssh://pc", "daemon": "A", "extra": "x"},
        ):
            with self.subTest(record=record):
                with open(path, "w") as f:
                    json.dump(record, f)
                with self.assertRaises(OSError) as raised:
                    daemon.load_access("s1")
                self.assertEqual(raised.exception.errno, errno.EINVAL)

    def test_describe_names_the_local_docker_desktop(self):
        with mock.patch("pwd.getpwuid", return_value=mock.Mock(pw_dir="/Users/u")):
            local = daemon.local_endpoint()
            self.assertEqual(local, "unix:///Users/u/.docker/run/docker.sock")
            self.assertEqual(daemon.describe(local), f"the local Docker Desktop ({local})")
            self.assertEqual(daemon.describe("ssh://u@pc"), "ssh://u@pc")
