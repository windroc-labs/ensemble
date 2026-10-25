"""Delegate refusal types, a start's SessionError as the run's result, and a live run's refusals
from its run dir alone.
"""

import io
import os
import shutil
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from ensemble.delegate import ConfigError, Spec, cli
from ensemble.delegate.composer import Composer
from ensemble.delegate.engine.drivers import AgentDriver
from ensemble.delegate.engine.harness_delegate import HarnessDelegate
from ensemble.delegate.engine.types import Begun, Finished, Layout, Result
from ensemble.session import ConfigError as SessionConfigError
from ensemble.session import SessionError
from tests.delegate.case import REPO, child_env
from tests.delegate.fakes.harness_session import HarnessSession, harness_launches
from tests.delegate.harness import SID, HarnessCase
from tests.delegate.runner import executed


class PlacementSession(HarnessSession):
    @classmethod
    def host(cls, name=None):
        if name == "unknown":
            raise SessionConfigError("unknown host unknown")
        return super().host(name)

    @classmethod
    def display(cls, display=None, *, host=None):
        if display == "invalid":
            raise SessionConfigError("invalid display")
        return display


class MissingTmuxSession(HarnessSession):
    @classmethod
    def preflight(cls, spec, **placement):
        raise SessionError("tmux not found")

    @classmethod
    def start(cls, spec, **placement):
        raise SessionError("tmux not found")


class Refusals(HarnessCase):
    def construct(self, *, session_cls=HarnessSession, **placement):
        spec = Spec("input", self.work, task="echo", client="fake")
        return HarnessDelegate("dummy-tester", spec, session_cls=session_cls, **placement)

    def test_host_display_and_harness_configuration_refusals_are_delegate_errors(self):
        for placement in ({"host": "unknown"}, {"display": "invalid"}):
            with self.subTest(placement=placement), self.assertRaises(ConfigError):
                self.construct(session_cls=PlacementSession, **placement)
            self.assertFalse(os.path.exists(self.work))
        with open(self.local, "a", encoding="utf-8") as file:
            file.write('[delegate.delegates]\nharness_host = "unknown"\n')
        with self.assertRaisesRegex(ConfigError, "harness_host"):
            self.make()
        self.assertFalse(os.path.exists(self.work))

    def test_client_preflight_driver_failure_is_a_constructor_refusal_without_metadata(self):
        with self.assertRaisesRegex(ConfigError, "tmux not found"):
            self.construct(session_cls=MissingTmuxSession)
        self.assertFalse(os.path.exists(self.work))

    def test_configuration_read_failures_are_delegate_errors_in_constructor_and_attach(self):
        self.make()
        before = self.metadata()
        for error in (OSError("cannot read configuration"), ValueError("bad configuration")):
            for action in (
                self.make,
                lambda: HarnessDelegate.attach(self.work, session_cls=HarnessSession),
            ):
                with (
                    self.subTest(error=error, action=action),
                    mock.patch("builtins.open", side_effect=error),
                    self.assertRaisesRegex(ConfigError, str(error)),
                ):
                    action()
        self.assertEqual(self.metadata(), before)

    def test_a_start_driver_error_is_the_result_with_one_finished(self):
        events = []
        spec = Spec("input", self.work, task="echo", mode="tui", name="agent")
        result = executed(
            Composer().resolve("dummy-tester", "echo"),
            spec,
            driver=AgentDriver(MissingTmuxSession),
            on_event=events.append,
        )
        self.assertEqual(result, Result(1, None, "tui: tmux not found"))
        self.assertEqual(events, [Finished(result)])

    def test_prompt_rerun_after_output_failure_replaces_the_incomplete_attempt(self):
        command = [
            sys.executable,
            "-P",
            "-m",
            "ensemble.delegate.cli",
            "prompt",
            "--role",
            "dummy-tester",
            "--task",
            "echo",
            "--input",
            "input",
            "--workdir",
            self.work,
        ]
        environment = child_env(None, REPO)
        with open(os.path.join(self.home, "missing"), "w", encoding="utf-8") as file:
            file.write("not a directory")
        failed = subprocess.run(
            [*command, "--out", os.path.join(self.home, "missing", "out.md")],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertNotEqual(failed.returncode, 0)
        output = os.path.join(self.work, "out.md")
        resumed = subprocess.run(
            [*command, "--out", output],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertTrue(os.path.isfile(output))
        self.assertFalse(os.path.exists(os.path.join(Layout(self.work).run, "delegate.json")))


class BoundDelegate(HarnessDelegate):
    def __init__(self, role, spec, **placement):
        super().__init__(role, spec, session_cls=HarnessSession, **placement)


class LiveRun(HarnessCase):
    def setUp(self):
        super().setUp()
        for key in ("ENSEMBLE_SID", "ENSEMBLE_EVENTS"):
            os.environ.pop(key, None)

    def cli(self, *extra):
        argv = ["run", "--role", "dummy-tester", "--task", "echo", "--input", "again"]
        argv += ["--out", os.path.join(self.home, "out.md"), "--workdir", self.work]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(
                [*argv, "--client", "fake", "--sid", SID, *extra], delegate_cls=BoundDelegate
            )
        return code, out.getvalue(), err.getvalue()

    def test_live_run_survives_emptied_agent_dir(self):
        agent = Layout(self.work).agent
        delegate = self.make()
        refusal = f"delegate: {self.work}: delegate is still alive\n"
        seen = []

        def meanwhile(event):
            if not isinstance(event, Begun):
                return
            shutil.rmtree(agent)
            os.mkdir(agent)
            with open(os.path.join(agent, "delegate.json"), "w", encoding="utf-8") as file:
                file.write('{"version": 1, "attempt": 9}')
            before = self.metadata()
            launches = len(harness_launches())
            seen.append(self.cli())
            view = HarnessDelegate.attach(self.work, session_cls=HarnessSession)
            self.assertEqual(view.resume().sid, SID)
            with self.assertRaises(ConfigError) as caught:
                view.detach()
            seen.append(str(caught.exception))
            self.assertEqual((self.metadata(), len(harness_launches())), (before, launches))

        self.assertEqual(delegate.run(meanwhile).outcome.status, "done")
        self.assertEqual(seen, [(2, "", refusal), f"{self.work}: delegate is still alive"])
        self.assertEqual(self.make().run().returncode, 0)
        self.assertEqual(self.metadata()["attempt"], 2)
