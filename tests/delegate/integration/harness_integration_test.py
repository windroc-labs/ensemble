"""HarnessDelegate plans before writing and persists its identity for durable views."""

import io
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from contextlib import redirect_stderr
from dataclasses import replace
from unittest import mock

from ensemble.delegate import ConfigError, DelegateEvent, Outcome, Progress, Spec
from ensemble.delegate.engine.drivers import InstructionDriver
from ensemble.delegate.engine.harness_delegate import HarnessDelegate
from ensemble.delegate.engine.state import RunState
from ensemble.delegate.engine.types import Begun, Finished, Layout, Limit, Result
from ensemble.session import (
    SIGNALS,
    Access,
    Exited,
    Launch,
    LimitHit,
    Mode,
    NoRecordedSession,
    Reply,
    SessionError,
    SessionStatus,
    Usage,
    Window,
)
from ensemble.session import ConfigError as SessionConfigError
from ensemble.session import Spec as SessionSpec
from ensemble.utils import Record
from tests.delegate.case import REPO, SRC, child_env
from tests.delegate.fakes import callback_recorder
from tests.delegate.fakes.harness_session import (
    FailingSession,
    HarnessSession,
    PersistentSession,
    RecordingHost,
    harness_launches,
)
from tests.delegate.fakes.session import SessionClass
from tests.delegate.harness import SID, HarnessCase
from tests.delegate.replies import block, mark
from tests.delegate.unit.runs import DONE

USAGE_QUERY = """import io, json, unittest
from unittest import mock
from ensemble.delegate import HarnessDelegate
from ensemble.session import Usage, Window
from tests.delegate.fakes.harness_session import HarnessSession, RecordingHost
from tests.delegate.fakes.usage import UsageClient
case = unittest.TestCase()
sentinel = Usage(Window(12.5, 1767225600.0), Window(37.0, None))
probes = []
for expected in (sentinel, None):
    UsageClient.usage_result = expected
    process = mock.Mock(stdout=io.StringIO("quota"))
    process.communicate.return_value = ("quota", "")
    with mock.patch("subprocess.Popen", return_value=process) as probe:
        case.assertIs(HarnessDelegate.usage("dummy-tester"), expected)
    case.assertEqual(probe.call_count, 1)
    probes.append(probe.call_count)
case.assertEqual(UsageClient.queried, ["role_usage", "role_usage"])
case.assertEqual((UsageClient.starts, HarnessSession.started, RecordingHost.launches), ([], [], []))
print(json.dumps({"queried": UsageClient.queried, "probes": probes,
                  "starts": UsageClient.starts, "session_starts": HarnessSession.started,
                  "launches": RecordingHost.launches}))
"""


class ConfiguredSession(HarnessSession):
    """Its Hosts are the configured [hosts], as HostedSession's are."""

    @classmethod
    def host(cls, name=None):
        if name is not None and name not in cls.configured["hosts"].tables:
            raise SessionConfigError(f"unknown host {name}")
        return super().host(name)


def _close_child(child):
    if child.poll() is None:
        child.kill()
    child.communicate(timeout=10)


