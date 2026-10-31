"""modes/headless.py: its start from a Spec (checks, one session line, then its Host's headless
worker for the launch's command with the child env; start failures), send and screen refused, over
a scripted worker that starts no process. Real children:
tests/session/integration/modes/headless_integration_test.py.
"""

import contextlib
import errno
import json
import os
import queue
import sys
import threading
import time
import unittest
from typing import ClassVar
from unittest import mock

from ensemble.session import SESSION_NAME_RULE
from ensemble.session.clients.base import Client
from ensemble.session.engine.types import (
    Access,
    Blocked,
    ClientContext,
    ClientHome,
    ConfigError,
    Container,
    Launch,
    LimitHit,
    Liveness,
    Permission,
    Prepared,
    Reply,
    SessionError,
    SessionInfo,
    Spec,
    Stderr,
    TurnEnd,
    TurnFailed,
    TurnStarted,
    WorkerOutput,
)
from ensemble.session.modes.base import POINTER
from ensemble.session.modes.headless import LINE_MAX, QUEUE_MAX, HeadlessMode
from ensemble.utils import ACCOUNT_ENV as ACCOUNT
from tests.session.case import PLUGIN_BIN
from tests.session.fakes.terminal import MemoryHost
from tests.session.fakes.worker import ScriptedWorker
from tests.session.headless_support import (
    GENERATION,
    NOTHING,
    SID,
    HeadlessCase,
    StubClient,
    StubRun,
)


class NoHeadless(StubClient):
    def headless(self):
        return None


class IdentityClient(Client):
    identity_env = ("HM_ID",)

    def headless(self):
        return None

    def tui(self):
        return None


class FailingRun(StubRun):
    def launch(self, spec: Spec):
        raise LookupError("no binding")


class FailingCommitRun(StubRun):
    def commit(self):
        raise LookupError("cannot persist")


class UnstreamedRun(StubRun):
    def events(self, lines):
        raise self.client.failure


class Base(HeadlessCase):
    @contextlib.contextmanager
    def faked(self, error=None):
        """Each headless worker its Host starts: its (launch, session lines by then) in the list;
        a scripted worker that exits at once, or `error` raised.
        """
        calls = []

        def worker(_host, name, launch, metadata):
            calls.append((launch, len(self.recorded)))
            if error is not None:
                raise error
            return ScriptedWorker(name, metadata["generation"])

        with mock.patch.object(MemoryHost, "headless_worker", autospec=True, side_effect=worker):
            yield calls


class GenerationTest(Base):
    def test_a_worker_with_another_generation_fails_the_start(self):
        other = ScriptedWorker("w", "e" * 32, code=None)
        with (
            mock.patch.object(MemoryHost, "headless_worker", return_value=other),
            self.assertRaisesRegex(
                SessionError, f"host memory: worker w generation {'e' * 32}, want {'f' * 32}"
            ),
        ):
            self.start()
        self.assertEqual(other.closes, 1)

    def test_the_admission_generation_names_the_worker_and_wins_over_host_metadata(self):
        with self.faked():
            self.start()
        self.assertEqual(self.mode.generation, "f" * 32)
        self.assertEqual(self.mode.worker.name, f"headless-{'f' * 32}")


class TuiOnly(Base):
    def test_send_and_screen_raise_driver_error(self):
        with self.faked():
            self.start()
        for call, message in (
            (lambda: self.mode.send("x"), "send: tui mode only"),
            (self.mode.screen, "screen: tui mode only"),
            (lambda: self.mode.screen(5), "screen: tui mode only"),
        ):
            with self.subTest(message=message), self.assertRaises(SessionError) as caught:
                call()
            self.assertEqual(str(caught.exception), message)


