"""hosts/docker/hook.py: the Docker Host's hook callback checks its state, marks the container
pane its launch recorded through docker exec, and names the host's target in the events line.
"""

import os
from types import SimpleNamespace
from unittest import mock

from ensemble.session.engine.types import HookCall, HookError
from ensemble.session.hosts.docker import hook as docker_hook
from tests.session.case import Case
from tests.session.unit.hosts.docker.support import FakePopen

DOCKER = "/usr/local/bin/docker"
GEN = "0123456789abcdef0123456789abcdef"
CONTAINER = f"ensemble-w-{GEN}"


class DockerHookTest(Case):
    def setUp(self):
        patcher = mock.patch("pwd.getpwuid", return_value=SimpleNamespace(pw_dir="/Users/a"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = {
            "docker": DOCKER,
            "endpoint": "unix:///Users/a/.docker/run/docker.sock",
            "container": CONTAINER,
            "target": "w",
            "generation": GEN,
        }
        self.events = os.path.join(self.home, "events")
        open(self.events, "w").close()

    def hook(self, identity, word="done", **changes):
        popen = FakePopen((identity.encode(), b"", 0), (b"", b"", 0))
        call = HookCall("Stop", {}, "sid", self.home, self.events, {**self.state, **changes})
        with mock.patch("subprocess.Popen", popen):
            docker_hook.on_hook(word, call)
        return [argv for argv, _, _ in popen.calls]

    def test_events_line_names_the_host_target_not_the_container_session(self):
        calls = self.hook(f"4242 $3 %7 4343\tsid\t{GEN}\n")
        prefix = [DOCKER, "--host", self.state["endpoint"], "exec", "-i", CONTAINER, "tmux"]
        self.assertEqual(
            calls,
            [
                [
                    *prefix,
                    "display-message",
                    "-p",
                    "-t",
                    "=w:",
                    "#{@process}\t#{@sid}\t#{@generation}",
                ],
                [*prefix, "set-option", "-t", "%7", "@state", "done"],
            ],
        )
        with open(self.events) as f:
            self.assertRegex(f.read(), r"^\d\d:\d\d:\d\d w done\n$")

    def test_a_mismatch_marks_nothing(self):
        for identity in (
            f"4242 $3 %7 4343\tother\t{GEN}\n",
            "4242 $3 %7 4343\tsid\tother\n",
            "bad\n",
        ):
            with self.subTest(identity=identity):
                self.assertEqual(len(self.hook(identity)), 1)

    def test_bad_state_is_a_hook_error(self):
        for changes, why in (
            ({"docker": "docker"}, "docker"),
            ({"target": "a b"}, "target"),
            ({"generation": "../x"}, "generation"),
            ({"endpoint": "unix:///other/.docker/run/docker.sock"}, "endpoint"),
            ({"container": "ensemble-x-1"}, "container"),
        ):
            with self.subTest(changes=changes), self.assertRaisesRegex(HookError, why):
                self.hook("", **changes)