class Planning(HarnessCase):
    def test_constructor_persists_resolved_metadata_0600_without_starting(self):
        callback = ["writeback", "--issue", "ticket-id"]
        self.make(host="agent", harness_host="harness", display="opaque-display", callback=callback)
        path = self.state_record()
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        data = self.metadata()
        self.assertEqual(
            (
                data["role"],
                data["host"],
                data["harness_host"],
                data["display"],
                data["callback"],
                data["attempt"],
            ),
            ("dummy-tester", "agent", "harness", "opaque-display", callback, 1),
        )
        self.assertEqual(data["spec"]["sid"], SID)
        self.assertEqual(data["spec"]["input"], "Hello.\nLiteral input.")
        self.assertEqual((HarnessSession.started, RecordingHost.launches), ([], []))

    def test_invalid_role_task_and_mode_write_nothing(self):
        original = Spec("input", self.work)
        for role, spec in (
            ("absent-role", original),
            ("dummy-tester", replace(original, task="absent-task")),
            ("dummy-tester", replace(original, mode="absent-mode")),
        ):
            with self.subTest(role=role, spec=spec), self.assertRaises(ConfigError):
                self.make(role=role, spec=spec)
            self.assertFalse(os.path.exists(self.work))
            self.assertEqual((HarnessSession.started, RecordingHost.launches), ([], []))

    def test_invalid_replacement_preserves_existing_files_and_metadata(self):
        self.make()
        before = self.metadata()
        marker = os.path.join(self.work, "draft.md")
        with open(marker, "w", encoding="utf-8") as file:
            file.write("Draft")
        files = set(os.listdir(self.work))
        with self.assertRaises(ConfigError):
            self.make(role="absent-role")
        self.assertEqual(self.metadata(), before)
        self.assertEqual(set(os.listdir(self.work)), files)
        with open(marker, encoding="utf-8") as file:
            self.assertEqual(file.read(), "Draft")

    def test_live_owner_refuses_replacement_without_writing(self):
        delegate = self.make(host="agent", harness_host="harness")
        record = delegate.detach()
        before = self.metadata()
        with self.assertRaises(ConfigError):
            self.make()
        self.assertEqual(self.metadata(), before)
        self.assertTrue(HarnessDelegate.attach(record).alive())

    def test_ended_run_replacement_counts_attempt_and_stale_detach_is_refused(self):
        first = self.make()
        self.make()
        self.assertEqual(self.metadata()["attempt"], 2)
        with self.assertRaises(ConfigError):
            first.detach()
        self.assertEqual(RecordingHost.launches, [])

    def test_a_harness_host_named_default_is_refused_naming_the_configured_hosts(self):
        self.settings(harness_host="default")
        with self.assertRaises(ConfigError) as caught:
            self.make()
        self.assertEqual(
            str(caught.exception),
            "[delegates] harness_host: want one of agent, changed, docker, harness, tmux",
        )
        self.assertFalse(os.path.lexists(self.work))

    def test_replacing_a_run_whose_harness_host_left_the_config_is_refused(self):
        ConfiguredSession.reset()
        self.make(harness_host="harness", session_cls=ConfiguredSession).detach()
        with open(self.state_record(), "rb") as file:
            before = file.read()
        self.settings(hosts=("agent", "changed"))
        with self.assertRaises(ConfigError) as caught:
            self.make(session_cls=ConfiguredSession)
        self.assertEqual(str(caught.exception), "unknown host harness")
        with open(self.state_record(), "rb") as file:
            self.assertEqual(file.read(), before)

    def test_an_injected_harness_host_must_be_a_configured_host(self):
        with self.assertRaises(ConfigError) as caught:
            self.make(harness_host=RecordingHost("ghost"))
        self.assertEqual(
            str(caught.exception),
            "harness_host ghost: want one of agent, changed, docker, harness, tmux",
        )
        self.assertFalse(os.path.lexists(self.work))

    def test_an_injected_harness_host_launches_the_harness(self):
        used = []

        class Injected(RecordingHost):
            def detach(self, name, launch, **kw):
                used.append((self.name, name))
                return super().detach(name, launch, **kw)

        record = self.make(host="agent", harness_host=Injected("harness")).detach()
        self.assertEqual(used, [("harness", record.sessions[0])])
        self.assertEqual(
            (self.metadata()["host"], self.metadata()["harness_host"]), ("agent", "harness")
        )

    def test_an_unset_harness_host_is_the_agents_host(self):
        self.make(host="changed").detach()
        self.assertEqual(
            (self.metadata()["host"], self.metadata()["harness_host"]), ("changed", "changed")
        )
        self.assertEqual(RecordingHost.launches[0]["host"], "changed")

    def test_in_process_and_instruction_detach_refused(self):
        for spec, driver, message in (
            (
                Spec("input", self.work, mode="in_process", client="fake", sid=SID),
                None,
                "detach: mode must be headless or tui, got 'in_process'",
            ),
            (
                Spec("input", self.work, client="fake", sid=SID),
                InstructionDriver(),
                "instruction cannot detach",
            ),
        ):
            with self.subTest(message=message):
                delegate = self.make(spec=spec, **({} if driver is None else {"driver": driver}))
                with self.assertRaises(ConfigError) as caught:
                    delegate.detach()
                self.assertEqual(str(caught.exception), message)
                self.assertEqual(RecordingHost.launches, [])

    def test_a_failed_launch_keeps_the_state_not_detached(self):
        for failure, raised in (
            (SessionError("detach refused"), SessionError),
            (SessionConfigError("events file refused"), ConfigError),
        ):
            with self.subTest(failure=failure):
                FailingHost.failure = failure
                self.work = os.path.join(self.home, type(failure).__name__)
                delegate = self.make(harness_host=FailingHost("harness"))
                with open(self.state_record(), "rb") as file:
                    before = file.read()
                with self.assertRaises(raised):
                    delegate.detach()
                with open(self.state_record(), "rb") as file:
                    self.assertEqual(file.read(), before)
                self.assertFalse(self.metadata()["detached"])

    def test_the_events_path_is_checked_at_construction_and_given_to_the_harness(self):
        HarnessSession.hosts["harness"] = RefusedEvents("harness")
        spec = Spec("input", self.work, client="fake", sid=SID, events="ev#1")
        with self.assertRaises(ConfigError) as caught:
            self.make(spec=spec, harness_host="harness")
        self.assertIn("tmux would misread it", str(caught.exception))
        self.assertFalse(os.path.lexists(self.work))
        record = self.make(spec=replace(spec, events="events.log")).detach()
        self.assertEqual(RecordingHost.launches[0]["events"], os.path.abspath("events.log"))
        self.assertEqual(self.metadata()["events"], os.path.abspath("events.log"))
        self.stop(record)

    def test_a_foreground_headless_run_refuses_events(self):
        spec = Spec("input", self.work, client="fake", sid=SID, events="events.log")
        with self.assertRaises(ConfigError) as caught:
            self.make(spec=spec).run()
        self.assertIn("events", str(caught.exception))
        self.assertEqual(HarnessSession.started, [])

    def test_a_run_whose_cwd_is_the_ensemble_checkout_starts(self):
        self.make(spec=Spec("input", self.work, cwd=REPO, client="fake")).run()
        self.assertEqual(HarnessSession.started[0]["spec"].access.cwd, REPO)
        metadata = self.metadata()
        self.assertNotIn("protect", metadata["spec"])
        self.assertNotIn("protect", metadata["plan"]["session"])

    def test_constructor_keeps_its_config_snapshot_when_local_file_changes(self):
        delegate = self.make(spec=Spec("input", self.work, task="echo", sid=SID))
        self.settings(language="French", client="another-client")
        delegate.run()
        launched = HarnessSession.started[-1]["spec"]
        self.assertEqual(launched.client, "fake")
        self.assertIn("English", launched.prompt)
        self.assertNotIn("French", launched.prompt)


