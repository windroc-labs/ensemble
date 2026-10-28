import base64
import io
import json
import runpy
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.case import REPO, Case

LAUNCHER = Path(REPO, "src/broker/internal/launcher")


class ContainerStart(Case):
    def setUp(self):
        self.code = runpy.run_path(str(LAUNCHER / "checks/start-container.py"))
        self.agent = SimpleNamespace(pw_uid=1001, pw_gid=1002, pw_dir="/home/agent")

    def run_start(self, hook="/usr/local/bin/hook", failure=-1):
        calls = []

        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            return SimpleNamespace(returncode=int(len(calls) - 1 == failure))

        with mock.patch("subprocess.run", run), mock.patch("pwd.getpwnam", return_value=self.agent):
            result = self.code["main"](["claude", "/state", "/work", hook, "content-check"])
        return result, calls

    def test_content_and_bootstrap_run_as_agent_and_hook_as_root(self):
        result, calls = self.run_start()
        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 3)
        for _, kwargs in calls[:2]:
            self.assertEqual(kwargs["user"], 1001)
            self.assertEqual(kwargs["group"], 1002)
            self.assertEqual(kwargs["extra_groups"], [])
            self.assertEqual(kwargs["env"]["HOME"], "/home/agent")
        self.assertNotIn("user", calls[2][1])
        self.assertEqual(calls[0][1]["input"], b"content-check")
        for argv, _ in calls[1:]:
            self.assertEqual(argv[1], "-I")
        self.assertEqual(calls[1][0][-3:], ["claude", "/state", "/work"])
        self.assertEqual(calls[2][0][-2:], ["hook", "/usr/local/bin/hook"])

    def test_each_failure_stops_at_its_own_stage(self):
        for failure, expected in enumerate((41, 42, 43)):
            with self.subTest(failure=failure):
                result, calls = self.run_start(failure=failure)
                self.assertEqual(result, expected)
                self.assertEqual(len(calls), failure + 1)

    def test_no_hook_needs_no_identity_switch(self):
        result, calls = self.run_start(hook="")
        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 2)
        for _, kwargs in calls:
            self.assertNotIn("user", kwargs)

    def test_opencode_has_no_bootstrap_state(self):
        with mock.patch("subprocess.run", return_value=SimpleNamespace(returncode=0)) as run:
            self.assertEqual(self.code["main"](["opencode", "/state", "/work", "", "check"]), 0)
        self.assertEqual(run.call_count, 1)


class CredentialBatch(Case):
    def setUp(self):
        self.code = runpy.run_path(str(LAUNCHER / "assets/credential-batch.py"))

    def payload(self):
        return io.StringIO(
            json.dumps(
                [
                    {"argv": ["write", name], "stdin": base64.b64encode(value).decode()}
                    for name, value in (("client", b"token\x00\xff"), ("owner", b"github"))
                ]
            )
        )

    def test_all_commands_receive_bytes_on_stdin_with_output_suppressed(self):
        with mock.patch("subprocess.run", return_value=SimpleNamespace(returncode=0)) as run:
            self.assertEqual(self.code["deliver"](self.payload()), {"completed": 2})
        self.assertEqual(run.call_args_list[0].kwargs["input"], b"token\x00\xff")
        self.assertEqual(run.call_args_list[1].kwargs["input"], b"github")
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["stdout"], -3)
            self.assertEqual(call.kwargs["stderr"], -3)

    def test_failure_returns_only_completed_prefix(self):
        for failure in (SimpleNamespace(returncode=1), OSError("secret")):
            with (
                self.subTest(failure=type(failure)),
                mock.patch(
                    "subprocess.run", side_effect=[SimpleNamespace(returncode=0), failure]
                ) as run,
            ):
                result = self.code["deliver"](self.payload())
            self.assertEqual(result, {"completed": 1})
            self.assertEqual(run.call_count, 2)

    def test_bad_payload_runs_nothing(self):
        for payload in ("", "null", "{}", '[{"argv":[],"stdin":"bad!"}]'):
            with self.subTest(payload=payload), mock.patch("subprocess.run") as run:
                self.assertEqual(self.code["deliver"](io.StringIO(payload)), {"completed": 0})
                run.assert_not_called()
