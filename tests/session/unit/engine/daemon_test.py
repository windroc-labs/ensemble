"""engine/daemon.py: the capped run, over fake processes; a session's daemon record and the docker
command line to a daemon.
"""

import contextlib
import errno
import json
import os
import re
import threading
from unittest import mock

from ensemble.session.engine import daemon
from ensemble.session.engine.daemon import OUT_MAX, Overflow, capped
from ensemble.session.engine.types import Daemon, DaemonRecord, SessionError
from ensemble.utils import DOCKER_STATE
from tests.session.case import Case
from tests.session.unit.hosts.docker.support import FakePopen, FakeProc, ssh_dir
from tests.utils.process_fixtures import DEVNULL, PIPE, TimeoutExpired

SYSTEM = "/usr/bin:/bin:/usr/sbin:/sbin"
LOCAL_ID = "8f1c2d3e-0a4b-4c5d-9e6f-a1b2c3d4e5f6"


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


class DockerCallTest(Case):
    def setUp(self):
        self.local = Daemon(
            "local",
            "unix:///Users/u/.docker/run/docker.sock",
            remote=False,
            path=SYSTEM,
            id=LOCAL_ID,
        )
        self.wrapper = ssh_dir(self.home)
        self.remote = Daemon(
            "remote",
            "ssh://u@pc:2222",
            remote=True,
            path=f"{self.wrapper}{os.pathsep}{SYSTEM}",
            id="PC:ID",
        )

    def test_a_daemon_s_call_ignores_a_hostile_docker_environment(self):
        hostile = {
            "DOCKER_HOST": "tcp://hostile:1234",
            "DOCKER_CONTEXT": "hostile",
            "DOCKER_CONFIG": self.home,
            "PATH": "/caller/bin",
        }
        for found in (self.local, self.remote):
            with self.subTest(daemon=found.name), mock.patch.dict(os.environ, hostile):
                argv, env = daemon.docker_call("/bin/docker", "ps", daemon=found)
                self.assertEqual(argv, ["/bin/docker", "--host", found.endpoint, "ps"])
                self.assertFalse(set(daemon.CLEARED) & set(env))
                self.assertEqual(env["PATH"], f"{found.path}{os.pathsep}/caller/bin")

    def test_a_remote_daemon_without_its_ssh_wrapper_is_refused_running_nothing(self):
        bare = os.path.join(self.home, "bare")
        os.makedirs(bare)
        unrunnable = os.path.join(self.home, "unrunnable")
        os.makedirs(unrunnable)
        with open(os.path.join(unrunnable, "ssh"), "w"):
            pass
        os.chmod(os.path.join(unrunnable, "ssh"), 0o644)
        for directory in (bare, unrunnable):
            found = self.remote._replace(path=f"{directory}{os.pathsep}{self.wrapper}")
            wrapper = os.path.join(directory, "ssh")
            want = (
                f"^Docker target ssh://u@pc:2222: ssh wrapper unavailable: {re.escape(wrapper)};"
                " reinstall the broker$"
            )
            for call in (
                lambda found=found: daemon.docker_call("/bin/docker", "ps", daemon=found),
                lambda found=found: daemon.attach_env(found),
            ):
                with (
                    self.subTest(directory=directory),
                    mock.patch("subprocess.Popen") as popen,
                    self.assertRaisesRegex(SessionError, want),
                ):
                    call()
                popen.assert_not_called()

    def test_attach_env_is_a_remote_daemon_s_path_and_set_ssh_agent(self):
        path = f"PATH={self.remote.path}"
        for sock, want in (
            ("/private/agent.sock", (path, "SSH_AUTH_SOCK=/private/agent.sock")),
            ("", (path,)),
        ):
            with self.subTest(sock=sock), mock.patch.dict(os.environ, {"SSH_AUTH_SOCK": sock}):
                self.assertEqual(daemon.attach_env(self.remote), want)
                self.assertEqual(daemon.attach_env(self.local), ())

    def test_ssh_agent_passes_only_a_set_socket(self):
        with mock.patch.dict(os.environ, {"SSH_AUTH_SOCK": "/private/agent.sock"}):
            self.assertEqual(daemon.ssh_agent(), {"SSH_AUTH_SOCK": "/private/agent.sock"})
        with mock.patch.dict(os.environ, {"SSH_AUTH_SOCK": ""}):
            self.assertEqual(daemon.ssh_agent(), {})

    def test_describe_names_the_local_docker_desktop(self):
        self.assertEqual(
            daemon.describe(self.local),
            "the local Docker Desktop (unix:///Users/u/.docker/run/docker.sock)",
        )
        self.assertEqual(daemon.describe(self.remote), "ssh://u@pc:2222")


