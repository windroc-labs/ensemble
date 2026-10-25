"""hosts/docker/mounts.py: the workspace volume name; daemon.py: the account home, endpoint and docker command line."""

import os
from unittest import mock

from ensemble.session.hosts.docker import daemon, mounts

from session.tests.case import Case

HOME = "/Users/example"


class VolumeTest(Case):
    def test_workspace_volume(self):
        self.assertEqual(mounts.workspace_volume("worker_1"), "ensemble-ws-worker_1")


class PathsTest(Case):
    def test_account_home_state_dir_endpoint_and_docker_call(self):
        with mock.patch("pwd.getpwuid", return_value=mock.Mock(pw_dir="/Users/acct")):
            self.assertEqual(daemon.account_home(), "/Users/acct")
            self.assertEqual(daemon.endpoint(), "unix:///Users/acct/.docker/run/docker.sock")
            env = {"DOCKER_HOST": "tcp://x", "DOCKER_CONTEXT": "c", "DOCKER_CONFIG": "/c", "K": "v"}
            with mock.patch.dict(os.environ, env):
                argv, called = daemon.docker_call("/bin/docker", "ps", "-a")
        self.assertEqual(
            argv,
            ["/bin/docker", "--host", "unix:///Users/acct/.docker/run/docker.sock", "ps", "-a"],
        )
        self.assertEqual(called["K"], "v")
        self.assertFalse({"DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG"} & set(called))
        self.assertEqual(mounts.state_dir("w"), os.path.join(self.ensemble, "docker-state", "w"))