class Views(HarnessCase):
    def test_attach_keeps_identity_after_default_client_and_rules_change(self):
        delegate = self.make(host="agent", harness_host="harness", display="original")
        record = delegate.detach(
            outer={
                "kind": "outer",
                "ctl": ["ensemble-orchestrator"],
                "resume": ["ensemble-orchestrator", "resume", "id"],
            }
        )
        before = self.metadata()
        self.settings(language="French", client="changed-client", default="changed")
        view = HarnessDelegate.attach(record)
        self.assertTrue(view.alive())
        self.assertEqual(view.resume(), record)
        self.assertEqual(self.metadata(), before)
        self.assertEqual(HarnessSession.started, [])
        self.assertEqual(len(RecordingHost.launches), 1)

    def test_attach_refuses_missing_malformed_and_foreign_metadata(self):
        os.makedirs(os.path.dirname(self.state_record()))
        for value in (None, "not json", "{}", '{"worker": true}'):
            with self.subTest(value=value):
                if value is not None:
                    with open(self.state_record(), "w", encoding="utf-8") as file:
                        file.write(value)
                with self.assertRaises(ConfigError) as caught:
                    HarnessDelegate.attach(self.work)
                self.assertIn(self.work, str(caught.exception))
        self.assertEqual((HarnessSession.started, RecordingHost.launches), ([], []))

    def test_attach_refuses_a_stored_host_removed_from_config(self):
        for removed in ("changed", "harness"):
            with self.subTest(removed=removed):
                ConfiguredSession.reset()
                self.settings()
                self.work = os.path.join(self.home, removed)
                self.make(
                    host="changed", harness_host="harness", session_cls=ConfiguredSession
                ).detach()
                self.settings(
                    hosts=tuple(h for h in ("agent", "harness", "changed") if h != removed)
                )
                with self.assertRaises(ConfigError) as caught:
                    HarnessDelegate.attach(self.work, session_cls=ConfiguredSession)
                self.assertEqual(str(caught.exception), f"unknown host {removed}")

    def test_resume_refuses_a_cwd_no_longer_trusted_and_launches_nothing(self):
        project = os.path.join(self.home, "project")
        os.mkdir(project)
        self.settings(trusted_dirs=(project,))
        spec = Spec("input", self.work, task="echo", sid=SID, cwd=project, client="fake")
        record = self.make(spec=spec).detach()
        self.assertTrue(self.metadata()["plan"]["session"]["access"]["project"])
        self.stop(record)
        self.settings()
        view = HarnessDelegate.attach(self.work)
        launches, starts = len(RecordingHost.launches), len(HarnessSession.started)
        with self.assertRaises(ConfigError) as caught:
            view.resume()
        self.assertEqual(str(caught.exception), f"cwd {project} is no longer in trusted_dirs")
        self.assertEqual(
            (len(RecordingHost.launches), len(HarnessSession.started)), (launches, starts)
        )

    def test_a_completed_foreground_tui_left_open_is_not_alive(self):
        spec = Spec("input", self.work, task="echo", sid=SID, client="fake", mode="tui")
        delegate = self.make(spec=spec)
        self.assertEqual(delegate.run().outcome.status, "done")
        agent = HarnessSession.attach(f"dummy-tester-{SID[:8]}")
        self.assertEqual(agent.alive(), True)
        self.assertEqual(
            (delegate.alive(), HarnessDelegate.attach(self.work).alive()), (False, False)
        )

    def test_a_completed_object_refuses_a_second_run_keeping_its_result(self):
        delegate = self.make()
        delegate.run()
        path = self.state_record()
        with open(path, "rb") as file:
            before = file.read()
        starts = len(HarnessSession.started)
        with self.assertRaises(ConfigError) as caught:
            delegate.run()
        self.assertEqual(str(caught.exception), f"{self.work}: delegate run already completed")
        with open(path, "rb") as file:
            self.assertEqual((file.read(), len(HarnessSession.started)), (before, starts))

    def test_a_completed_object_refuses_to_detach_launching_nothing(self):
        delegate = self.make()
        delegate.run()
        launched = list(RecordingHost.launches)
        with self.assertRaises(ConfigError) as caught:
            delegate.detach()
        self.assertEqual(str(caught.exception), f"{self.work}: delegate run already completed")
        self.assertEqual(RecordingHost.launches, launched)

    def test_a_run_whose_session_was_never_recorded_is_not_resumable(self):
        delegate = self.make()
        with mock.patch.object(
            HarnessSession, "inspect", side_effect=NoRecordedSession("no recorded session")
        ):
            self.assertEqual(
                HarnessDelegate.attach(self.work).session_status(), SessionStatus("no", None, "")
            )
        self.assertFalse(delegate.alive())

    def test_completed_session_status_uses_public_history_without_starting(self):
        delegate = self.make()
        delegate.run()
        delegate.close()
        count = len(HarnessSession.started)
        view = HarnessDelegate.attach(self.work)
        self.assertEqual(view.session_status(), HarnessSession.history)
        self.assertEqual(len(HarnessSession.started), count)
        self.assertEqual(HarnessSession.inspected[-1].sid, SID)

    def test_valid_outcome_resume_does_nothing_for_each_terminal_status(self):
        for status in ("done", "needs_input", "failed"):
            with self.subTest(status=status):
                self.work = os.path.join(self.home, status)
                data: dict = dict(DONE, status=status)
                if status == "needs_input":
                    data["questions"] = ["Continue?"]
                HarnessSession.script = (Reply(block(data)), Exited(0))
                delegate = self.make()
                result = delegate.run()
                self.assertEqual(result.outcome, Outcome(**data, url=Layout(self.work).out))
                view = HarnessDelegate.attach(self.work)
                starts = len(HarnessSession.started)
                launches = len(RecordingHost.launches)
                first = view.resume()
                self.assertEqual(view.resume(), first)
                self.assertEqual(
                    (len(HarnessSession.started), len(RecordingHost.launches)), (starts, launches)
                )

    def test_latest_interrupted_attempt_does_not_reuse_an_earlier_outcome(self):
        first = self.make()
        first.run()
        first.close()
        self.make()
        view = HarnessDelegate.attach(self.work)
        self.assertIsNone(view.outcome())
        record = view.resume()
        self.assertTrue(view.alive())
        self.assertEqual(view.resume(), record)
        self.assertEqual(self.metadata()["attempt"], 3)

    def test_resume_uses_attach_config_snapshot(self):
        team = self.team_dir()
        self.settings(team_dirs=(team,))
        self.make(spec=Spec("input", self.work, task="echo", client="fake", sid=SID))
        self.settings(language="French", team_dirs=(team,))
        view = HarnessDelegate.attach(self.work)
        self.settings(
            language="German", client="changed-client", default="changed", team_dirs=(team,)
        )
        view.resume()
        view.close()
        view.run()
        launched = HarnessSession.started[-1]["spec"]
        self.assertEqual((launched.sid, launched.client), (SID, "fake"))
        self.assertIn("French", launched.prompt)
        self.assertNotIn("German", launched.prompt)


class UsageQueries(HarnessCase):
    def test_role_client_usage_preserves_sentinel_identity_and_none_without_starting(self):
        self.settings(client="role_usage")
        with open(self.local, "a", encoding="utf-8") as file:
            file.write(
                "[session.clients.role_usage]\n"
                'implementation = "tests.delegate.fakes.usage:UsageClient"\n'
                'label = "role_usage"\n'
                f"cwd = {json.dumps(self.home)}\n"
            )
        result = subprocess.run(
            [sys.executable, "-P", "-S", "-c", USAGE_QUERY],
            cwd=self.home,
            env=child_env(None, REPO),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "queried": ["role_usage", "role_usage"],
                "probes": [1, 1],
                "starts": [],
                "session_starts": [],
                "launches": [],
            },
        )

    def test_usage_configures_and_asks_only_the_given_session_class(self):
        sentinel = Usage(Window(12.5, 1767225600.0), Window(37.0, None))
        asked = []

        class Probe(SessionClass):
            @classmethod
            def usage(cls, client):
                asked.append(client)
                return sentinel

        self.assertIs(HarnessDelegate.usage("dummy-tester", session_cls=Probe), sentinel)
        self.assertEqual(asked, ["fake"])
        self.assertEqual(
            sorted(Probe.configured["hosts"].tables),
            ["agent", "changed", "docker", "harness", "tmux"],
        )

    def test_session_config_errors_are_the_delegates(self):
        class Refusing(SessionClass):
            @classmethod
            def usage(cls, client):
                raise SessionConfigError(f"[clients.{client}]: no usage probe")

        with self.assertRaises(ConfigError) as caught:
            HarnessDelegate.usage("dummy-tester", session_cls=Refusing)
        self.assertEqual(str(caught.exception), "[clients.fake]: no usage probe")
        self.settings(default="nope")
        with self.assertRaises(ConfigError) as caught:
            HarnessDelegate.usage("dummy-tester", session_cls=SessionClass)
        self.assertEqual(
            str(caught.exception),
            "[hosts] default: 'nope' is not a Host here; "
            "want one of agent, changed, docker, harness, tmux",
        )


