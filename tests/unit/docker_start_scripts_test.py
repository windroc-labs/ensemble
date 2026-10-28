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
        self.code = runpy.run_path(str(LAUNCHER / "startup/startup.py"))

    def run_start(self, failure=-1):
        calls = []

        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            return SimpleNamespace(returncode=int(len(calls) - 1 == failure))

        with mock.patch("subprocess.run", run):
            result = self.code["main"](["claude", "/state", "/work", "content-check"])
        return result, calls

    def test_content_and_bootstrap_share_agent_exec(self):
        result, calls = self.run_start()
        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1]["input"], b"content-check")
        self.assertEqual(
            calls[1][0][1:], ["-I", "/opt/ensemble/lib/bootstrap.py", "claude", "/state", "/work"]
        )

    def test_each_failure_stops_at_its_own_stage(self):
        for failure, expected in enumerate(
            (self.code["CONTENT_FAILED"], self.code["BOOTSTRAP_FAILED"])
        ):
            with self.subTest(failure=failure):
                result, calls = self.run_start(failure=failure)
                self.assertEqual(result, expected)
                self.assertEqual(len(calls), failure + 1)

    def test_wrong_usage_has_its_own_code(self):
        self.assertEqual(self.code["main"]([]), self.code["USAGE"])

    def test_oserror_keeps_stage(self):
        for failed, expected in enumerate(
            (self.code["CONTENT_FAILED"], self.code["BOOTSTRAP_FAILED"])
        ):
            with mock.patch(
                "subprocess.run", side_effect=[SimpleNamespace(returncode=0)] * failed + [OSError()]
            ):
                self.assertEqual(
                    self.code["main"](["claude", "/state", "/work", "check"]), expected
                )

    def test_opencode_has_no_bootstrap_state(self):
        with mock.patch("subprocess.run", return_value=SimpleNamespace(returncode=0)) as run:
            self.assertEqual(self.code["main"](["opencode", "/state", "/work", "check"]), 0)
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