class Start(Base):
    def test_a_commit_failure_leaves_no_session_line_or_process(self):
        with (
            self.faked() as calls,
            self.assertRaisesRegex(LookupError, "^cannot persist$") as caught,
        ):
            self.start(StubClient(run=FailingCommitRun))
        self.assertIsNone(caught.exception.__cause__)
        self.assertEqual((self.recorded, self.mode, calls), ([], None, []))

    def test_one_session_line_then_the_launch_command_with_the_child_env_less_identities(self):
        launch = Launch(
            NOTHING,
            env={"HM_SET": "1"},
            unset=("HM_UNSET",),
            transcript="/t.jsonl",
            resume="stub --resume s1",
        )
        self.session_dependencies = {"class_loader": lambda _path, _base: IdentityClient}
        self.configure_session(
            {"inner": {"implementation": "ensemble.session.clients.inner:InnerClient"}}
        )
        with (
            mock.patch.dict(
                os.environ,
                {
                    "HM_UNSET": "x",
                    "HM_ID": "i",
                    "HM_KEEP": "k",
                    "PATH": os.pathsep.join((PLUGIN_BIN, os.environ["PATH"])),
                },
            ),
            self.faked() as calls,
        ):
            prepared = self.start(
                StubClient(launch), access=Access(cwd=self.dir, project=True), prompt="hi"
            )
            env = {k: v for k, v in os.environ.items() if k not in ("HM_UNSET", "HM_ID")}
        info = SessionInfo(
            sid=SID,
            cwd=self.dir,
            project=True,
            transcript="/t.jsonl",
            resume="stub --resume s1",
        )
        ((launch, lines),) = calls
        self.assertEqual(self.recorded, [info])
        self.assertEqual(
            (launch.argv, launch.cwd, dict(launch.env), lines),
            (
                NOTHING,
                self.dir,
                {
                    **env,
                    "HM_SET": "1",
                    "PWD": self.dir,
                    "ENSEMBLE_AGENT_SESSION": "1",
                    "ENSEMBLE_CLIENT": "stub",
                },
                1,
            ),
        )
        self.assertEqual(self.mode.worker.writes, [b""])
        (spec,) = StubRun.specs
        self.assertEqual(prepared, Prepared(spec, info, NOTHING, launch.env))
        self.assertEqual((spec.prompt, spec.sid, spec.client), ("hi", SID, "stub"))

    def test_the_spec_client_replaces_the_ambient_and_launch_client(self):
        for launch_env in ({}, {"ENSEMBLE_CLIENT": "stale"}):
            with (
                self.subTest(launch_env=launch_env),
                mock.patch.dict(os.environ, {"ENSEMBLE_CLIENT": "ambient"}),
                self.faked() as calls,
            ):
                self.start(StubClient(Launch(NOTHING, env=launch_env)))
                self.assertEqual(calls[0][0].env["ENSEMBLE_CLIENT"], "stub")

    def test_its_cwd_is_the_launch_cwd_else_the_access_cwd(self):
        given, access = (os.path.join(self.dir, d) for d in ("given", "access"))
        for launched, cwd, want in (
            (given, access, given),
            ("", access, access),
        ):
            with self.subTest(want=want), self.faked() as calls:
                prepared = self.start(
                    StubClient(Launch(NOTHING, cwd=launched)), access=Access(cwd=cwd)
                )
                self.assertEqual(
                    (prepared.info.cwd, calls[0][0].cwd, prepared.env["PWD"]), (want,) * 3
                )

    def test_the_client_note_follows_the_prompt_only_with_commands(self):
        quiet = StubClient()
        quiet.client_note = ""
        for client, commands, prompt in (
            (StubClient(), (), "hi"),
            (StubClient(), ("git",), "hi\n\nSTUB NOTE"),
            (quiet, ("git",), "hi"),
        ):
            with self.subTest(commands=commands, note=client.client_note), self.faked():
                prepared = self.start(
                    client, access=Access(cwd=self.dir, commands=commands), prompt="hi"
                )
                self.assertEqual((prepared.spec.prompt, StubRun.specs[-1].prompt), (prompt, prompt))

    def test_refused_before_its_session_line_or_a_process(self):
        for client, fields, error, message in (
            (NoHeadless(), {}, ConfigError, "client 'stub' does not support headless mode"),
            (
                StubClient(),
                {"name": "w 1"},
                ConfigError,
                f"name must match {SESSION_NAME_RULE}, got 'w 1'",
            ),
            (StubClient(), {"events": "/e"}, ConfigError, "events: tui mode only"),
            (StubClient(), {"display": "mgr"}, ConfigError, "display: tui mode only"),
            (
                StubClient(),
                {"flags": ("--verbose", "--resume", SID)},
                ConfigError,
                "--resume: picks the session; use sid= or resume=",
            ),
            (StubClient(), {"sid": None}, ConfigError, "sid: missing"),
            (StubClient(Launch(())), {}, ConfigError, "client 'stub' gave no command"),
            (StubClient(run=FailingRun), {}, LookupError, "no binding"),
        ):
            with self.subTest(message=message):
                with self.faked() as calls, self.assertRaises(error) as caught:
                    self.start(client, **fields)
                self.assertEqual(str(caught.exception), message)
                self.assertEqual(calls, [])
        self.assertEqual((self.recorded, self.mode), ([], None))

    def test_its_checks_in_order_tui_keys_selection_then_clients(self):
        self.configure_session(
            {"broken": {"implementation": "ensemble.session.clients.broken:BrokenClient"}}
        )
        for fields, message in (
            ({"events": "/e", "flags": ("--resume",)}, "events: tui mode only"),
            ({"flags": ("--resume",)}, "--resume: picks the session; use sid= or resume="),
            ({}, "[clients.broken].implementation: "),
        ):
            with (
                self.subTest(fields=fields),
                self.faked(AssertionError("started")),
                self.assertRaises(ConfigError) as caught,
            ):
                self.start(**fields)
            self.assertTrue(str(caught.exception).startswith(message), caught.exception)
        self.assertEqual((self.recorded, StubRun.specs), ([], []))

    def test_a_spawn_failure_is_a_driver_error_after_the_session_line_with_no_session(self):
        for error in (OSError("no exec"), ValueError("bad argument")):
            with self.subTest(error=error):
                self.recorded = []
                with self.faked(error) as calls, self.assertRaises(SessionError) as caught:
                    self.start()
                self.assertEqual(str(caught.exception), f"cannot start {sys.executable}: {error}")
                self.assertEqual(len(calls), 1)
                self.assertEqual((len(self.recorded), self.mode), (1, None))

    def test_a_stream_setup_failure_closes_its_worker_after_the_session_line(self):
        for error in (OSError("no stream"), ValueError("bad stream")):
            with self.subTest(error=error):
                self.recorded, client = [], StubClient(run=UnstreamedRun)
                client.failure = error
                worker = ScriptedWorker(generation=GENERATION, code=None)
                with (
                    mock.patch.object(MemoryHost, "headless_worker", return_value=worker),
                    self.assertRaises(SessionError) as caught,
                ):
                    self.start(client)
                self.assertEqual(str(caught.exception), f"cannot start {sys.executable}: {error}")
                self.assertIs(caught.exception.__cause__, error)
                self.assertEqual(worker.closes, 1)
                self.assertEqual((len(self.recorded), self.mode), (1, None))