class Completion(HarnessCase):
    def test_foreground_headless_owns_live_session_and_publishes_result_before_finished(self):
        HarnessSession.script = (Reply(mark("start", "working")), Reply(block(DONE)), Exited(0))
        delegate = self.make()
        seen = []

        def observe(event):
            seen.append(event)
            if isinstance(event, (Begun, Progress)):
                self.assertTrue(delegate.alive())
                self.assertEqual(delegate.session_status(), HarnessSession.history)
                self.assertEqual(HarnessSession.inspected, [])
                self.assertIsNone(delegate.outcome())
            else:
                self.assertFalse(delegate.alive())
                self.assertEqual(delegate.outcome(), event.result.outcome)
                view = HarnessDelegate.attach(self.work)
                self.assertFalse(view.alive())
                self.assertEqual(view.outcome(), event.result.outcome)

        result = delegate.run(observe)
        self.assertEqual([type(event) for event in seen], [Begun, Progress, Finished])
        self.assertEqual(seen[-1], Finished(result))
        self.assertEqual(HarnessSession.started[0]["spec"].sid, SID)

    def test_headless_keeps_display_for_harness_and_passes_none_to_agent(self):
        delegate = self.make(display="opaque", host="agent", harness_host="harness")
        record = delegate.detach()
        self.assertEqual(record.display, "opaque")
        self.assertEqual(RecordingHost.launches[0]["display"], "opaque")
        self.stop(record)
        delegate.run()
        self.assertIsNone(HarnessSession.started[0]["display"])
        self.assertEqual(self.metadata()["display"], "opaque")

    def test_finished_callback_queries_the_completed_result_before_run_returns(self):
        delegate = self.make()
        finished = []

        def observe(event):
            if isinstance(event, Finished):
                finished.append(event)
                view = HarnessDelegate.attach(self.work)
                self.assertFalse(view.alive())
                self.assertEqual(view.outcome(), event.result.outcome)
                self.assertEqual(view.status().outcome, "done")

        result = delegate.run(observe)
        self.assertEqual(finished, [Finished(result)])

    def test_every_returned_result_has_one_last_finished_event(self):
        for script in (
            (Reply(block(DONE)), Exited(0)),
            (Reply("no outcome"), Exited(0)),
            (Exited(7),),
            (LimitHit(1767225600.0), Exited(0)),
        ):
            with self.subTest(script=script):
                self.work = os.path.join(self.home, f"completion-{len(HarnessSession.started)}")
                HarnessSession.script = script
                seen = []
                result = self.make().run(seen.append)
                self.assertIsInstance(seen[0], Begun)
                self.assertEqual(
                    [event for event in seen if isinstance(event, Finished)], [Finished(result)]
                )
                self.assertEqual(seen[-1], Finished(result))
                if isinstance(script[0], LimitHit):
                    self.assertIsNone(result.outcome)
                    self.assertEqual(result.limit, Limit(1767225600.0))
                    self.assertFalse(HarnessSession.live)

    def test_spawn_failure_is_finished_without_begun(self):
        FailingSession.reset()
        delegate = HarnessDelegate(
            "dummy-tester",
            Spec("input", self.work, client="fake"),
            session_cls=FailingSession,
        )
        seen = []
        result = delegate.run(seen.append)
        self.assertEqual(result.returncode, 1)
        self.assertIn("fake spawn refused", result.error)
        self.assertEqual(seen, [Finished(result)])

    def test_on_event_exception_keeps_original_exception_and_stops_session(self):
        failure = RuntimeError("callback failure")
        seen = []

        def callback(event):
            seen.append(event)
            raise failure

        delegate = self.make()
        with self.assertRaises(RuntimeError) as caught:
            delegate.run(callback)
        self.assertIs(caught.exception, failure)
        self.assertEqual(len(seen), 1)
        self.assertIsInstance(seen[0], Begun)
        self.assertFalse(HarnessSession.live)


class CountingDisplay(HarnessSession):
    calls: list = []

    @classmethod
    def display(cls, value=None, *, host=None):
        cls.calls.append((value, host))
        return f"completed:{value}"


class ValidatingHost(RecordingHost):
    validated: list = []

    def validate(self, name, launch):
        super().validate(name, launch)
        ValidatingHost.validated.append((self.name, name, launch))
        if not name.replace("-", "").replace("_", "").isalnum():
            raise SessionConfigError(f"session name {name!r}: want [A-Za-z0-9_-]+")


class FailingHost(RecordingHost):
    failure: Exception = SessionError("detach refused")

    def detach(self, name, launch, *, display=None, events=None, record=None):
        raise FailingHost.failure


class RefusedEvents(RecordingHost):
    def events_path(self, events):
        raise SessionConfigError(f"events file {events}: tmux would misread it")


class RootedSession(HarnessSession):
    asked: list = []
    failure: Exception | None = None

    @classmethod
    def class_roots(cls, client):
        cls.asked.append(client)
        if cls.failure is not None:
            raise cls.failure
        return ("/extra/root", SRC["session"])


