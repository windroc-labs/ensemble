"""hosts/docker/mounts.py: the workspace volume name and state dir; daemon.py: the docker command line
to a daemon.
"""

import os
from unittest import mock

from ensemble.session.engine import daemon
from ensemble.session.engine.types import Daemon
from ensemble.session.hosts.docker import mounts
from tests.session.case import Case

LOCAL = Daemon(
    "local",
    "unix:///Users/acct/.docker/run/docker.sock",
    remote=False,
    path="/usr/bin:/bin:/usr/sbin:/sbin",
    id="8f1c2d3e-0a4b-4c5d-9e6f-a1b2c3d4e5f6",
)


class VolumeTest(Case):
    def test_workspace_volume(self):
        self.assertEqual(mounts.workspace_volume("worker_1"), "ensemble-ws-worker_1")


class PathsTest(Case):
    def test_state_dir_and_docker_call(self):
        env = {"DOCKER_HOST": "tcp://x", "DOCKER_CONTEXT": "c", "DOCKER_CONFIG": "/c", "K": "v"}
        with mock.patch.dict(os.environ, {**env, "PATH": "/caller/bin"}):
            argv, called = daemon.docker_call("/bin/docker", "ps", "-a", daemon=LOCAL)
        self.assertEqual(
            argv,
            ["/bin/docker", "--host", "unix:///Users/acct/.docker/run/docker.sock", "ps", "-a"],
        )
        self.assertEqual(called["K"], "v")
        self.assertEqual(called["PATH"], f"{LOCAL.path}{os.pathsep}/caller/bin")
        self.assertFalse({"DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG"} & set(called))
        self.assertEqual(mounts.state_dir("w"), os.path.join(self.ensemble, "docker-state", "w"))