class WorkerHost(MemoryHost):
    """Its headless workers' (name, launch, metadata) in `starts`; each `worker`, else a scripted
    one that exits 0 at once.
    """

    def __init__(self, worker=None):
        super().__init__()
        self.scripted, self.starts = worker, []

    def headless_worker(self, name, launch, metadata):
        self.starts.append((name, launch, metadata))
        worker = self.scripted or ScriptedWorker(name)
        worker.generation = metadata["generation"]
        return worker


class TerminalEvents(Base):
    def mode_for(self, events, codes=(0,)):
        spec = Spec(workdir=self.dir, access=Access(cwd=self.dir), sid=SID)
        mode = HeadlessMode(
            Prepared(
                spec, SessionInfo(sid=SID, cwd=self.dir, project=False, transcript="", resume="")
            ),
            ScriptedWorker(code=codes[-1], codes=codes),
            "g" * 32,
        )
        mode.worker.exit_read = True
        mode.exited.set()
        for event in (*events, None, None):
            mode.items.put(event)
        return mode

    def drain(self, mode):
        found = []
        for _ in range(20):
            batch, ended = mode.poll(0)
            found.extend(batch)
            if ended:
                return found
        self.fail("headless never ended")

    def test_success_waits_for_both_streams_and_known_zero_exit(self):
        mode = self.mode_for(
            [TurnStarted(), Reply("whole\n\nreply"), TurnEnd("whole\n\nreply")], (None, 0)
        )
        self.assertEqual(mode.poll(0), ([TurnStarted()], False))
        self.assertEqual(mode.poll(0), ([Reply("whole\n\nreply")], False))
        self.assertEqual(mode.poll(0), ([], False))
        self.assertEqual(mode.poll(0), ([], False))
        self.assertEqual(mode.poll(0), ([], False))
        self.assertEqual(mode.poll(0), ([TurnEnd("whole\n\nreply")], True))
        self.assertEqual(mode.returncode(0), 0)
        self.assertEqual(mode.poll(0), ([], True))

    def test_a_failure_read_after_a_stop_is_the_interrupt(self):
        mode = self.mode_for([TurnStarted(), TurnFailed("incomplete_turn")], (-15,))
        mode.stop()
        found = self.drain(mode)
        self.assertEqual(
            [e for e in found if isinstance(e, TurnFailed)], [TurnFailed("interrupted")]
        )

    def test_nonzero_exit_overrides_structured_success(self):
        for code in (2, -9):
            with self.subTest(code=code):
                mode = self.mode_for([TurnEnd("partial")], (code,))
                found = self.drain(mode)
                self.assertEqual(len(found), 1)
                self.assertIsInstance(found[0], TurnFailed)
                self.assertEqual(found[0].error, "process_exit")
                self.assertIn(str(code), found[0].detail)

    def test_eof_or_text_without_native_completion_is_failure_even_at_zero_exit(self):
        mode = self.mode_for([Reply("partial"), Stderr("truncated stream")])
        found = self.drain(mode)
        self.assertEqual(found[:-1], [Reply("partial"), Stderr("truncated stream")])
        self.assertEqual(found[-1], TurnFailed("incomplete_turn", "truncated stream"))

    def test_native_failure_wins_over_repeated_or_late_completion(self):
        failure = TurnFailed("server_overloaded", "try again")
        mode = self.mode_for([failure, failure, TurnEnd("false success")], (1,))
        self.assertEqual(self.drain(mode), [failure])

    def test_uninteractive_permission_fails_at_once_then_its_exit_closes_the_worker(self):
        mode = self.mode_for([Blocked("approve tool"), TurnEnd("false success")])
        self.assertEqual(mode.poll(0), ([TurnFailed("permission_required", "approve tool")], True))
        self.assertEqual(mode.worker.closes, 0)
        self.assertEqual(mode.returncode(0), 0)
        self.assertEqual(mode.worker.closes, 1)
        self.assertEqual(mode.poll(0), ([], True))

    def test_an_earlier_native_failure_is_the_one_a_permission_request_yields(self):
        failure = TurnFailed("server_overloaded", "try again")
        mode = self.mode_for([failure, Blocked("approve tool")])
        self.assertEqual(self.drain(mode), [failure])

    def test_usage_limit_is_observable_and_cannot_be_a_success(self):
        mode = self.mode_for([LimitHit(5), TurnEnd("false success")])
        self.assertEqual(self.drain(mode), [LimitHit(5), TurnFailed("usage_limit")])