class HarnessLaunch(HarnessCase):
    def test_pythonpath_is_the_harness_import_roots_not_the_callers(self):
        RootedSession.asked, RootedSession.failure = [], None
        with mock.patch.dict(os.environ, {"PYTHONPATH": "/caller/only"}):
            self.make(session_cls=RootedSession, host="agent", harness_host="harness").detach()
        roots = (SRC["delegate"], REPO, "/extra/root")
        self.assertEqual(RecordingHost.launches[0]["env"]["PYTHONPATH"], os.pathsep.join(roots))
        self.assertEqual(RootedSession.asked[-1], "fake")

    def test_an_unloadable_class_is_refused_writing_nothing(self):
        RootedSession.asked, RootedSession.failure = [], SessionConfigError("[clients.fake]: no x")
        with self.assertRaises(ConfigError) as caught:
            self.make(session_cls=RootedSession)
        self.assertEqual(str(caught.exception), "[clients.fake]: no x")
        self.assertFalse(os.path.lexists(self.work))

    def test_the_harness_runs_in_the_run_dir_with_its_signals_blocked(self):
        project = os.path.join(self.home, "project")
        os.mkdir(project)
        spec = Spec("input", self.work, client="fake", sid=SID, cwd=project)
        self.make(spec=spec).detach()
        launch = RecordingHost.launches[0]
        self.assertEqual(
            (launch["cwd"], launch["blocked_signals"]), (Layout(self.work).run, SIGNALS)
        )
        self.assertEqual(
            launch["argv"],
            [sys.executable, "-P", "-s", "-m", "ensemble.delegate.engine.harness", self.work],
        )


class DeadOnArrival(RecordingHost):
    def detach(self, name, launch, **kw):
        process = super().detach(name, launch, **kw)
        process.live = False
        return process


class TuiStart(HarnessCase):
    def tui(self, **placement):
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        return self.make(spec=spec, host="agent", **placement)

    def test_detach_returns_once_the_agent_session_is_live(self):
        record = self.tui(harness_host="harness").detach()
        agent = HarnessSession.live[record.sessions[1]]
        self.assertEqual(agent.alive(), True)
        self.assertTrue(HarnessDelegate.attach(record).alive())

    def test_a_harness_ending_first_fails_the_detach_and_stays_resumable(self):
        RecordingHost.harness_starts_agent = False
        delegate = self.tui(harness_host=DeadOnArrival("harness"))
        with self.assertRaises(SessionError) as caught:
            delegate.detach()
        name = f"dummy-tester-{SID[:8]}"
        self.assertEqual(
            str(caught.exception), f"tui session {name} did not start: its harness ended"
        )
        self.assertNotIn(name, HarnessSession.live)
        self.assertTrue(self.metadata()["detached"])
        RecordingHost.harness_starts_agent = True
        resumed = HarnessDelegate.attach(self.work).resume()
        self.assertEqual(HarnessSession.live[resumed.sessions[1]].alive(), True)

    def test_a_timeout_closes_the_harness_and_fails_the_detach(self):
        RecordingHost.harness_starts_agent = False
        delegate = self.tui(harness_host="harness")
        clock = iter((0.0, 1.0, 31.0))
        with (
            mock.patch("time.monotonic", side_effect=lambda: next(clock)),
            mock.patch("time.sleep") as slept,
            self.assertRaises(SessionError),
        ):
            delegate.detach()
        self.assertEqual(slept.call_count, 1)
        harness = RecordingHost.processes["harness", f"dummy-tester-{SID[:8]}-drive"]
        self.assertFalse(harness.alive())

    def test_an_unrelated_live_tui_of_the_same_name_does_not_stand_for_this_run(self):
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID, name="X")
        delegate = self.make(spec=spec, host="agent", harness_host="harness")
        other = "22222222-0000-4000-8000-000000000000"
        HarnessSession.start(
            SessionSpec(
                workdir=self.home,
                access=Access(cwd=self.home),
                client="fake",
                mode=Mode.TUI,
                sid=other,
                name="X",
            ),
            host="agent",
        )
        RecordingHost.harness_starts_agent = False
        clock = iter((0.0, 1.0, 31.0))
        with (
            mock.patch("time.monotonic", side_effect=lambda: next(clock)),
            mock.patch("time.sleep"),
            self.assertRaises(SessionError) as caught,
        ):
            delegate.detach()
        self.assertEqual(str(caught.exception), "tui session X did not start in 30 s")
        self.assertEqual([launch["name"] for launch in harness_launches()], ["X-drive"])
        self.assertEqual(HarnessSession.live["X"].sid, other)
        self.assertEqual(HarnessSession.live["X"].alive(), True)

    def test_a_headless_detach_waits_for_nothing(self):
        RecordingHost.harness_starts_agent = False
        with mock.patch("time.sleep", side_effect=AssertionError("waited")):
            record = self.make(host="agent", harness_host="harness").detach()
        self.assertEqual(len(record.sessions), 1)


class Announce(HarnessCase):
    def run_with_events(self):
        self.events = os.path.join(self.home, "events")
        spec = Spec("input", self.work, client="fake", sid=SID, events=self.events)
        return self.make(spec=spec)

    def test_one_outcome_line_for_the_harness_session_per_result(self):
        delegate = self.run_with_events()
        asking = Outcome(**{**DONE, "status": "needs_input", "questions": ["Which?"]})
        for result in (
            Result(0, Outcome(**DONE)),
            Result(0, asking),
            Result(1, None, "no outcome"),
            None,
        ):
            delegate.announce(result)
        with open(self.events, encoding="utf-8") as file:
            lines = file.read().splitlines()
        stamp, name = "[0-9]{2}:[0-9]{2}:[0-9]{2}", f"dummy-tester-{SID[:8]}-drive"
        for line, status in zip(lines, ("done", "needs_input", "error", "error"), strict=True):
            self.assertRegex(line, f"^{stamp} {name} outcome {status}$")

    def test_a_write_error_is_printed_not_raised(self):
        delegate = self.run_with_events()
        os.mkdir(self.events)
        err = io.StringIO()
        with redirect_stderr(err):
            delegate.announce(Result(0, Outcome(**DONE)))
        self.assertTrue(err.getvalue().startswith("delegate: "), err.getvalue())

    def test_a_run_without_events_writes_no_line(self):
        self.make().announce(None)
        self.assertCountEqual(os.listdir(self.home), [".ensemble", "run"])