class RecordTest(Case):
    GOOD = {
        "name": "remote",
        "id": "ABCD:EFGH:IJKL",
        "endpoint": "ssh://u@pc",
        "path": "/usr/local/libexec/ensemble/ssh:/usr/bin",
        "docker": "/usr/local/bin/docker",
    }

    def setUp(self):
        self.path = os.path.join(os.path.expanduser(DOCKER_STATE), "s1", daemon.RECORD_FILE)

    def write(self, text):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            f.write(text)

    def test_a_record_round_trips_its_daemon_and_docker(self):
        for found in (
            Daemon(
                "remote",
                "ssh://u@pc",
                remote=True,
                path="/usr/local/libexec/ensemble/ssh:/usr/bin",
                id="ABCD:EFGH",
            ),
            Daemon(
                "local",
                "unix:///Users/u/.docker/run/docker.sock",
                remote=False,
                path=SYSTEM,
                id=LOCAL_ID,
            ),
        ):
            with self.subTest(name=found.name):
                daemon.save_record("s1", DaemonRecord(found, "/bin/docker"))
                self.assertEqual(daemon.load_record("s1"), DaemonRecord(found, "/bin/docker"))
                with open(self.path) as f:
                    self.assertEqual(
                        json.load(f),
                        {
                            "name": found.name,
                            "id": found.id,
                            "endpoint": found.endpoint,
                            "path": found.path,
                            "docker": "/bin/docker",
                        },
                    )
        self.assertIsNone(daemon.load_record("s2"))

    def test_a_sessions_recorded_image_is_its_last_starts(self):
        folder = os.path.dirname(self.path)
        os.makedirs(folder)
        digest = "sha256:" + "d" * 64
        generation = "0123456789abcdef0123456789abcdef"
        for host, result, found in (
            ({"generation": generation}, {"image_digest": digest}, digest),
            (None, {"image_digest": digest}, None),
            ({"generation": "../x"}, {"image_digest": digest}, None),
            ({"generation": generation}, None, None),
            ({"generation": generation}, {"image_digest": "ensemble-worker:latest"}, None),
            ([], {"image_digest": digest}, None),
        ):
            with self.subTest(host=host, result=result):
                for name, value in (
                    (daemon.SESSION_RECORD, host),
                    (daemon.result_file(generation), result),
                ):
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(os.path.join(folder, name))
                    if value is not None:
                        with open(os.path.join(folder, name), "w") as f:
                            json.dump(value, f)
                if found:
                    self.assertEqual(daemon.recorded_image("s1"), found)
                    continue
                with self.assertRaises(OSError) as caught:
                    daemon.recorded_image("s1")
                self.assertEqual(caught.exception.errno, errno.EIO)

    def test_a_malformed_record_is_refused_naming_its_file(self):
        good = self.GOOD
        for label, text in (
            ("name", json.dumps({**good, "name": "pc"})),
            ("name type", json.dumps({**good, "name": 1})),
            ("id", json.dumps({**good, "id": "../x"})),
            ("id empty", json.dumps({**good, "id": ""})),
            ("id long", json.dumps({**good, "id": "a" * 129})),
            ("id null", json.dumps({**good, "id": None})),
            ("endpoint empty", json.dumps({**good, "endpoint": ""})),
            ("endpoint control", json.dumps({**good, "endpoint": "ssh://pc\n"})),
            ("path empty", json.dumps({**good, "path": ""})),
            ("path control", json.dumps({**good, "path": "/a\x7f"})),
            ("docker relative", json.dumps({**good, "docker": "docker"})),
            ("docker empty", json.dumps({**good, "docker": ""})),
            ("extra key", json.dumps({**good, "host": "ssh://pc"})),
            ("missing key", json.dumps({k: v for k, v in good.items() if k != "path"})),
            ("not an object", "[]"),
            ("not json", "{"),
            ("too long", json.dumps({**good, "path": "/a" * daemon.RECORD_MAX})),
        ):
            with self.subTest(label=label):
                self.write(text)
                with self.assertRaises(OSError) as raised:
                    daemon.load_record("s1")
                self.assertEqual(raised.exception.errno, errno.EINVAL)
                self.assertEqual(raised.exception.filename, self.path)
        self.write(json.dumps(good))
        self.assertEqual(daemon.load_record("s1").daemon.id, good["id"])