class HomedRun(StubRun):
    reads: ClassVar[list[str]] = []

    def _home(self):
        return ClientHome("f", "d", "HOME_DIR", "c", self._files, persistent=self.persistent)

    persistent = None

    @classmethod
    def _files(cls):
        cls.reads.append("files")
        return {"a": "b"}


class PrivateHomedRun(HomedRun):
    persistent = ("sessions",)


class AccountsClient(StubClient):
    def _accounts(self):
        return frozenset({"default", "work"})


class HomeHost(WorkerHost):
    _inherit_env = False

    def _client_env(self, name, client, home, *, resume):
        return {home.env: f"/state/{name}/{home.dir}"} if home else {}

    def _resume_argv(self, name):
        return ("ensemble-session", "resume", name)


class ThroughItsHost(Base):
    def setUp(self):
        super().setUp()
        HomedRun.reads.clear()

    def start_on(self, host, client_class=StubRun, owner=None, generation=GENERATION, **fields):
        spec = Spec(workdir=self.dir, access=Access(cwd=self.dir), sid=SID, client="stub", **fields)
        self.mode = HeadlessMode.start(
            spec,
            StubClient(Launch(NOTHING, transcript="/t.jsonl"), run=client_class),
            registry=self.session_cls._config().registry,
            record=self.recorded.append,
            host=host,
            owner=owner,
            generation=generation,
        )
        self.addCleanup(self.stop_quietly, self.mode)
        return self.mode

    def stop_quietly(self, mode):
        with contextlib.suppress(SessionError):
            mode.stop()

    def pumps(self, before):
        """The pump threads started since `before`."""
        return [t for t in threading.enumerate() if t.name.endswith("(_pump)") and t not in before]

    def test_start_goes_through_its_host_worker_headless_never_popen(self):
        host = WorkerHost()
        with mock.patch("subprocess.Popen", side_effect=AssertionError("popen")):
            mode = self.start_on(host)
        ((name, launch, metadata),) = host.starts
        prepared = mode.prepared
        self.assertRegex(mode.generation, "^[0-9a-f]{32}$")
        self.assertEqual(
            (name, launch, metadata),
            (
                f"headless-{mode.generation}",
                Launch(prepared.argv, env=prepared.env, cwd=self.dir, transcript="/t.jsonl"),
                {"generation": mode.generation},
            ),
        )
        worker = host.calls.worker.call_args
        self.assertEqual((worker.args[0], worker.kwargs["terminal"]), (name, False))
        self.assertFalse(hasattr(host, "pipe"))
        self.assertEqual(len(self.recorded), 1)

    def test_a_client_that_exited_before_its_input_ended_still_reports_its_exit(self):
        worker = ScriptedWorker(code=3)
        worker.write = mock.Mock(side_effect=SessionError("worker w: exited"))
        worker.state = mock.Mock(return_value=Liveness.ABSENT)
        mode = self.start_on(WorkerHost(worker))
        self.assertEqual(mode.returncode(5), 3)
        live = ScriptedWorker(code=None)
        live.write = mock.Mock(side_effect=SessionError("worker w: input ended"))
        with self.assertRaisesRegex(SessionError, "^cannot start .*: worker w: input ended$"):
            self.start_on(WorkerHost(live))
        self.assertEqual(live.closes, 1)

    def test_start_ends_the_workers_input(self):
        worker = ScriptedWorker(code=None)
        self.start_on(WorkerHost(worker))
        self.assertEqual(worker.writes, [b""])

    def test_a_runs_generation_is_the_one_it_is_given_named_or_not(self):
        for name, generation in (("r1", "a" * 32), (None, "b" * 32)):
            with self.subTest(name=name):
                host = WorkerHost()
                mode = self.start_on(host, name=name, generation=generation)
                ((worker, _, metadata),) = host.starts
                self.assertEqual((mode.generation, metadata["generation"]), (generation,) * 2)
                self.assertEqual(worker, name or f"headless-{generation}")

    def test_a_nameless_run_on_a_named_headless_host_is_refused_before_any_state(self):
        class Committed(StubRun):
            commits = []

            def commit(self):
                self.commits.append(True)

        host = WorkerHost()
        host._named_headless = True
        message = "^a headless session on host memory needs a name$"
        with (
            mock.patch.object(host, "_client_context", side_effect=AssertionError("context")),
            mock.patch.object(host, "_client_env", side_effect=AssertionError("env")),
        ):
            for call in ("preflight", "start"):
                more = (
                    {"record": self.recorded.append, "generation": GENERATION}
                    if call == "start"
                    else {}
                )
                with self.subTest(call=call), self.assertRaisesRegex(ConfigError, message):
                    getattr(HeadlessMode, call)(
                        Spec(workdir=self.dir, access=Access(cwd=self.dir), sid=SID, client="stub"),
                        StubClient(Launch(NOTHING), run=Committed),
                        registry=self.session_cls._config().registry,
                        host=host,
                        **more,
                    )
        self.assertEqual((self.recorded, Committed.commits, host.starts), ([], [], []))
        host.calls.validate.assert_not_called()
        self.assertFalse(os.path.lexists(os.path.join(self.dir, "prompt")))

    def test_an_owner_is_accepted_and_kept_nowhere(self):
        host = WorkerHost()
        mode = self.start_on(host, owner="/h")
        self.assertEqual(host.starts[0][2], {"generation": mode.generation})
        self.assertEqual(mode.opening_pending, None)

    def test_a_named_run_names_its_owner_as_its_harness(self):
        host = HomeHost()
        self.start_on(host, client_class=HomedRun, owner="/h", name="w1")
        metadata = host.starts[0][2]
        self.assertEqual((metadata["harness"], metadata["sid"]), ("/h", SID))
        self.assertNotIn("owner", metadata)

    def test_returncode_is_the_hosts_confirmed_code_then_closes_the_worker_once(self):
        worker = ScriptedWorker(code=None, codes=[None, 3])
        mode = self.start_on(WorkerHost(worker))
        self.assertEqual(mode.state(), Liveness.LIVE)
        self.assertIsNone(mode.returncode(0.1))
        self.assertEqual(worker.closes, 0)
        with worker.condition:
            worker.code = 3
            worker.condition.notify_all()
        self.assertEqual(mode.returncode(5), 3)
        self.assertEqual(mode.state(), Liveness.ABSENT)
        self.assertEqual(mode.returncode(0), 3)
        mode.stop()
        self.assertEqual(worker.closes, 1)

    def test_stop_closes_the_worker_once_and_a_refused_close_raises(self):
        worker = ScriptedWorker(code=None)
        mode = self.start_on(WorkerHost(worker))
        mode.stop()
        mode.stop()
        self.assertEqual(worker.closes, 1)
        refusing = ScriptedWorker(code=None)
        refusing.close = mock.Mock(side_effect=SessionError("worker w: refused"))
        mode = self.start_on(WorkerHost(refusing))
        with self.assertRaisesRegex(SessionError, "^worker w: refused$"):
            mode.stop()

    def test_a_failed_close_is_retried_by_the_next_stop(self):
        worker = ScriptedWorker(code=None)
        closes = [SessionError("worker w: removal failed"), None]
        worker.close = mock.Mock(side_effect=closes)
        mode = self.start_on(WorkerHost(worker))
        with self.assertRaisesRegex(SessionError, "^worker w: removal failed$"):
            mode.stop()
        mode.stop()
        mode.stop()
        self.assertEqual(worker.close.call_count, 2)

    def test_output_frames_become_lines_with_universal_newlines(self):
        worker = ScriptedWorker(
            batches=[
                [WorkerOutput(b'{"text": "a"}\r\n{"te'), WorkerOutput(b"bad \xff\rnext", "stderr")],
                [WorkerOutput(b'xt": "\xe9\x9b'), WorkerOutput(b'\xaa"}\n')],
            ]
        )
        mode = self.start_on(WorkerHost(worker))
        found, ended = [], False
        for _ in range(50):
            batch, ended = mode.poll(1)
            found.extend(batch)
            if ended:
                break
        self.assertTrue(ended)
        self.assertEqual(
            [event for event in found if isinstance(event, Reply)], [Reply("a"), Reply("雪")]
        )
        self.assertEqual(
            [event for event in found if isinstance(event, Stderr)],
            [Stderr("bad \ufffd"), Stderr("next")],
        )

    def test_a_line_over_line_max_fails_the_run_and_stops_the_worker(self):
        chunk = b"x" * (1 << 20)
        worker = ScriptedWorker(
            batches=[[WorkerOutput(chunk)] for _ in range(LINE_MAX // len(chunk) + 1)], code=None
        )
        mode = self.start_on(WorkerHost(worker))
        message = f"^headless session headless-{mode.generation}: a client output line over "
        with self.assertRaisesRegex(SessionError, message + f"{LINE_MAX} bytes$"):
            for _ in range(50):
                mode.poll(1)
        self.assertEqual(worker.closes, 1)

    def test_a_named_headless_spec_gets_the_hosts_client_env_and_metadata(self):
        host = HomeHost()
        with mock.patch.dict(os.environ, {"HM_AMBIENT": "1"}):
            mode = self.start_on(host, name="w1", resume=True, client_class=HomedRun)
        ((name, launch, metadata),) = host.starts
        self.assertEqual((name, StubRun.specs[-1].env), ("w1", {"HOME_DIR": "/state/w1/d"}))
        self.assertNotIn("HM_AMBIENT", launch.env)
        self.assertEqual(
            dict(metadata),
            {
                "sid": SID,
                "client": "stub",
                "generation": GENERATION,
                "home_field": "f",
                "home_dir": "d",
                "credential": "c",
                "home_files": "{}",
                "resume": "1",
            },
        )
        self.assertEqual(HomedRun.reads, [])
        resume = f"cd {self.dir} && ensemble-session resume w1"
        self.assertEqual((mode.prepared.info.resume, self.recorded[0].resume), (resume, resume))

    def test_a_fresh_start_reads_the_home_files_once(self):
        host = HomeHost()
        self.start_on(host, name="w1", client_class=HomedRun)
        self.assertEqual(json.loads(host.starts[0][2]["home_files"]), {"a": "b"})
        self.assertEqual(HomedRun.reads, ["files"])

    def test_a_private_home_resume_reads_the_home_files(self):
        host = HomeHost()
        self.start_on(host, name="w1", resume=True, client_class=PrivateHomedRun)
        self.assertEqual(json.loads(host.starts[0][2]["home_files"]), {"a": "b"})
        self.assertEqual(HomedRun.reads, ["files"])

    def test_a_host_without_client_homes_never_reads_the_home_files(self):
        host = WorkerHost()
        self.start_on(host, name="w1", client_class=HomedRun)
        self.assertNotIn("home_files", host.starts[0][2])
        self.assertEqual(HomedRun.reads, [])

    def test_a_selected_account_names_the_home_in_metadata_and_never_reaches_the_run(self):
        for env, account in (({}, None), ({ACCOUNT: "work"}, "work"), ({ACCOUNT: ""}, None)):
            with self.subTest(env=env), mock.patch.dict(os.environ, {ACCOUNT: "outer"}):
                host = HomeHost()
                spec = Spec(
                    workdir=self.dir,
                    access=Access(cwd=self.dir),
                    sid=SID,
                    client="claude",
                    name="w1",
                    env={**env, "OTHER": "1"},
                )
                client = AccountsClient(Launch(NOTHING), run=HomedRun)
                for call in ("preflight", "start"):
                    more = (
                        {"record": self.recorded.append, "generation": GENERATION}
                        if call == "start"
                        else {}
                    )
                    getattr(HeadlessMode, call)(
                        spec,
                        client,
                        registry=self.session_cls._config().registry,
                        host=host,
                        **more,
                    )
                ((_, launch, metadata),) = host.starts
                self.assertEqual(metadata.get("home_account"), account)
                self.assertEqual(
                    dict(StubRun.specs[-1].env), {"OTHER": "1", "HOME_DIR": "/state/w1/d"}
                )
                self.assertNotIn(ACCOUNT, launch.env)

    def test_the_run_gets_yolo_on_an_isolated_host_else_auto(self):
        seen = []

        class Seen(StubRun):
            def launch(self, spec):
                seen.append(self._context.permission)
                return super().launch(spec)

        for isolated in (False, True):
            host = WorkerHost()
            host._isolated = isolated
            self.start_on(host, client_class=Seen)
        self.assertEqual(seen, [Permission.AUTO, Permission.YOLO])

    def test_the_run_gets_the_hosts_context_object(self):
        context = ClientContext(
            "host.docker.internal",
            Permission.YOLO,
            Container("/image/bin", "/image/python", "/image/modules", "/run/state"),
        )
        seen = []

        class Seen(StubRun):
            def launch(self, spec):
                seen.append(self._context)
                return super().launch(spec)

        host = WorkerHost()
        with mock.patch.object(host, "_client_context", return_value=context):
            self.start_on(host, client_class=Seen)
        self.assertEqual(len(seen), 1)
        self.assertIs(seen[0], context)

    def test_an_over_budget_prompt_becomes_a_pointer_to_a_file_in_the_workdir(self):
        big, path = "雪" * 40_000, os.path.join(self.dir, "prompt", "prompt.md")
        host = WorkerHost()
        self.start_on(host, prompt=big)
        (spec,) = StubRun.specs
        self.assertEqual(spec.prompt, POINTER.format(size=len(big.encode()), path=path))
        self.assertEqual(spec.access.reads, (os.path.dirname(path),))
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), big)
        self.assertNotIn("prompt_file", host.starts[0][2])

    def test_a_container_context_puts_the_prompt_in_metadata_not_on_disk(self):
        context = ClientContext(
            "host.docker.internal",
            Permission.YOLO,
            Container("/image/bin", "/image/python", "/image/modules", "/run/state"),
        )
        big, path = "雪" * 40_000, "/run/state/prompt/prompt.md"
        host = WorkerHost()
        with mock.patch.object(host, "_client_context", return_value=context):
            self.start_on(host, prompt=big, name="w")
        metadata = host.starts[0][2]
        self.assertEqual((metadata["prompt_file"], metadata["prompt"]), (path, big))
        self.assertEqual(StubRun.specs[0].prompt, POINTER.format(size=len(big.encode()), path=path))
        self.assertFalse(os.path.lexists(os.path.join(self.dir, "prompt")))

    def test_a_prompt_within_the_budget_stays_and_preflight_writes_nothing(self):
        at = "a" * 99_998
        self.start_on(WorkerHost(), prompt=at)
        self.assertEqual(StubRun.specs[0].prompt, at)
        HeadlessMode.preflight(
            Spec(workdir=self.dir, access=Access(cwd=self.dir), sid=SID, prompt="雪" * 40_000),
            StubClient(),
            registry=self.session_cls._config().registry,
            host=WorkerHost(),
        )
        self.assertFalse(os.path.lexists(os.path.join(self.dir, "prompt")))

    def test_an_unwritable_prompt_file_starts_nothing(self):
        real = os.open

        def refusing(path, *args, **kwargs):
            if str(path).endswith("prompt.md"):
                raise OSError(errno.EACCES, "Permission denied")
            return real(path, *args, **kwargs)

        host = WorkerHost()
        with (
            mock.patch("os.open", refusing),
            self.assertRaisesRegex(SessionError, "^prompt file .*prompt.md: Permission denied$"),
        ):
            self.start_on(host, prompt="雪" * 40_000)
        self.assertEqual((host.starts, self.recorded), ([], []))

    def test_a_nameless_spec_gets_no_host_env_or_resume_and_only_its_generation(self):
        host = HomeHost()
        mode = self.start_on(host, client_class=HomedRun)
        ((name, _, metadata),) = host.starts
        self.assertEqual(
            (name, metadata, self.recorded[0].resume),
            (f"headless-{mode.generation}", {"generation": mode.generation}, ""),
        )
        self.assertEqual(StubRun.specs[-1].env, {})

    def test_preflight_validates_a_named_run_on_its_host(self):
        for name, validated in ((None, []), ("w1", ["w1"])):
            with self.subTest(name=name):
                host = HomeHost()
                spec = Spec(
                    workdir=self.dir, access=Access(cwd=self.dir), sid=SID, client="stub", name=name
                )
                HeadlessMode.preflight(
                    spec,
                    StubClient(Launch(NOTHING), run=HomedRun),
                    registry=self.session_cls._config().registry,
                    host=host,
                )
                self.assertEqual([c.args[0] for c in host.calls.validate.call_args_list], validated)

    def test_after_stop_a_polling_caller_still_gets_the_end(self):
        mode = self.start_on(WorkerHost(ScriptedWorker(code=None)))
        mode.stop()
        ends = [mode.poll(5)[1] for _ in range(2)]
        self.assertEqual(ends, [False, True])

    def test_an_item_queued_as_a_stopped_poll_times_out_is_kept(self):
        mode = self.start_on(WorkerHost())
        for done in mode.done:
            self.assertTrue(done.wait(5))
        mode.stop()
        late, real = RuntimeError("late"), queue.Queue()

        class Racy:
            def get(self, timeout):
                mode.items = real
                real.put(late)
                for done in mode.done:
                    done.set()
                raise queue.Empty

        for done in mode.done:
            done.clear()
        mode.items = Racy()
        self.assertEqual(mode.poll(0), ([], False))
        with self.assertRaises(RuntimeError) as caught:
            mode.poll(0)
        self.assertIs(caught.exception, late)

    def test_the_event_queue_is_bounded_and_a_stop_frees_a_pump_waiting_on_it(self):
        worker = ScriptedWorker(
            batches=[[WorkerOutput(b"e\n" * (QUEUE_MAX + 2), "stderr")]], code=None
        )
        before = set(threading.enumerate())
        mode = self.start_on(WorkerHost(worker))
        self.assertEqual(mode.items.maxsize, QUEUE_MAX)
        deadline = time.monotonic() + 5
        while not mode.items.full() and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.05)
        pumps = self.pumps(before)  # stderr's waits on the queue, stdout's on its running worker
        self.assertEqual((mode.items.full(), len(pumps)), (True, 2))
        mode.stop()
        for pump in pumps:
            pump.join(5)
        self.assertFalse(any(pump.is_alive() for pump in pumps))
        self.assertTrue(mode.items.full())  # its last lines and its end dropped
        ended = False
        for _ in range(QUEUE_MAX + 4):
            if ended := mode.poll(0.1)[1]:
                break
        self.assertTrue(ended)


if __name__ == "__main__":
    unittest.main()