class Names(HarnessCase):
    def test_the_agent_is_name_and_the_harness_name_drive_in_every_mode(self):
        default = f"dummy-tester-{SID[:8]}"
        for mode, name in (
            ("headless", None),
            ("tui", None),
            ("headless", "base"),
            ("tui", "base"),
        ):
            with self.subTest(mode=mode, name=name):
                HarnessSession.reset()
                self.work = os.path.join(self.home, f"{mode}-{name}")
                spec = Spec("input", self.work, mode=mode, client="fake", sid=SID, name=name)
                record = self.make(spec=spec, host="agent", harness_host="harness").detach()
                want = name or default
                self.assertEqual(record.name, want)
                self.assertEqual(
                    record.sessions, (f"{want}-drive",) + ((want,) if mode == "tui" else ())
                )
                self.assertEqual(
                    (self.metadata()["agent_name"], self.metadata()["harness_name"]),
                    (want, f"{want}-drive"),
                )
                self.assertEqual(RecordingHost.launches[0]["name"], f"{want}-drive")

    def test_the_harness_host_validates_the_harness_name_and_launch_at_construction(self):
        ValidatingHost.validated = []
        HarnessSession.hosts["harness"] = ValidatingHost("harness")
        self.make(host="agent", harness_host="harness")
        ((host, name, launch),) = ValidatingHost.validated
        self.assertEqual((host, name), ("harness", f"dummy-tester-{SID[:8]}-drive"))
        self.assertEqual(launch.cwd, Layout(self.work).run)
        self.assertEqual(RecordingHost.launches, [])

    def test_an_invalid_name_is_refused_writing_nothing(self):
        HarnessSession.hosts["harness"] = ValidatingHost("harness")
        for mode in ("headless", "tui"):
            with self.subTest(mode=mode):
                spec = Spec("input", self.work, mode=mode, client="fake", sid=SID, name="bad name")
                with self.assertRaises(ConfigError) as caught:
                    self.make(spec=spec, host="agent", harness_host="harness")
                self.assertIn("bad name", str(caught.exception))
                self.assertFalse(os.path.lexists(self.state_record()))
                self.assertFalse(os.path.lexists(self.work))

    def test_resume_reuses_the_recorded_names(self):
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID, name="base")
        record = self.make(spec=spec, host="agent", harness_host="harness").detach()
        self.stop(record)
        resumed = HarnessDelegate.attach(self.work).resume()
        self.assertEqual((resumed.name, resumed.sessions), ("base", ("base-drive", "base")))
        self.assertEqual(harness_launches()[-1]["name"], "base-drive")


class Outer(HarnessCase):
    OUTER = {
        "kind": "pipeline",
        "ctl": ("ensemble-orchestrator",),
        "resume": ("ensemble-orchestrator", "resume", "TASK-1"),
    }

    def test_the_default_record_is_a_role_run_answered_by_ensemble_delegate(self):
        record = self.make().detach()
        self.assertEqual(
            (record.kind, record.detail, record.ctl, record.resume),
            (
                "role",
                "dummy-tester",
                ("ensemble-delegate",),
                ("ensemble-delegate", "resume", self.work),
            ),
        )

    def test_outer_is_the_hosts_record_and_resumes_record(self):
        record = self.make(host="agent", harness_host="harness").detach(outer=self.OUTER)
        self.assertEqual((record.kind, record.ctl, record.resume), tuple(self.OUTER.values()))
        self.assertEqual(
            self.metadata()["outer"],
            {k: list(v) if k != "kind" else v for k, v in self.OUTER.items()},
        )
        harness = HarnessSession.hosts["harness"].attach(record.sessions[0])
        self.assertEqual(harness.record(), record)
        self.stop(record)
        resumed = HarnessDelegate.attach(self.work).resume()
        self.assertEqual((resumed.kind, resumed.ctl, resumed.resume), tuple(self.OUTER.values()))
        self.assertEqual(RecordingHost.launches[-1]["record"], resumed)

    def test_an_unknown_or_invalid_outer_launches_nothing(self):
        for outer, message in (
            ({"kind": "crew", "extra": 1}, "outer: unknown key extra"),
            ({"ctl": ("/x/ensemble-crew",)}, "ctl"),
            ({"ctl": ("ensemble-crew",), "resume": ("ensemble-crew", "resume", "r")}, "ctl"),
            ({"resume": ("ensemble-delegate", "resume", "--help")}, "resume"),
        ):
            with self.subTest(outer=outer):
                delegate = self.make()
                with self.assertRaises(ConfigError) as caught:
                    delegate.detach(outer=outer)
                self.assertIn(message, str(caught.exception))
                self.assertEqual(RecordingHost.launches, [])
                self.assertFalse(self.metadata()["detached"])


class Display(HarnessCase):
    def test_completed_once_in_the_caller_and_passed_unchanged(self):
        CountingDisplay.calls = []
        CountingDisplay.reset()
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        record = HarnessDelegate(
            "dummy-tester",
            spec,
            session_cls=CountingDisplay,
            host="agent",
            harness_host="harness",
            display="given",
        ).detach()
        self.assertEqual(CountingDisplay.calls, [("given", "agent")])
        self.assertEqual((self.metadata()["display"], record.display), ("completed:given",) * 2)
        self.assertEqual(RecordingHost.launches[0]["display"], "completed:given")
        self.assertEqual(CountingDisplay.started[-1]["display"], "completed:given")
        CountingDisplay.live[record.sessions[1]].close()
        HarnessDelegate.attach(self.work, session_cls=CountingDisplay).run()
        self.assertEqual(CountingDisplay.started[-1]["display"], "completed:given")
        self.stop(record)
        HarnessDelegate.attach(self.work, session_cls=CountingDisplay).resume()
        self.assertEqual(harness_launches()[-1]["display"], "completed:given")
        self.assertEqual(CountingDisplay.calls, [("given", "agent")])

    def test_a_headless_agent_starts_without_a_display(self):
        CountingDisplay.reset()
        HarnessDelegate(
            "dummy-tester",
            Spec("input", self.work, client="fake", sid=SID),
            session_cls=CountingDisplay,
            display="given",
        ).run()
        self.assertIsNone(CountingDisplay.started[-1]["display"])


class ConcurrentDetach(HarnessCase):
    def test_two_processes_launch_one_harness(self):
        code = (
            "import os, sys, time\n"
            "from ensemble.delegate import ConfigError, HarnessDelegate, Spec\n"
            "from tests.delegate.fakes.harness_session import PersistentSession\n"
            "spec = Spec('input', sys.argv[1], client='fake', sid=sys.argv[4])\n"
            "delegate = HarnessDelegate('dummy-tester', spec, session_cls=PersistentSession,\n"
            "                           host='agent', harness_host='harness')\n"
            "open(sys.argv[2] + str(os.getpid()), 'w').close()\n"
            "while not os.path.exists(sys.argv[3]): time.sleep(0.01)\n"
            "try:\n"
            "    print(delegate.detach().to_line())\n"
            "except ConfigError as error:\n"
            "    print('refused:', error)\n"
        )
        ready, gate = os.path.join(self.home, "ready-"), os.path.join(self.home, "go")
        children = []
        for _ in range(2):
            children.append(
                subprocess.Popen(
                    [sys.executable, "-P", "-S", "-c", code, self.work, ready, gate, SID],
                    cwd=self.home,
                    env=child_env(None, REPO),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    encoding="utf-8",
                )
            )
            self.addCleanup(_close_child, children[-1])
            deadline = time.monotonic() + 10
            while not os.path.exists(ready + str(children[-1].pid)):
                self.assertLess(time.monotonic(), deadline, "child did not construct")
                time.sleep(0.01)
        with open(gate, "w", encoding="utf-8"):
            pass
        outputs = []
        for child in children:
            stdout, stderr = child.communicate(timeout=10)
            self.assertEqual(child.returncode, 0, stderr)
            outputs.append(stdout.strip())
        refused = [line for line in outputs if line.startswith("refused:")]
        self.assertEqual(len(refused), 1, outputs)
        self.assertIn(
            refused[0].split(": ", 2)[-1],
            ("delegate attempt was replaced", "delegate is still alive"),
        )
        self.assertEqual(Record.parse(next(o for o in outputs if o not in refused)).sid, SID)
        path = os.path.join(self.ensemble, "harness-fake", "launches.jsonl")
        with open(path, encoding="utf-8") as file:
            self.assertEqual(len(file.readlines()), 1)


class RunDir(HarnessCase):
    def test_new_run_lays_out_agent_and_run_dirs(self):
        self.make()
        layout = Layout(self.work)
        self.assertEqual(sorted(os.listdir(self.work)), ["agent", "run"])
        self.assertTrue(os.path.isdir(layout.agent) and os.path.isdir(layout.run))
        self.assertEqual(self.state_record(), os.path.join(layout.run, "delegate.json"))
        self.assertEqual(stat.S_IMODE(os.stat(self.state_record()).st_mode), 0o600)

    def test_run_files_outside_agent_dir_are_all_under_run_dir(self):
        log = os.path.join(self.home, "callback.log")
        callback = [sys.executable, "-P", "-S", callback_recorder.__file__, log]
        self.make(callback=callback).run()
        layout = Layout(self.work)
        outside = {
            os.path.relpath(os.path.join(root, name), self.work)
            for root, dirs, files in os.walk(self.work)
            if not (root + os.sep).startswith(layout.agent + os.sep)
            for name in files
        }
        self.assertEqual(
            outside,
            {
                os.path.join("run", name)
                for name in (
                    "delegate.json",
                    "delegate.lock",
                    "delegate.run.lock",
                    "delegate.callback.lock",
                    "run.jsonl",
                    "out.md",
                )
            },
        )
        with open(os.path.join(layout.run, "run.jsonl"), encoding="utf-8") as file:
            kinds = {json.loads(line)["kind"] for line in file}
        self.assertTrue({"input", "result", "end"} <= kinds, kinds)
        self.assertEqual(HarnessSession.started[-1]["spec"].workdir, layout.run)

    def test_no_state_dir_under_home(self):
        self.make().run()
        self.make(host="agent", harness_host="harness").detach()
        self.assertFalse(os.path.lexists(os.path.join(self.ensemble, "delegates")))
        records = [
            os.path.join(root, name)
            for root, _dirs, files in os.walk(self.home)
            for name in files
            if name == "delegate.json"
        ]
        self.assertEqual(records, [self.state_record()])

    def test_a_new_run_after_its_agent_dir_was_pruned_counts_the_next_attempt(self):
        self.make().run()
        shutil.rmtree(Layout(self.work).agent)
        self.assertEqual(self.make().run().returncode, 0)
        self.assertEqual(self.metadata()["attempt"], 2)
        self.assertTrue(os.path.isdir(Layout(self.work).agent))

    def test_reads_take_the_run_record_not_one_planted_in_the_agent_dir(self):
        self.make().run()
        with open(
            os.path.join(Layout(self.work).agent, "delegate.json"), "w", encoding="utf-8"
        ) as file:
            file.write("edited by the agent")
        view = HarnessDelegate.attach(self.work)
        self.assertEqual(view.outcome().status, "done")
        self.assertEqual(self.make().run().returncode, 0)

    def test_a_refused_construction_writes_no_state_record(self):
        with self.assertRaises(ConfigError):
            self.make(role="absent-role")
        self.assertFalse(os.path.lexists(self.state_record()))
        self.assertFalse(os.path.lexists(self.work))

    def test_a_foreground_run_holds_the_run_lock_against_a_new_construction(self):
        delegate = self.make()
        seen = []

        def construct(event):
            if isinstance(event, Begun):
                with self.assertRaises(ConfigError) as caught:
                    self.make()
                seen.append(str(caught.exception))

        delegate.run(construct)
        self.assertEqual(seen, [f"{self.work}: delegate is still alive"])
        self.assertEqual(self.make().run().returncode, 0)
        self.assertEqual(self.metadata()["attempt"], 2)


class Corrupt(HarnessCase):
    def corrupt(self, text):
        self.make()
        with open(self.state_record(), "w", encoding="utf-8") as file:
            file.write(text)

    def test_a_corrupt_record_is_replaced_keeping_a_readable_attempt(self):
        for text, attempt in (
            ("not json", 1),
            ("[1]", 1),
            ('{"version": 2, "attempt": 4}', 5),
            ('{"version": 1, "attempt": 6}', 7),
            ('{"attempt": true}', 1),
            ('{"attempt": 0}', 1),
            ('{"version": 1, "attempt": 2, "spec": null}', 3),
            ('{"version": 1, "attempt": 3, "spec": "x"}', 4),
        ):
            with self.subTest(text=text):
                self.work = os.path.join(self.home, f"corrupt-{attempt}-{len(text)}")
                self.corrupt(text)
                self.make()
                self.assertEqual(self.metadata()["attempt"], attempt)

    def test_attach_by_workdir_or_record_refuses_a_corrupt_record(self):
        self.corrupt('{"version": 2}')
        for call in (
            lambda: HarnessDelegate.attach(self.work),
            lambda: HarnessDelegate.attach(Record.parse(self.record_line())),
        ):
            with self.subTest(call=call), self.assertRaises(ConfigError) as caught:
                call()
            self.assertIn(self.work, str(caught.exception))

    def record_line(self):
        return Record(
            1,
            "role",
            "n",
            (),
            SID,
            self.work,
            ("ensemble-delegate",),
            ("ensemble-delegate", "resume", self.work),
            None,
            None,
        ).to_line()

    def test_a_held_run_lock_keeps_a_corrupt_record(self):
        self.corrupt("not json")
        with RunState(Layout(self.work)).run_lock(), self.assertRaises(ConfigError) as caught:
            self.make()
        self.assertEqual(str(caught.exception), f"{self.work}: delegate is still alive")
        with open(self.state_record(), encoding="utf-8") as file:
            self.assertEqual(file.read(), "not json")

    def test_a_live_harness_named_by_a_corrupt_record_keeps_it(self):
        HarnessSession.hosts["harness"].detach("x-drive", Launch(("x",), cwd=self.home))
        self.corrupt('{"harness_host": "harness", "harness_name": "x-drive", "attempt": 2}')
        with self.assertRaises(ConfigError) as caught:
            self.make()
        self.assertEqual(str(caught.exception), f"{self.work}: delegate is still alive")
        RecordingHost.processes["harness", "x-drive"].close()
        self.make()
        self.assertEqual(self.metadata()["attempt"], 3)

    def test_an_unknown_harness_host_in_a_corrupt_record_is_refused(self):
        self.corrupt('{"harness_host": "ghost", "harness_name": "x-drive"}')
        ConfiguredSession.reset()
        with self.assertRaises(ConfigError) as caught:
            self.make(session_cls=ConfiguredSession)
        self.assertEqual(str(caught.exception), "unknown host ghost")
        with open(self.state_record(), encoding="utf-8") as file:
            self.assertEqual(json.load(file)["harness_host"], "ghost")


class Attempts(HarnessCase):
    def test_resume_counts_an_attempt_and_its_run_stamps_it(self):
        log = os.path.join(self.home, "callback.log")
        callback = [sys.executable, "-P", "-S", callback_recorder.__file__, log]
        record = self.make(callback=callback).detach()
        self.assertEqual(self.metadata()["attempt"], 1)
        self.stop(record)
        view = HarnessDelegate.attach(self.work)
        view.resume()
        self.assertEqual(self.metadata()["attempt"], 2)
        view.resume()
        self.assertEqual(self.metadata()["attempt"], 2)
        HarnessDelegate.attach(self.work).run()
        with open(log, encoding="utf-8") as file:
            lines = [json.loads(entry)["line"] for entry in file]
        stamps = {(event.sid, event.attempt) for event in map(DelegateEvent.parse, lines)}
        self.assertEqual(stamps, {(SID, 2)})


class LiveNameSession(HarnessSession):
    """Refuses, as the real preflight does, a session whose name a live one holds."""

    @classmethod
    def preflight(cls, spec, **placement):
        super().preflight(spec, **placement)
        if spec.name in cls.live:
            raise SessionError(f"tui session {spec.name} is running")


class Answer(HarnessCase):
    def test_an_answer_closes_the_ended_runs_open_tui_and_detaches_its_next_attempt(self):
        LiveNameSession.reset()
        tui = Spec("Hello.", self.work, client="fake", sid=SID, mode="tui")
        placed = {"host": "agent", "harness_host": "harness", "session_cls": LiveNameSession}
        record = self.make(spec=tui, **placed).detach()
        harness, agent = (
            RecordingHost.processes[host, name]
            for host, name in zip(("harness", "agent"), record.sessions, strict=True)
        )
        answer = replace(tui, input="Use option B.", resume=True)
        with self.assertRaisesRegex(ConfigError, "delegate is still alive"):
            self.make(spec=answer, **placed)
        self.assertTrue(agent.alive())
        self.assertEqual(self.metadata()["attempt"], 1)
        harness.close()
        self.assertEqual(self.make(spec=answer, **placed).detach(), record)
        self.assertFalse(agent.alive())
        state = self.metadata()
        session = state["plan"]["session"]
        self.assertEqual((state["attempt"], session["sid"], session["resume"]), (2, SID, True))
        self.assertTrue(session["prompt"].endswith("\nUse option B.\n"), session["prompt"])


class ConcurrentResume(HarnessCase):
    def test_two_processes_resume_once_and_return_the_same_record(self):
        delegate = HarnessDelegate(
            "dummy-tester",
            Spec("input", self.work, client="fake", sid=SID),
            session_cls=PersistentSession,
            host="agent",
            harness_host="harness",
            display="opaque",
        )
        code = (
            "import os, sys, time\n"
            "from ensemble.delegate import HarnessDelegate\n"
            "open(sys.argv[2] + str(os.getpid()), 'w').close()\n"
            "while not os.path.exists(sys.argv[3]): time.sleep(0.01)\n"
            "print(HarnessDelegate.attach(sys.argv[1]).resume().to_line())\n"
        )
        ready, gate = os.path.join(self.home, "ready-"), os.path.join(self.home, "go")
        children = [
            subprocess.Popen(
                [sys.executable, "-P", "-S", "-c", code, self.work, ready, gate],
                cwd=self.home,
                env=child_env(None, REPO),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
            )
            for _ in range(2)
        ]
        for child in children:
            self.addCleanup(_close_child, child)
        deadline = time.monotonic() + 10
        while not all(os.path.exists(ready + str(child.pid)) for child in children):
            self.assertLess(time.monotonic(), deadline, "resume children did not reach barrier")
            time.sleep(0.01)
        with open(gate, "w", encoding="utf-8"):
            pass
        records = []
        for child in children:
            stdout, stderr = child.communicate(timeout=10)
            self.assertEqual(child.returncode, 0, stderr)
            records.append(Record.parse(stdout))
        self.assertEqual(records[0], records[1])
        self.assertEqual((records[0].sid, records[0].display), (SID, "opaque"))
        path = os.path.join(self.ensemble, "harness-fake", "launches.jsonl")
        with open(path, encoding="utf-8") as file:
            self.assertEqual(len(file.readlines()), 1)
        self.assertTrue(delegate.alive())
