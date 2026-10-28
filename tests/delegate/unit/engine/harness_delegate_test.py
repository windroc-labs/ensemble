"""HarnessDelegate spawns its drive through ProcessControl, no Host, and names it in a close
failure; the drive's environment holds no launcher variable; a run's state lives only in its run dir, which counts attempts, and a
refused start creates nothing; a fresh workdir passes the Docker Host's preflight; preview and
instruct write no state; owner overlays shape its run; resume continues a run in the mode it
records or in the one it is given, and a run whose process died, its tui agent session left open,
at once; replace, resume and detach first kill the callback group a dead harness left; checkouts
lists its agent dir's real checkouts and clean deletes that dir, refusing a live run or a dir out of
place.
"""

import contextlib
import errno
import fcntl
import io
import itertools
import json
import os
import shutil
import signal
import threading
import unittest
import uuid
from collections.abc import Callable
from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest import mock

from ensemble.delegate import ConfigError, Outcome, Spec
from ensemble.delegate.composer import Composer, RunParams
from ensemble.delegate.engine.config import module_roots
from ensemble.delegate.engine.drivers import AgentDriver
from ensemble.delegate.engine.harness_delegate import COLOR_WAIT, DRAIN_WAIT, HarnessDelegate
from ensemble.delegate.engine.record import CLOSED, HANDLER, RunRecord
from ensemble.delegate.engine.runner import STOP_LIMIT, report
from ensemble.delegate.engine.state import RunState
from ensemble.delegate.engine.types import Begun, Finished, Layout, Result
from ensemble.session import (
    SIGNALS,
    Access,
    EnvironmentSpec,
    Exited,
    HookCallback,
    Mode,
    NotShowable,
    Reply,
    Session,
    SessionBusy,
    SessionError,
    TurnEnd,
    Unanswered,
)
from ensemble.session import ConfigError as SessionConfigError
from ensemble.session import Spec as SessionSpec
from ensemble.session.engine.types import Liveness, SessionRow
from ensemble.utils import OutcomeStatus, StatusState, Stopped
from tests.delegate.fakes.harness_session import (
    DrivenDelegate,
    FailingSession,
    FakeControl,
    FakeViewer,
    HarnessSession,
    LockingSession,
    RecordingHost,
    RecordingProcess,
    harness_launches,
)
from tests.delegate.harness import SID, HarnessCase
from tests.delegate.replies import block
from tests.delegate.unit.engine.orphans import Clock, Orphan
from tests.session import harnesses
from tests.session.case import PLUGIN_BIN
from tests.session.unit.hosts.docker import support
from tests.utils.process_fixtures import DEVNULL, STDOUT

ORPHAN = 4242


def gives_up():
    """HarnessSession's next worker ends its attempt without an outcome (a nudge, then STOP_LIMIT
    more turn ends), its session left open.
    """
    HarnessSession.script = (TurnEnd("No outcome block."),) * (1 + STOP_LIMIT)
    HarnessSession.lingers = True


class RealSpawn(DrivenDelegate):
    _spawn = staticmethod(HarnessDelegate._spawn)


class CreateTest(HarnessCase):
    def test_resumed_run_keeps_the_owners_brake(self):
        command = "owner-brake researcher"
        record = self.make(
            role="researcher",
            spec=Spec("input", self.work, client="fake", sid=SID),
            overlays=({"roles": {"researcher": {"gate": command}}},),
        ).detach()
        self.stop(record)
        resumed = DrivenDelegate.attach(self.work, session_cls=HarnessSession).resume()
        self.assertEqual(self.metadata()["plan"]["run"]["gate"], command)
        self.assertIn(command, self.metadata()["plan"]["session"]["access"]["commands"])
        self.stop(resumed)

    def test_only_the_owners_overlay_sets_a_brake_regardless_of_callback(self):
        command = "owner-brake researcher"
        for callback in (None, (), ("owner",), ("owner", "writeback")):
            for expected in ("", command):
                with self.subTest(callback=callback, gate=expected):
                    preview = DrivenDelegate.preview(
                        "researcher",
                        Spec("input", self.work, client="fake", sid=SID),
                        callback=callback,
                        overlays=({"roles": {"researcher": {"gate": expected}}},),
                        session_cls=HarnessSession,
                    )
                    self.assertEqual(preview.plan.run.gate, expected)
                    self.assertEqual(
                        expected in preview.plan.session.access.commands, bool(expected)
                    )
                    if expected:
                        self.assertIn(f"`{expected}`", preview.plan.session.prompt)

    def test_create_persists_placement_with_overlays(self):
        class Bound(DrivenDelegate):
            def __init__(self, role, spec, **placement):
                super().__init__(role, spec, session_cls=HarnessSession, **placement)

        overlay = {"effort": "high"}
        view = Bound.create(
            "dummy-tester",
            Spec("input", self.work, client="fake", sid=SID),
            display="none",
            callback=("callback",),
            overlays=(overlay,),
        )
        self.assertIsInstance(view, Bound)
        self.assertEqual(self.metadata()["overlays"], [overlay])
        self.assertEqual(
            (self.metadata()["display"], self.metadata()["callback"]), ("none", ["callback"])
        )
        attached = Bound.attach(self.work, sid=SID, session_cls=HarnessSession)
        self.assertEqual(attached.workdir, self.work)


class ReferenceRepoTest(HarnessCase):
    def test_a_local_run_records_no_reference_repo_and_writes_no_intent(self):
        spec = Spec(
            "Reference: T\nRepo: acme/ensemble\n\nHello.",
            self.work,
            task="echo",
            sid=SID,
            client="fake",
        )
        view = self.make(spec=spec)
        self.assertEqual(self.metadata()["plan"]["repo"], None)
        self.assertFalse(self.metadata()["plan"]["docker"])
        view._complete(
            Result(0, Outcome("needs_input", "Question", "Answer needed", questions=["Which?"]))
        )
        view.resume(input="Reference: T\nRepo: acme/other\n\nThe answer.")
        self.assertEqual(self.metadata()["plan"]["repo"], None)
        self.assertNotIn("container-intent.json", os.listdir(os.path.join(self.work, "run")))


class Drive(HarnessCase):
    """The drive: a detached local process through ProcessControl, no Host (AC16, AC17)."""

    spawned = False

    def headless(self):
        spec = Spec("input", self.work, task="echo", mode="headless", client="fake", sid=SID)
        if not self.spawned:
            return self.make(spec=spec, host="agent")
        return RealSpawn(
            "dummy-tester", spec, session_cls=HarnessSession, viewer=FakeViewer(), host="agent"
        )

    def real_spawn(self, popen):
        """HarnessDelegate's own _spawn, its Popen `popen`."""
        self.spawned = True
        self.enterContext(mock.patch("subprocess.Popen", popen))

    def test_spawn_detached_with_signals_blocked(self):
        seen = {}

        def popen(argv, **options):
            seen.update(options, argv=argv, state=self.metadata())
            seen["mask"] = signal.pthread_sigmask(signal.SIG_BLOCK, [])
            return mock.Mock(wait=mock.Mock(return_value=0))

        self.real_spawn(popen)
        delegate = self.headless()
        before = signal.pthread_sigmask(signal.SIG_BLOCK, [])
        record = delegate.detach()
        control = seen["state"]["drive"]
        self.assertEqual(seen["argv"], ["fake-drive", control])
        self.assertEqual(seen["state"]["detached"], True)
        self.assertEqual(self.metadata()["drive"], control)
        self.assertTrue(set(SIGNALS) <= seen["mask"])
        self.assertEqual(signal.pthread_sigmask(signal.SIG_BLOCK, []), before)
        self.assertEqual(
            {k: seen[k] for k in ("cwd", "stdin", "stderr", "start_new_session")},
            {
                "cwd": os.path.join(self.work, "run"),
                "stdin": DEVNULL,
                "stderr": STDOUT,
                "start_new_session": True,
            },
        )
        launch = FakeControl.attach(control).data()["launch"]
        self.assertEqual(launch["blocked_signals"], list(SIGNALS))
        self.assertEqual(launch["argv"][-2:], ["ensemble.delegate.engine.harness", self.work])
        self.assertEqual(seen["env"], launch["env"])
        self.assertEqual(RecordingHost.launches, [])
        self.assertEqual(record.sessions, ())

    def test_drive_log_appended_0600(self):
        log = os.path.join(self.work, "run", "drive.log")
        written = []

        def popen(_argv, **options):
            os.write(options["stdout"], b"line\n")
            written.append(oct(os.fstat(options["stdout"]).st_mode & 0o777))
            return mock.Mock(wait=mock.Mock(return_value=0))

        self.real_spawn(popen)
        delegate = self.headless()
        with open(log, "w", encoding="utf-8") as file:
            file.write("before\n")
        os.chmod(log, 0o600)
        delegate.detach()
        with open(log, encoding="utf-8") as file:
            self.assertEqual(file.read(), "before\nline\n")
        self.assertEqual(written, ["0o600"])

    def test_drive_log_symlink_refused(self):
        self.real_spawn(mock.Mock(side_effect=AssertionError("spawned")))
        delegate = self.headless()
        target = os.path.join(self.home, "elsewhere")
        os.symlink(target, os.path.join(self.work, "run", "drive.log"))
        with self.assertRaises(SessionError) as caught:
            delegate.detach()
        self.assertRegex(str(caught.exception), r"^detach dummy-tester-0f8fad5b-drive: ")
        self.assertFalse(os.path.lexists(target))
        self.assertEqual((self.metadata()["detached"], self.metadata()["drive"]), (False, None))

    def test_spawn_failure_restores_state(self):
        for error, why in (
            (FileNotFoundError(2, "No such file or directory"), "No such file or directory"),
            (TimeoutError("process controller did not start"), "process controller did not start"),
        ):
            with self.subTest(error=error):
                shutil.rmtree(self.work, ignore_errors=True)
                delegate = self.headless()
                before = self.metadata()
                if isinstance(error, TimeoutError):
                    waits = mock.patch.object(FakeControl, "wait_started", side_effect=error)
                else:
                    waits = contextlib.nullcontext()
                    FakeControl.spawn_error = error
                with (
                    waits,
                    mock.patch.object(FakeControl, "cancel") as cancel,
                    self.assertRaises(SessionError) as caught,
                ):
                    delegate.detach()
                FakeControl.spawn_error = None
                self.assertEqual(
                    str(caught.exception), f"detach dummy-tester-{SID[:8]}-drive: {why}"
                )
                cancel.assert_called_once_with()
                self.assertEqual(self.metadata(), before)

    def test_docker_agent_needs_no_other_host(self):
        with open(self.local, "a", encoding="utf-8") as file:
            file.write(
                "[session.hosts.box]\n"
                'implementation = "ensemble.session.hosts.docker.host:DockerHost"\n'
                'docker = "/usr/local/bin/docker"\n'
            )
        HarnessSession.hosts["box"] = RecordingHost("box")
        spec = Spec(
            "Reference: T\nRepo: acme/ensemble\n\nHi.",
            self.work,
            mode="tui",
            client="fake",
            sid=SID,
        )
        delegate = self.make(spec=spec, host="box")
        self.assertEqual(self.metadata()["plan"]["docker"], True)
        record = delegate.detach()
        self.assertEqual(record.sessions, (f"dummy-tester-{SID[:8]}",))
        self.assertEqual(
            [launch["name"] for launch in harness_launches()], [f"{record.name}-drive"]
        )
        self.assertEqual([h for h, _ in RecordingHost.processes], ["box"])
        self.stop(record)

    def test_alive_from_control(self):
        delegate = self.headless()
        record = delegate.detach()
        control = FakeControl(self.metadata()["drive"])
        view = DrivenDelegate.attach(record, session_cls=HarnessSession)
        self.assertTrue(view.alive())
        control.close()
        self.assertFalse(view.alive())
        with mock.patch.object(FakeControl, "attach", side_effect=PermissionError("not a dir")):
            self.assertFalse(view.alive())
        with self.store().mutex():
            RunState(Layout(self.work)).write(json.dumps({"drive": control.path}))
        control._save(alive=True)
        self.assertTrue(view._live(RunState(Layout(self.work)).read()))
        control.close()
        self.assertFalse(view._live(RunState(Layout(self.work)).read()))

    def store(self):
        return RunState(Layout(self.work))

    def test_close_terminates_then_kills(self):
        record = self.headless().detach()
        control = FakeControl(self.metadata()["drive"])
        for stubborn, signals in ((None, ["terminate"]), ("term", ["terminate", "kill"])):
            with self.subTest(stubborn=stubborn):
                control._save(alive=True, signals=[])
                FakeControl.stubborn = stubborn
                DrivenDelegate.attach(record, session_cls=HarnessSession).close()
                self.assertEqual(control.data()["signals"], signals)
                self.assertFalse(control.running())
        self.assertEqual(FakeViewer.closed, [])

    def test_close_outlived_raises(self):
        record = self.headless().detach()
        control = FakeControl(self.metadata()["drive"])
        FakeControl.stubborn = "kill"
        with self.assertRaises(SessionError) as caught:
            DrivenDelegate.attach(record, session_cls=HarnessSession).close()
        self.assertEqual(str(caught.exception), f"close dummy-tester-{SID[:8]}-drive: did not exit")
        self.assertEqual(control.data()["signals"], ["terminate", "kill"])


class Record(HarnessCase):
    def test_sessions_without_drive(self):
        for mode, sessions in (("tui", (f"dummy-tester-{SID[:8]}",)), ("headless", ())):
            with self.subTest(mode=mode):
                shutil.rmtree(self.work, ignore_errors=True)
                spec = Spec("input", self.work, task="echo", mode=mode, client="fake", sid=SID)
                record = self.make(spec=spec, host="agent").detach()
                self.assertEqual(record.sessions, sessions)
                self.assertNotIn(f"dummy-tester-{SID[:8]}-drive", record.sessions)
                self.stop(record)


class DockerHeadlessTest(HarnessCase):
    def test_a_headless_session_carries_the_agent_name_only_on_the_docker_host(self):
        HarnessSession.hosts["box"] = RecordingHost("box")
        with open(self.local, "a", encoding="utf-8") as file:
            file.write(
                "[session.hosts.box]\n"
                'implementation = "ensemble.session.hosts.docker.host:DockerHost"\n'
                'docker = "/usr/local/bin/docker"\n'
            )
        name = f"dummy-tester-{SID[:8]}"
        for host, want in (("box", name), ("agent", None)):
            with self.subTest(host=host):
                spec = Spec(
                    "input", self.work, task="echo", mode="headless", client="fake", sid=SID
                )
                placement = {"session_cls": HarnessSession, "host": host}
                preview = DrivenDelegate.preview("dummy-tester", spec, **placement)
                self.assertEqual((preview.agent, preview.plan.session.name), (want, want))
                self.assertEqual(preview.plan.docker, want is not None)
                self.assertEqual(preview.plan.spec.name, want)
        spec = Spec("input", self.work, task="echo", mode="headless", client="fake", sid=SID)
        view = self.make(spec=spec, host="box")
        self.assertEqual(self.metadata()["plan"]["session"]["name"], name)
        view._complete(Result(0, Outcome("needs_input", "Q", "A", questions=["Which?"])))
        view.resume(input="The answer.")
        self.assertEqual(self.metadata()["plan"]["session"]["name"], name)


class ForegroundHeadlessCloseTest(HarnessCase):
    """close from another process of a foreground headless run: its own process holds its run
    lock and its Tie; on Docker its session is closed by name, elsewhere close refuses.
    """

    def setUp(self):
        super().setUp()
        HarnessSession.hosts["box"] = RecordingHost("box")
        with open(self.local, "a", encoding="utf-8") as file:
            file.write(
                "[session.hosts.box]\n"
                'implementation = "ensemble.session.hosts.docker.host:DockerHost"\n'
                'docker = "/usr/local/bin/docker"\n'
            )
        for patcher in (
            mock.patch.object(HarnessSession, "attach", side_effect=SessionError("no session")),
            mock.patch.object(HarnessSession, "retire"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def foreground(self, host):
        spec = Spec("input", self.work, task="echo", mode="headless", client="fake", sid=SID)
        self.make(spec=spec, host=host)
        return DrivenDelegate.attach(self.work, session_cls=HarnessSession)

    def test_on_docker_it_closes_the_running_runs_session_by_name(self):
        view = self.foreground("box")
        with RunState(Layout(self.work)).run_lock():
            view.close()
        HarnessSession.retire.assert_called_once_with(
            f"dummy-tester-{SID[:8]}", sid=SID, host="box"
        )
        HarnessSession.retire.side_effect = Unanswered("still running without its harness")
        with RunState(Layout(self.work)).run_lock(), self.assertRaises(Unanswered):
            view.close()

    def test_elsewhere_a_running_one_is_refused_an_ended_one_is_left(self):
        view = self.foreground("agent")
        refused = self.assertRaisesRegex(SessionError, "no close route; end it in its terminal")
        with RunState(Layout(self.work)).run_lock(), refused:
            view.close()
        view.close()
        HarnessSession.retire.assert_not_called()

    def test_an_ended_docker_run_retires_nothing(self):
        self.foreground("box").close()
        HarnessSession.retire.assert_not_called()


def broker_command(argv) -> str:
    """The faked broker's command; an AssertionError names any other process, which a patched
    Popen would otherwise answer from a missing fixture as FileNotFoundError."""
    if os.path.basename(argv[0]) != "ensemble-broker":
        raise AssertionError(f"unmodelled process: {' '.join(argv[:4])}")
    return argv[1]


class DockerPreflightTest(HarnessCase):
    """The real Session's Docker Host, its broker process faked, no daemon socket in its account
    home and its hook command on PATH, preflights a fresh workdir's run.
    """

    def setUp(self):
        super().setUp()
        self.settings(client="claude")
        with open(self.local, "a", encoding="utf-8") as file:
            file.write(
                "[session.hosts.box]\n"
                'implementation = "ensemble.session.hosts.docker.host:DockerHost"\n'
                'docker = "/usr/local/bin/docker"\n'
            )
        self.calls, self.failing = [], False
        path = os.pathsep.join((PLUGIN_BIN, os.environ.get("PATH", os.defpath)))
        for patcher in (
            mock.patch("subprocess.Popen", side_effect=self.broker),
            mock.patch("pwd.getpwuid", return_value=SimpleNamespace(pw_dir=self.home)),
            mock.patch.dict(os.environ, {"PATH": path}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def broker(self, argv, **_):
        command = broker_command(argv)
        self.calls.append(command)
        if self.failing and command == "build":
            return support.FakeProc(
                argv, err=support.text("error-build_failed.json").encode(), code=8
            )
        return support.FakeProc(argv, out=support.text(f"{command}-output.json").encode())

    def create(self, mode):
        spec = Spec("input", self.work, task="echo", mode=mode, sid=SID)
        return self.make(spec=spec, session_cls=Session, host="box")

    def test_a_fresh_workdir_passes_the_docker_preflight(self):
        for mode in ("headless", "tui"):
            with self.subTest(mode=mode):
                self.assertFalse(os.path.exists(self.work))
                self.create(mode)
                self.assertEqual(self.calls, ["version", "build"])
                self.assertTrue(os.path.isdir(Layout(self.work).agent))
                self.assertTrue(self.metadata()["plan"]["docker"])
                shutil.rmtree(self.work)
                self.calls.clear()

    def test_an_unmodelled_process_fails_naming_it(self):
        with self.assertRaisesRegex(AssertionError, "unmodelled process: /bin/sh -c true"):
            self.broker(["/bin/sh", "-c", "true"])

    def test_a_refused_docker_preflight_leaves_no_workdir(self):
        self.failing = True
        with self.assertRaisesRegex(ConfigError, "Example build_failed"):
            self.create("headless")
        self.assertEqual(self.calls, ["version", "build"])
        self.assertFalse(os.path.exists(self.work))


class CallerSessionTest(HarnessCase):
    """The Docker classes pass in a caller's tmux pane and agent session, as on CI: a plain
    unittest run in an agent's shell once leaked TMUX (the viewer's placement then ran tmux through
    their patched Popen) and ENSEMBLE_AGENT_SESSION (a nested run refused)."""

    maxDiff = None

    def test_the_docker_tests_ignore_the_callers_tmux_and_agent_session(self):
        caller = {
            "TMUX": "/tmp/tmux-1/default,1,0",
            "TMUX_PANE": "%1",
            "ENSEMBLE_AGENT_SESSION": "1",
        }
        loader = unittest.defaultTestLoader
        suite = unittest.TestSuite(
            loader.loadTestsFromTestCase(case) for case in (DockerPreflightTest, VerifierDockerTest)
        )
        cases, result = suite.countTestCases(), unittest.TestResult()
        with mock.patch.dict(os.environ, caller):
            suite.run(result)
        failed = [f"{t.id()}: {tb.splitlines()[-1]}" for t, tb in result.failures + result.errors]
        self.assertEqual(failed, [])
        self.assertGreater(cases, 0)
        self.assertEqual((result.testsRun, result.skipped), (cases, []))


class VerifierClientTest(HarnessCase):
    """verifier uses the router's client overlay and an explicit Spec client (WDRC-196)."""

    def test_the_gate_s_overlay_selects_each_client(self):
        for client in ("codex", "opencode", "claude"):
            with self.subTest(client=client), mock.patch.object(HarnessSession, "usage") as usage:
                overlays = ({}, {"roles": {"verifier": {"client": client}}})
                DrivenDelegate.usage("verifier", overlays=overlays, session_cls=HarnessSession)
                usage.assert_called_once_with(client)

    def test_a_spec_selects_each_client_over_the_overlay(self):
        self.settings(default="docker")  # mini-op needs a Docker default
        HarnessSession.hosts["agent"] = RecordingHost("docker")
        for client in ("codex", "opencode", "claude"):
            with self.subTest(client=client):
                spec = Spec(
                    "Repo: windroc-labs/ensemble", self.work, task="verify", sid=SID, client=client
                )
                self.make(role="verifier", spec=spec, overlays=({"client": "fake"},))
                self.assertEqual(self.metadata()["plan"]["session"]["client"], client)
                shutil.rmtree(self.work)


class VerifierDockerTest(HarnessCase):
    """verifier on the packaged Docker Host, its broker faked: a missing mini-op:env or keychain
    item fails before any container starts (WDRC-110 FR-50); no keychain, Docker or network. Its
    session harness's start runs in this process (harnesses.Inline), so the faked broker answers.
    Its github [output] needs the Docker Host's App (WDRC-338).
    """

    def setUp(self):
        super().setUp()
        self.settings(default="docker")
        with open(self.local, "a", encoding="utf-8") as file:
            file.write('[session.hosts.docker.github]\napp = "ensemble-broker-bot"\n')
        self.calls, self.programs, self.refusals = [], [], {}
        path = os.pathsep.join((PLUGIN_BIN, os.environ.get("PATH", os.defpath)))
        for patcher in (
            mock.patch("subprocess.Popen", side_effect=self.broker),
            mock.patch("pwd.getpwuid", return_value=SimpleNamespace(pw_dir=self.home)),
            mock.patch.dict(os.environ, {"PATH": path}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def broker(self, argv, **kw):
        if harnesses.spawned(argv):
            return harnesses.Inline(argv)
        program = os.path.basename(argv[0])
        self.programs.append(program)
        if program == "tmux":  # the packaged tmux Host's listing: no server, no session
            process = mock.MagicMock(returncode=1)
            process.__enter__.return_value = process
            process.poll.return_value = 1
            gone = "no server running on /tmp/tmux-0/default\n"
            process.communicate.return_value = (
                ("", gone) if kw.get("text") else (b"", gone.encode())
            )
            return process
        command = broker_command(argv)
        self.calls.append(command)
        if command in self.refusals:
            code, name, message = self.refusals[command]
            error = {"command": command, "code": name, "message": message, "cleanup": "not_needed"}
            return support.FakeProc(
                argv, err=json.dumps({"exit_code": code, "error": error}).encode(), code=code
            )
        return support.FakeProc(argv, out=support.text(f"{command}-output.json").encode())

    def failure(self):
        spec = Spec(
            "Repo: windroc-labs/ensemble\nRef: main",
            self.work,
            task="verify",
            mode="headless",
            sid=SID,
        )
        try:
            delegate = self.make(role="verifier", spec=spec, session_cls=Session)
            result = delegate.run()
        except (ConfigError, SessionError) as error:
            return str(error)
        self.assertNotEqual(result.returncode, 0)
        return result.error

    def test_an_unmodelled_process_fails_naming_it(self):
        with self.assertRaisesRegex(AssertionError, "unmodelled process: /usr/bin/docker ps"):
            self.broker(["/usr/bin/docker", "ps"])

    def test_a_missing_image_fails_before_any_container(self):
        self.refusals["build"] = (
            8,
            "build_failed",
            "Base image mini-op:env is not a local image; build or load it first (ensemble-broker never pulls it).",
        )
        self.assertIn("mini-op:env", self.failure())
        self.assertIn("build", self.calls)
        self.assertNotIn("start", self.calls)

    def test_a_missing_keychain_item_fails_before_any_container(self):
        self.refusals["start"] = (
            5,
            "login_required",
            "No keychain item ensemble-broker-env/mini-op-ssh-key: run ensemble-broker login env mini-op-ssh-key.",
        )
        self.assertIn("ensemble-broker-env/mini-op-ssh-key", self.failure())
        self.assertEqual(self.calls.count("start"), 1)
        self.assertNotIn("docker", self.programs)


class IdentityTest(HarnessCase):
    def start_tui(self):
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        record = self.make(spec=spec).detach()
        return record, DrivenDelegate.attach(record)

    def test_a_foreign_agent_survives_old_run_close(self):
        record, view = self.start_tui()
        original = HarnessSession.live[record.sessions[0]]
        original.stop()
        replacement = HarnessSession.start(
            replace(original.spec, sid=SID.replace("0", "1")), host="agent"
        )
        with self.assertRaisesRegex(ConfigError, "still alive"):
            view.resume(input="answer")
        with self.assertRaisesRegex(ConfigError, "still alive"):
            self.make()
        FakeControl(self.metadata()["drive"]).close()  # its drive ends, its worker gone
        self.assertEqual((view.status().alive, view.status().state), (False, "gone"))
        view.close()
        self.assertTrue(replacement.alive())

    def test_a_cached_agent_cannot_close_its_replacement(self):
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        view = self.make(spec=spec)
        view.run()
        name = self.metadata()["agent_name"]
        original = HarnessSession.live[name]
        original.stop()
        HarnessSession.start(replace(original.spec, sid=SID.replace("0", "1")), host="agent")
        view.close()
        self.assertTrue(RecordingHost.processes["agent", name].alive())


class OuterTest(HarnessCase):
    def test_detach_returns_the_record_named_by_the_outer_starter(self):
        outer = {
            "kind": "pipeline",
            "name": "TASK-1",
            "ctl": ("ensemble-orchestrator",),
            "resume": ("ensemble-orchestrator", "resume", "TASK-1"),
        }
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        record = self.make(spec=spec).detach(outer=outer)
        self.assertEqual(record.name, "TASK-1")
        self.assertEqual(DrivenDelegate.attach(record).resume(), record)


class RunDirTest(HarnessCase):
    def record(self):
        return os.path.join(self.work, "run", "delegate.json")

    def stored(self):
        with open(self.record(), encoding="utf-8") as file:
            return json.load(file)

    def test_session_handlers_round_trip_with_and_without_the_saved_key(self):
        self.make()
        original = self.stored()
        self.assertEqual(original["plan"]["session"]["handlers"], [])
        for present in (True, False):
            with self.subTest(present=present):
                data = json.loads(json.dumps(original))
                if not present:
                    del data["plan"]["session"]["handlers"]
                with open(self.record(), "w", encoding="utf-8") as file:
                    json.dump(data, file)
                view = DrivenDelegate.attach(self.work)
                self.assertEqual(view._state.plan.session.handlers, ())
                view._save(view._state)
                self.assertEqual(self.stored(), original)
                again = DrivenDelegate.attach(self.work)
                self.assertEqual(again._state.plan.session, view._state.plan.session)

    def test_the_default_session_class_is_null(self):
        self.make(session_cls=Session, driver=AgentDriver(HarnessSession))
        self.assertIsNone(self.stored()["session_cls"])
        self.assertIs(DrivenDelegate.attach(self.work).session_cls, Session)

    def test_a_historical_default_class_path_still_loads(self):
        self.make(session_cls=Session, driver=AgentDriver(HarnessSession))
        data = self.stored()
        data["session_cls"] = f"{Session.__module__}:{Session.__qualname__}"
        with open(self.record(), "w", encoding="utf-8") as file:
            json.dump(data, file)
        self.assertIs(DrivenDelegate.attach(self.work).session_cls, Session)

    def test_an_injected_session_class_is_loaded_from_its_saved_path(self):
        self.make()
        self.assertEqual(
            self.stored()["session_cls"],
            "tests.delegate.fakes.harness_session:HarnessSession",
        )
        self.assertIs(DrivenDelegate.attach(self.work).session_cls, HarnessSession)

    def test_a_stored_outcome_loads_unchecked_for_recovers_replay(self):
        # A run begun before summary's limit fell from 4000 to 300 is still written back.
        self.make()
        data = self.stored()
        outcome = Outcome("done", "T", "S" * 4000)
        data["result"] = {
            "returncode": 0,
            "outcome": asdict(outcome),
            "error": "",
            "limit": None,
            "instruction": None,
        }
        with open(self.record(), "w", encoding="utf-8") as file:
            json.dump(data, file)
        self.assertEqual(DrivenDelegate.attach(self.work).outcome(), outcome)

    def corrupt(self, text):
        self.make()
        with open(self.record(), "w", encoding="utf-8") as file:
            file.write(text)

    def test_the_run_record_is_the_runs_only_state(self):
        self.make()
        self.assertEqual(sorted(os.listdir(self.work)), ["agent", "run"])
        self.assertEqual(self.stored()["spec"]["sid"], SID)
        self.assertEqual(
            sorted(os.listdir(os.path.join(self.work, "run"))), ["delegate.json", "delegate.lock"]
        )
        self.assertEqual(os.listdir(self.ensemble), ["config.local.toml"])

    def test_refused_start_creates_nothing(self):
        top = os.path.join(self.home, "a")
        self.work = os.path.join(top, "b", "work")
        HarnessSession.preflight_error = SessionConfigError("preflight refused")
        with self.assertRaisesRegex(ConfigError, "preflight refused"):
            self.make()
        self.assertEqual(len(HarnessSession.preflights), 1)
        HarnessSession.preflight_error = None
        with self.assertRaisesRegex(ConfigError, "absent-role"):
            self.make(role="absent-role")
        self.assertFalse(os.path.lexists(top))
        self.assertEqual(os.listdir(self.ensemble), ["config.local.toml"])

    def test_a_refused_start_on_a_workdir_makes_none_of_its_dirs(self):
        os.mkdir(self.work)
        HarnessSession.preflight_error = SessionConfigError("preflight refused")
        with self.assertRaisesRegex(ConfigError, "preflight refused"):
            self.make()
        self.assertEqual(os.listdir(self.work), [])

    def test_a_custom_drivers_busy_preflight_is_an_ordinary_session_error(self):
        busy = SessionBusy("dummy-lock", "ensemble-x-1 (owner x)")
        HarnessSession.preflight_error = busy
        with self.assertRaises(SessionError) as caught:
            self.make()
        self.assertIs(type(caught.exception), SessionError)
        self.assertEqual(str(caught.exception), str(busy))
        self.assertFalse(os.path.lexists(self.work))

    def test_a_live_run_refuses_a_new_one_before_making_the_agent_dir(self):
        self.make()
        os.rmdir(os.path.join(self.work, "agent"))
        before = self.stored()
        with (
            RunState(Layout(self.work)).run_lock(),
            self.assertRaisesRegex(ConfigError, "delegate is still alive"),
        ):
            self.make()
        self.assertEqual((os.listdir(self.work), self.stored()), (["run"], before))

    def test_a_new_run_of_the_same_sid_counts_the_next_attempt(self):
        self.make()
        self.make()
        self.assertEqual(self.stored()["attempt"], 2)
        self.make(spec=Spec("input", self.work, client="fake", sid=SID.replace("0", "1")))
        self.assertEqual(self.stored()["attempt"], 1)

    def test_a_corrupt_run_record_keeps_a_readable_attempt(self):
        for text, attempt in (("not json", 1), ('{"attempt": 4}', 5)):
            with self.subTest(text=text):
                self.corrupt(text)
                self.make()
                self.assertEqual(self.stored()["attempt"], attempt)

    def test_a_held_run_lock_keeps_a_corrupt_run_record(self):
        self.corrupt("not json")
        with (
            RunState(Layout(self.work)).run_lock(),
            self.assertRaisesRegex(ConfigError, "delegate is still alive"),
        ):
            self.make()
        with open(self.record(), encoding="utf-8") as file:
            self.assertEqual(file.read(), "not json")

    def test_an_old_layout_workdir_names_its_missing_run_record(self):
        self.make()
        os.replace(self.record(), os.path.join(self.work, "delegate.json"))
        with self.assertRaises(ConfigError) as caught:
            DrivenDelegate.attach(self.work, session_cls=HarnessSession)
        self.assertEqual(str(caught.exception), f"invalid {self.record()}: missing metadata")


def tree(top):
    found = {}
    for root, _dirs, files in os.walk(top):
        for name in files:
            with open(os.path.join(root, name), "rb") as file:
                found[os.path.relpath(os.path.join(root, name), top)] = file.read()
    return found


def go_cache(root):
    """A Go module cache below `root`: read-only directories holding a read-only file."""
    module = os.path.join(root, "gopath", "mod", "github.com", "mattn", "go-shellwords@v1.0.12")
    os.makedirs(module)
    with open(os.path.join(module, "go.mod"), "w") as file:
        file.write("module x\n")
    os.chmod(os.path.join(module, "go.mod"), 0o444)
    while module != os.path.join(root, "gopath"):
        os.chmod(module, 0o555)
        module = os.path.dirname(module)


class CleanTest(HarnessCase):
    def agent(self):
        return Layout(self.work).agent

    def refused(self, message, workdir=None, **options):
        for dry in (False, True):
            with self.subTest(dry=dry), self.assertRaises(ConfigError) as caught:
                DrivenDelegate.clean(workdir or self.work, dry=dry, **options)
            self.assertEqual(str(caught.exception), message)

    def test_checkouts_lists_real_src_entries(self):
        self.assertIsNone(DrivenDelegate.checkouts(self.work))
        self.make()
        self.assertEqual(DrivenDelegate.checkouts(self.work), ())
        src = os.path.join(self.agent(), "src")
        real = os.path.join(os.path.realpath(self.work), "agent", "src")
        for path in ("o/r-TASK-1", "p/q-TASK-1"):
            os.makedirs(os.path.join(src, path))
        elsewhere = os.path.join(self.home, "elsewhere")
        os.makedirs(os.path.join(elsewhere, "n-TASK-1"))
        os.symlink(os.path.join(elsewhere, "n-TASK-1"), os.path.join(src, "o", "s-TASK-1"))
        os.symlink(elsewhere, os.path.join(src, "x"))
        with open(os.path.join(src, "o", "f-TASK-1"), "w") as file:
            file.write("x")
        self.assertEqual(
            DrivenDelegate.checkouts(self.work),
            (os.path.join(real, "o", "r-TASK-1"), os.path.join(real, "p", "q-TASK-1")),
        )
        moved = os.path.join(self.home, "moved")
        os.replace(self.agent(), moved)
        os.symlink(moved, self.agent())
        self.assertEqual(DrivenDelegate.checkouts(self.work), ())

    def test_clean_deletes_agent_files_and_keeps_run_records(self):
        self.make()
        deep = os.path.join(self.agent(), "tmp", "deep")
        os.makedirs(deep)
        os.makedirs(os.path.join(self.agent(), "src", "o", "r-TASK-1"))
        with open(os.path.join(deep, "pr.md"), "w") as file:
            file.write("x")
        os.chmod(os.path.join(deep, "pr.md"), 0o444)
        with open(os.path.join(self.work, "notes.md"), "w") as file:
            file.write("kept")
        run = tree(Layout(self.work).run)
        name = DrivenDelegate.attach(self.work).status().name
        self.assertTrue(DrivenDelegate.clean(self.work, dry=True))
        self.assertTrue(os.path.isdir(deep))
        self.assertTrue(DrivenDelegate.clean(self.work))
        self.assertFalse(os.path.lexists(self.agent()))
        self.assertEqual(tree(Layout(self.work).run), run)
        self.assertEqual(sorted(os.listdir(self.work)), ["notes.md", "run"])
        self.assertIsNone(DrivenDelegate.checkouts(self.work))
        self.assertEqual(DrivenDelegate.attach(self.work).status().name, name)
        self.assertFalse(DrivenDelegate.clean(self.work))
        never = os.path.join(self.home, "never")
        self.assertFalse(DrivenDelegate.clean(never))
        self.assertFalse(os.path.lexists(never))

    def test_scratch_deletes_only_checkouts_and_temp_files_never_following_a_link(self):
        self.make()
        agent = self.agent()
        for path in ("src/o/r-TASK-1/f", "publish/Research/r.md", "prd.md"):
            os.makedirs(os.path.dirname(os.path.join(agent, path)), exist_ok=True)
            with open(os.path.join(agent, path), "w") as file:
                file.write("x")
        elsewhere = os.path.join(self.home, "elsewhere")
        os.makedirs(elsewhere)
        open(os.path.join(elsewhere, "keep"), "w").close()
        os.symlink(elsewhere, os.path.join(agent, "tmp"))
        run = tree(Layout(self.work).run)
        self.assertTrue(DrivenDelegate.clean(self.work, dry=True, scratch=True))
        self.assertEqual(sorted(os.listdir(agent)), ["prd.md", "publish", "src", "tmp"])
        self.assertTrue(DrivenDelegate.clean(self.work, scratch=True))
        self.assertEqual(os.listdir(agent), ["prd.md"])
        self.assertEqual(os.listdir(elsewhere), ["keep"])
        self.assertEqual(tree(Layout(self.work).run), run)
        self.assertFalse(DrivenDelegate.clean(self.work, scratch=True))
        self.assertFalse(DrivenDelegate.clean(os.path.join(self.home, "never"), scratch=True))

    def test_clean_and_scratch_delete_a_read_only_go_module_cache(self):
        for scratch, left in ((True, ["prd.md"]), (False, None)):
            with self.subTest(scratch=scratch):
                self.make()
                go_cache(os.path.join(self.agent(), "tmp"))
                go_cache(os.path.join(self.agent(), "src", "o", "r-TASK-1"))
                open(os.path.join(self.agent(), "prd.md"), "w").close()
                self.assertTrue(DrivenDelegate.clean(self.work, scratch=scratch))
                self.assertEqual(
                    os.listdir(self.agent()) if os.path.lexists(self.agent()) else None, left
                )

    def test_scratch_refuses_a_live_run_or_one_whose_liveness_is_unknown(self):
        self.make()
        os.makedirs(os.path.join(self.agent(), "tmp"))
        with RunState(Layout(self.work)).run_lock():
            self.refused(f"{self.work}: delegate is still alive", scratch=True)
        with open(Layout(self.work).run + "/delegate.json", "w") as file:
            file.write("not json")
        self.refused(f"{self.work}: delegate liveness unknown", scratch=True)
        self.assertTrue(os.path.isdir(os.path.join(self.agent(), "tmp")))

    def test_scratch_refuses_an_agent_session_left_open_after_its_run(self):
        view = self.make(spec=Spec("input", self.work, mode="tui", client="fake", sid=SID))
        gives_up()
        view.run()
        self.assertFalse(view.alive())
        os.makedirs(os.path.join(self.agent(), "tmp"), exist_ok=True)
        self.refused(f"{self.work}: its agent session is still open", scratch=True)
        self.assertTrue(os.path.isdir(os.path.join(self.agent(), "tmp")))

    def test_clean_refuses_a_live_run(self):
        self.make()
        with RunState(Layout(self.work)).run_lock():
            self.refused(f"{self.work}: delegate is still alive")
        with open(Layout(self.work).run + "/delegate.json", "w") as file:
            file.write("not json")
        with RunState(Layout(self.work)).run_lock():
            self.refused(f"{self.work}: delegate is still alive")
        self.assertTrue(os.path.isdir(self.agent()))

    def test_clean_without_agent_files_refuses_a_live_run(self):
        self.make()
        shutil.rmtree(self.agent())
        with RunState(Layout(self.work)).run_lock():
            files = tree(self.work)
            self.refused(f"{self.work}: delegate is still alive")
            self.assertEqual(tree(self.work), files)

    def test_clean_without_agent_files_keeps_an_ended_run(self):
        self.make()
        shutil.rmtree(self.agent())
        files = tree(self.work)
        for dry in (False, True):
            with self.subTest(dry=dry):
                self.assertFalse(DrivenDelegate.clean(self.work, dry=dry))
                self.assertEqual(tree(self.work), files)

    def test_clean_refuses_a_live_detached_run(self):
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        self.make(spec=spec, host="agent").detach()
        self.refused(f"{self.work}: delegate is still alive")
        self.assertTrue(os.path.isdir(self.agent()))

    def test_clean_refuses_an_agent_dir_out_of_place(self):
        self.make()
        elsewhere = os.path.join(self.home, "elsewhere")
        os.makedirs(os.path.join(elsewhere, "keep"))
        os.replace(self.agent(), os.path.join(self.home, "agent"))
        os.symlink(elsewhere, self.agent())
        self.refused(f"not a real directory: {self.agent()}, refusing to touch")
        self.assertTrue(os.path.isdir(os.path.join(elsewhere, "keep")))
        os.unlink(self.agent())
        with open(self.agent(), "w") as file:
            file.write("x")
        self.refused(f"not a real directory: {self.agent()}, refusing to touch")
        os.unlink(self.agent())
        os.replace(os.path.join(self.home, "agent"), self.agent())
        link = os.path.join(self.home, "link")
        os.symlink(self.work, link)
        self.refused(f"not a real directory: {link}/agent, refusing to touch", link)
        self.assertTrue(os.path.isdir(self.agent()))

    def test_clean_holds_the_run_mutex_while_it_deletes(self):
        self.make()
        held = []

        def rmtree(path, **_kwargs):
            with open(Layout(self.work).run + "/delegate.lock", "rb") as file:
                try:
                    fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    held.append(path)

        with mock.patch.object(shutil, "rmtree", side_effect=rmtree):
            DrivenDelegate.clean(self.work)
        self.assertEqual(held, [self.agent()])

    def test_a_new_run_after_clean_lays_out_again_with_the_next_attempt(self):
        self.make()
        DrivenDelegate.clean(self.work)
        self.make()
        self.assertTrue(os.path.isdir(self.agent()))
        self.assertEqual(self.metadata()["attempt"], 2)


class RunEndTest(HarnessCase):
    """A run's end clears agent/tmp but an `out` directly in it, unless its agent session is open."""

    def setUp(self):
        super().setUp()
        self.tmp = Layout(self.work).tmp
        self.elsewhere = os.path.join(self.home, "elsewhere")
        os.makedirs(self.elsewhere)
        open(os.path.join(self.elsewhere, "keep"), "w").close()

    def kept(self, **spec):
        """A run whose `out` is agent/tmp/deliverable.md."""
        out = os.path.join(self.tmp, "deliverable.md")
        return self.make(spec=Spec("input", self.work, sid=SID, client="fake", out=out, **spec))

    def fill(self):
        os.makedirs(os.path.join(self.tmp, "deep"), exist_ok=True)
        for name in ("deep/pr.md", "msg", "deliverable.md"):
            with open(os.path.join(self.tmp, name), "w") as file:
                file.write("x")
        os.symlink(self.elsewhere, os.path.join(self.tmp, "link"))

    def test_a_finished_run_leaves_only_its_out_there(self):
        view = self.kept(task="echo")
        self.fill()
        view.run()
        self.assertEqual(os.listdir(self.tmp), ["deliverable.md"])
        self.assertEqual(os.listdir(self.elsewhere), ["keep"])

    def test_a_run_whose_out_lies_elsewhere_or_without_a_temp_dir_ends_cleanly(self):
        view = self.make()
        self.fill()
        view.run()
        self.assertFalse(os.path.lexists(self.tmp))
        self.assertEqual(os.listdir(self.elsewhere), ["keep"])
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.make().run()
        self.assertNotIn(self.tmp, err.getvalue())

    def test_a_symlinked_temp_dir_is_unlinked_never_followed(self):
        view = self.make()
        os.symlink(self.elsewhere, self.tmp)
        view.run()
        self.assertFalse(os.path.lexists(self.tmp))
        self.assertEqual(os.listdir(self.elsewhere), ["keep"])

    def test_a_run_that_raises_clears_it_too(self):
        view = self.make()
        self.fill()

        def fail(_event):
            raise RuntimeError("boom")

        with self.assertRaisesRegex(RuntimeError, "boom"):
            view.run(on_event=fail)
        self.assertFalse(os.path.lexists(self.tmp))

    def test_an_agent_session_left_open_keeps_it(self):
        view = self.make(spec=Spec("input", self.work, mode="tui", client="fake", sid=SID))
        self.fill()
        gives_up()
        view.run()
        self.assertEqual(sorted(os.listdir(self.tmp)), ["deep", "deliverable.md", "link", "msg"])

    def test_a_read_only_go_module_cache_is_cleared_too(self):
        for kept, left in ((True, ["deliverable.md"]), (False, None)):
            with self.subTest(kept=kept):
                view = self.kept(task="echo") if kept else self.make()
                self.fill()
                go_cache(self.tmp)
                view.run()
                self.assertEqual(os.listdir(self.tmp) if os.path.lexists(self.tmp) else None, left)

    def test_a_removal_error_is_printed_and_the_result_kept(self):
        view = self.make()
        self.fill()
        with (
            mock.patch.object(shutil, "rmtree", side_effect=PermissionError("no")),
            mock.patch("sys.stderr", new_callable=io.StringIO) as err,
        ):
            result = view.run()
        self.assertEqual((result.outcome.status, err.getvalue()[-13:]), ("done", "delegate: no\n"))


class PreviewTest(HarnessCase):
    def test_preview_names_the_sessions_hosts_and_callback_and_writes_nothing(self):
        for mode, agent in (("tui", f"dummy-tester-{SID[:8]}"), ("headless", None)):
            with self.subTest(mode=mode):
                spec = Spec("input", self.work, task="echo", mode=mode, client="fake", sid=SID)
                preview = DrivenDelegate.preview(
                    "dummy-tester",
                    spec,
                    session_cls=HarnessSession,
                    callback=["writeback", "--issue", "ticket-id"],
                )
                self.assertEqual(
                    (
                        preview.agent,
                        preview.host,
                        preview.callback,
                        preview.plan.sid,
                        preview.plan.spec.mode,
                    ),
                    (
                        agent,
                        "agent",
                        ("writeback", "--issue", "ticket-id"),
                        SID,
                        mode,
                    ),
                )
                self.assertTrue(preview.plan.prompt.endswith("\n# Input\n\ninput\n"))
        self.assertFalse(os.path.exists(self.work))
        self.assertEqual(os.listdir(self.ensemble), ["config.local.toml"])
        self.assertEqual(
            (HarnessSession.started, RecordingHost.launches, harness_launches()), ([], [], [])
        )

    def test_preview_refuses_an_unknown_role_like_the_constructor(self):
        spec = Spec("input", self.work, client="fake", sid=SID)
        with self.assertRaisesRegex(ConfigError, "absent-role"):
            DrivenDelegate.preview("absent-role", spec, session_cls=HarnessSession)
        self.assertFalse(os.path.exists(self.work))


class InstructTest(HarnessCase):
    def test_instruct_delivers_the_composed_prompt_without_state_or_session(self):
        out = os.path.join(self.work, "prompt.md")
        result = DrivenDelegate.instruct(
            "dummy-tester", Spec("Hello.", self.work, task="echo", out=out)
        )
        self.assertEqual(result, Result(0, None, instruction=out))
        composer = Composer()
        want = composer.render(
            composer.resolve("dummy-tester", "echo"),
            RunParams(input="Hello.", out=out, workdir=Layout(self.work).agent),
        )
        with open(out, encoding="utf-8") as file:
            self.assertEqual(file.read(), want)
        self.assertIn("**Your task**: `echo`", want)
        self.assertEqual(os.listdir(self.work), ["prompt.md"])
        self.assertEqual(
            (HarnessSession.started, RecordingHost.launches, harness_launches()), ([], [], [])
        )


class CheckTuiTest(HarnessCase):
    def setUp(self):
        super().setUp()
        HarnessSession.reset(showable=True)
        self.events = os.path.join(self.home, "events")

    def check(self, **kw):
        DrivenDelegate.check_tui(session_cls=HarnessSession, **kw)

    def test_the_agents_host_places_the_display_and_the_events_file_is_checked_not_made(self):
        with (
            mock.patch.object(FakeViewer, "layout", autospec=True) as layout,
        ):
            self.check(display="opener/right", events=self.events)
        [(_, host)] = [call.args for call in layout.call_args_list]
        self.assertEqual(
            (host.name, layout.call_args.kwargs), ("agent", {"display": "opener/right"})
        )
        self.assertFalse(os.path.exists(self.events))

    def test_a_host_refusing_the_display_or_the_events_is_a_config_error(self):
        for owner, method, error in (
            (FakeViewer, "layout", SessionError("no pane to show the TUI beside (not in tmux)")),
            (FakeViewer, "layout", SessionConfigError("no tmux session absent")),
            (
                FakeViewer,
                "layout",
                SessionConfigError("display 'a/sideways': not <opener>[/<split>]"),
            ),
        ):
            with (
                self.subTest(method=method, error=error),
                mock.patch.object(owner, method, side_effect=error),
                self.assertRaises(ConfigError) as caught,
            ):
                self.check(display="d", events=self.events)
            self.assertEqual(str(caught.exception), str(error))
        missing = os.path.join(self.home, "missing", "events")
        with self.assertRaises(ConfigError) as caught:
            self.check(display="d", events=missing)
        self.assertEqual(str(caught.exception), f"events file {missing}: No such file or directory")
        self.assertEqual((RecordingHost.launches, harness_launches()), ([], []))


class AttendedBusyTest(HarnessCase):
    """An attended TUI whose harness ended before its agent went live, by its recorded result."""

    BUSY = "busy: lock dummy-lock is held by ensemble-other-x (owner other)"

    def started(self, error, lock="dummy-lock"):
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        delegate = self.make(spec=spec, host="agent")
        state = delegate._state
        environment = (
            None if lock is None else EnvironmentSpec(image="x", gateway="workers", lock=lock)
        )
        session = replace(state.plan.session, environment=environment)
        state = replace(
            state, plan=replace(state.plan, session=session), result=Result(1, None, error)
        )
        with (
            mock.patch.object(delegate, "_answered_agent", return_value=None),
            mock.patch.object(delegate, "_read", return_value=state),
        ):
            delegate._started(state)

    def test_attended_tui_busy_raises_an_ordinary_session_error_naming_the_holder(self):
        for error in (f"tui: {self.BUSY}", self.BUSY):
            with self.subTest(error), self.assertRaises(SessionError) as caught:
                self.started(error)
            self.assertIs(type(caught.exception), SessionError)
            self.assertEqual(str(caught.exception), self.BUSY)

    def test_a_foreign_or_unprintable_busy_message_keeps_did_not_start(self):
        for error, lock in (
            (f"tui: {self.BUSY}", "other-lock"),
            (f"tui: {self.BUSY}", None),
            ("tui: busy: lock dummy-lock is held by \x1b[2J (owner o)", "dummy-lock"),
            ("tui: broker: start failed", "dummy-lock"),
        ):
            with self.subTest(error=error, lock=lock), self.assertRaises(SessionError) as caught:
                self.started(error, lock)
            self.assertNotIsInstance(caught.exception, SessionBusy)
            self.assertRegex(str(caught.exception), r"^tui session \S+ did not start\b")


class LaunchColorTest(HarnessCase):
    def setUp(self):
        super().setUp()
        self.clock = Clock(self)

    def launched(
        self, agent: mock.Mock, *, drive: Callable[[], bool] = lambda: True
    ) -> tuple[str, str]:
        """The stderr of a tui launch whose agent `agent` is live at once (unless its alive is set)
        and whose drive runs while `drive()`, and its agent name.
        """
        err = self.enterContext(mock.patch("sys.stderr", new_callable=io.StringIO))
        delegate = self.make(spec=Spec("input", self.work, mode="tui", client="fake", sid=SID))
        if agent.alive.side_effect is None:
            agent.alive.return_value = True
        with (
            mock.patch.object(delegate, "_probed_agent", return_value=(agent, None)),
            mock.patch.object(delegate, "_publish_record"),
            mock.patch.object(delegate, "_running", side_effect=lambda _drive: drive()),
        ):
            delegate._started(delegate._state)
        return err.getvalue(), delegate._state.agent_name

    def opening(self, *pending: str | None) -> mock.Mock:
        """A live agent whose opening_pending gives `pending`, then its last for good."""
        agent = mock.Mock(spec=["alive", "status", "opening_pending"])
        values = itertools.chain(pending, itertools.repeat(pending[-1]))
        type(agent).opening_pending = mock.PropertyMock(side_effect=lambda: next(values))
        return agent

    def test_an_opening_unsent_at_the_color_wait_is_said(self):
        said, name = self.launched(self.opening("/color"))
        self.assertEqual(
            said,
            f"delegate: session {name}: color not set: not ready within {COLOR_WAIT} s; "
            f"ensemble-session send {name} /color\n",
        )

    def test_an_opening_sent_in_time_or_none_is_not_said(self):
        for agent in (
            self.opening("/color", "/color", None),
            mock.Mock(spec=["alive", "status"]),
        ):
            with self.subTest(agent=agent):
                self.assertEqual(self.launched(agent)[0], "")

    def test_its_drive_or_tui_gone_ends_the_wait_at_once_unsaid(self):
        def soon() -> bool:
            return self.clock.now < 2

        for gone in ("drive", "tui"):
            with self.subTest(gone=gone):
                self.clock.now = 0.0
                agent = self.opening("/color")
                if gone == "tui":
                    agent.alive.side_effect = soon
                said, _name = self.launched(agent, drive=soon if gone == "drive" else lambda: True)
                self.assertEqual((said, self.clock.now < 3), ("", True), self.clock.now)


class LaunchTest(HarnessCase):
    def test_the_launchers_variables_stay_out_of_the_harness_environment(self):
        launcher = {"ENSEMBLE_SID": SID, "ENSEMBLE_EVENTS": "/manager/events", "KEPT": "kept"}
        lease = (
            "ENSEMBLE_MANAGER",
            "ENSEMBLE_HOLDER_ID",
            "ENSEMBLE_HOLDER_GENERATION",
            "ENSEMBLE_HOLDER_PID",
        )
        with mock.patch.dict(os.environ, launcher | dict.fromkeys(lease, "1")):
            self.make().detach()
        environment = harness_launches()[0]["env"]
        for name in ("ENSEMBLE_SID", "ENSEMBLE_EVENTS", *lease):
            self.assertNotIn(name, environment)
        self.assertEqual(environment["KEPT"], "kept")

    def test_the_harness_is_no_agent_session_so_carries_no_marker(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("ENSEMBLE_AGENT_SESSION", None)
            self.make().detach()
        self.assertNotIn("ENSEMBLE_AGENT_SESSION", harness_launches()[0]["env"])


class SaveFailureTest(HarnessCase):
    def test_a_failed_result_save_still_hands_on_the_runs_real_finished(self):
        delegate, events, real = self.make(), [], os.replace
        record = os.path.join(Layout(self.work).run, "delegate.json")

        def replace(src, dst):
            if str(dst) == record:
                raise OSError("disk full")
            real(src, dst)

        with mock.patch.object(os, "replace", replace):
            result = delegate.run(on_event=events.append)
        finished = [e for e in events if isinstance(e, Finished)]
        self.assertEqual([e.result for e in finished], [result])
        self.assertEqual(result.outcome.status, "done")


class UsageSession(HarnessSession):
    clients = []

    @classmethod
    def usage(cls, client):
        cls.clients.append(client)


class OverlayTest(HarnessCase):
    def test_usage_reads_the_client_the_owner_overlays_resolve(self):
        UsageSession.clients = []
        overlays = ({"roles": {"dummy-tester": {"client": "claude"}}},)
        DrivenDelegate.usage("dummy-tester", session_cls=UsageSession)
        DrivenDelegate.usage("dummy-tester", overlays=overlays, session_cls=UsageSession)
        DrivenDelegate.usage(
            "dummy-tester",
            overlays=({"roles": {"dummy-tester": {"client": "opencode", "tier": 3}}},),
            session_cls=UsageSession,
        )
        self.assertEqual(UsageSession.clients, ["fake", "claude", "opencode"])

    def test_owner_overlays_shape_the_persisted_run(self):
        self.make()
        self.assertEqual(self.metadata()["plan"]["run"]["effort"], "low")
        self.make(overlays=({"roles": {"dummy-tester": {"effort": "high"}}},))
        self.assertEqual(self.metadata()["plan"]["run"]["effort"], "high")

    def test_a_record_without_overlays_is_corrupt(self):
        self.make()
        path = self.state_record()
        with open(path, encoding="utf-8") as file:
            data = json.load(file)
        self.assertEqual(data["overlays"], [])
        del data["overlays"]
        with open(path, "w", encoding="utf-8") as file:
            json.dump(data, file)
        with self.assertRaises(ConfigError) as caught:
            DrivenDelegate.attach(self.work, session_cls=HarnessSession)
        self.assertEqual(str(caught.exception), f"invalid {path}: 'overlays'")

    def test_resume_resolves_with_the_overlays_it_was_built_with(self):
        record = self.make(overlays=({"roles": {"dummy-tester": {"effort": "high"}}},)).detach()
        self.stop(record)
        DrivenDelegate.attach(self.work, session_cls=HarnessSession).resume()
        self.assertEqual(self.metadata()["plan"]["run"]["effort"], "high")


class ResumeInputTest(HarnessCase):
    def waiting(self, *, mode="headless", outer=None, close=True):
        events = os.path.join(self.home, "events")
        spec = Spec(
            "initial", self.work, task="echo", mode=mode, client="fake", sid=SID, events=events
        )
        record = self.make(spec=spec, display="none").detach(outer=outer)
        view = DrivenDelegate.attach(self.work, session_cls=HarnessSession)
        view._complete(
            Result(0, Outcome("needs_input", "Question", "Answer needed", questions=["Which?"]))
        )
        if close:
            view.close()
        return view, record

    def test_answers_use_the_same_identity_placement_and_new_prompt(self):
        for mode in ("headless", "tui"):
            with self.subTest(mode=mode):
                view, record = self.waiting(mode=mode)
                before = self.metadata()
                resumed = view.resume(input="The answer.")
                state = self.metadata()
                self.assertEqual(
                    (resumed.sid, resumed.name, resumed.display),
                    (record.sid, record.name, record.display),
                )
                self.assertEqual(state["attempt"], before["attempt"] + 1)
                self.assertIsNone(state["result"])
                self.assertEqual(state["spec"]["input"], "The answer.")
                for key in (
                    "role",
                    "display",
                    "callback",
                    "events",
                    "agent_name",
                    "harness_name",
                    "outer",
                    "overlays",
                ):
                    self.assertEqual(state[key], before[key], key)
                for key in ("sid", "task", "mode"):
                    self.assertEqual(state["spec"][key], before["spec"][key], key)
                self.assertTrue(
                    HarnessSession.preflights[-1]["spec"].prompt.endswith(
                        "\n# Input\n\nThe answer.\n"
                    )
                )
                launches = len(harness_launches())
                with self.assertRaises(ConfigError):
                    view.resume(input="The answer.")
                self.assertEqual(len(harness_launches()), launches)
                view.close()

    def test_input_refuses_every_outcome_other_than_needs_input_without_changes(self):
        for status in (None, "done", "failed"):
            with self.subTest(status=status):
                view = self.make()
                if status is not None:
                    HarnessSession.script = (
                        Reply(
                            block(
                                {
                                    "status": status,
                                    "title": "T",
                                    "summary": "S",
                                    "deliverable": "body",
                                }
                            )
                        ),
                        Exited(0),
                    )
                    view.run()
                before, launches = self.metadata(), len(harness_launches())
                with self.assertRaisesRegex(ConfigError, "needs_input"):
                    view.resume(input="answer")
                self.assertEqual(self.metadata(), before)
                self.assertEqual(len(harness_launches()), launches)

    def test_input_refuses_a_still_live_harness(self):
        view, _ = self.waiting(close=False)
        before, launches = self.metadata(), len(harness_launches())
        with self.assertRaisesRegex(ConfigError, "alive"):
            view.resume(input="answer")
        self.assertEqual(self.metadata(), before)
        self.assertEqual(len(harness_launches()), launches)

    def test_pipeline_input_is_refused_only_at_the_delegate_cli(self):
        from ensemble.delegate import cli

        outer = {
            "kind": "pipeline",
            "name": "TASK-1",
            "ctl": ("ensemble-orchestrator",),
            "resume": ("ensemble-orchestrator", "resume", "TASK-1"),
        }
        view, record = self.waiting(outer=outer)
        self.assertEqual(view.ctl(), ("ensemble-orchestrator",))
        before, launches = self.metadata(), len(harness_launches())

        class Bound(DrivenDelegate):
            @classmethod
            def attach(cls, key, *, sid=None):
                return DrivenDelegate.attach(key, sid=sid, session_cls=HarnessSession)

        for arguments, stdin in (
            (["resume", self.work, "--input", "answer"], ""),
            (["resume", "--input", "answer"], record.to_line()),
        ):
            out, err = io.StringIO(), io.StringIO()
            with (
                mock.patch("sys.stdout", out),
                mock.patch("sys.stderr", err),
                mock.patch("sys.stdin", io.StringIO(stdin)),
            ):
                code = cli.main(arguments, delegate_cls=Bound)
            self.assertEqual((code, out.getvalue()), (2, ""))
            self.assertIn("pipeline", err.getvalue())
            self.assertEqual(self.metadata(), before)
            self.assertEqual(len(harness_launches()), launches)
        self.assertEqual(view.resume(input="answer").ctl, ("ensemble-orchestrator",))


class ResumeModeTest(HarnessCase):
    def gave_up(self, mode):
        """A run ended without an outcome: a tui one's worker gave up, its session left open; a
        headless one's detached harness ended.
        """
        spec = Spec("input", self.work, mode=mode, client="fake", sid=SID)
        if mode == "tui":
            view = self.make(spec=spec)
            gives_up()
        else:
            self.make(spec=spec).detach()
            view = DrivenDelegate.attach(self.work, session_cls=HarnessSession)
            view.close()
            HarnessSession.script = (Reply("No outcome block."), Exited(0))
        self.assertIsNone(view.run().outcome)
        self.assertFalse(view.alive())

    def resumed(self, **mode):
        launches = len(harness_launches())
        view = DrivenDelegate.attach(self.work, session_cls=HarnessSession)
        record = view.resume(**mode)
        self.assertEqual(len(harness_launches()), launches + 1)
        self.assertEqual(view.resume(**mode), record)
        self.assertEqual(len(harness_launches()), launches + 1)
        return record, self.metadata()

    def modes(self, state):
        return (state["spec"]["mode"], state["plan"]["session"]["mode"], state["attempt"])

    def test_a_role_showing_none_runs_at_the_none_display(self):
        for show, mode, want in (
            ("none", "tui", "none"),
            ("open {{session}}", "tui", "mgr"),
            ("", "headless", "mgr"),
        ):
            with self.subTest(show=show, mode=mode):
                spec = Spec("input", self.work, mode=mode, client="fake", sid=SID)
                made = self.make(spec=spec, overlays=({"show": show},), display="mgr")
                self.assertEqual((made.driver.display, self.metadata()["display"]), (want, want))
        spec = Spec("input", self.work, mode="headless", client="fake", sid=SID)
        record = self.make(spec=spec, overlays=({"show": "none"},), display="mgr").detach()
        self.assertEqual((record.display, harness_launches()[-1]["display"]), ("none", "none"))
        view = DrivenDelegate.attach(self.work, session_cls=HarnessSession)
        view.close()
        HarnessSession.script = (Reply("No outcome block."), Exited(0))
        self.assertIsNone(view.run().outcome)
        record, state = self.resumed(mode="tui")
        self.assertEqual((record.sid, record.display, state["display"]), (SID, "none", "none"))
        agent = RecordingHost.processes["agent", state["agent_name"]]
        self.assertEqual(agent.display(), "none")

    def test_a_tui_run_that_gave_up_resumes_headless_with_its_sid_and_records_the_mode(self):
        self.gave_up("tui")
        agent = HarnessSession.live[self.metadata()["agent_name"]]
        record, state = self.resumed(mode="headless")
        self.assertEqual((record.sid, record.sessions), (SID, ()))
        self.assertEqual(self.modes(state), ("headless", "headless", 2))
        self.assertEqual((state["spec"]["name"], state["spec"]["events"]), (None, None))
        self.assertEqual(agent.alive(), False)
        self.assertEqual(HarnessSession.preflights[-1]["spec"].mode, "headless")

    def test_without_a_mode_a_tui_run_resumes_in_tui(self):
        self.gave_up("tui")
        before = self.metadata()
        record, state = self.resumed()
        self.assertEqual(record.sessions, (before["agent_name"],))
        self.assertEqual(self.modes(state), ("tui", "tui", 2))
        self.assertEqual(
            (state["spec"]["name"], state["spec"]["events"]),
            (before["spec"]["name"], before["spec"]["events"]),
        )

    def test_a_headless_run_resumes_in_tui_with_its_agent_session_live(self):
        self.gave_up("headless")
        record, state = self.resumed(mode="tui")
        self.assertEqual(record.sessions, (state["agent_name"],))
        self.assertEqual(self.modes(state), ("tui", "tui", 2))
        self.assertEqual(
            (state["spec"]["name"], state["spec"]["events"]), (state["agent_name"], state["events"])
        )
        self.assertEqual(HarnessSession.live[state["agent_name"]].sid, SID)

    def test_a_mode_detach_refuses_is_refused_changing_nothing(self):
        self.gave_up("tui")
        before = self.metadata()
        launches = len(harness_launches())
        view = DrivenDelegate.attach(self.work, session_cls=HarnessSession)
        for mode in ("in_process", "remote"):
            with self.subTest(mode=mode), self.assertRaisesRegex(ConfigError, repr(mode)):
                view.resume(mode=mode)
        self.assertEqual(self.metadata(), before)
        self.assertEqual(len(harness_launches()), launches)
        self.assertEqual(HarnessSession.live[before["agent_name"]].alive(), True)


class StatusTest(HarnessCase):
    def test_content_is_the_current_status_line(self):
        self.make().detach()
        view = DrivenDelegate.attach(self.work, session_cls=HarnessSession)
        self.assertEqual(view.content(), view.status().to_line())

    def test_tui_content_uses_the_current_result(self):
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        record = self.make(spec=spec).detach()
        view = DrivenDelegate.attach(record)
        agent = HarnessSession.live[record.sessions[0]]
        drive = FakeControl(self.metadata()["drive"])
        block = "```ensemble-outcome\n{}\n```"
        for result, given, expected in (
            (None, "agent content", "agent content"),
            (Result(0, None), "agent content", "agent content"),
            (None, f"Built X.\n{block}\nDone.", block),
            (Result(0, Outcome("done", "T", "S")), "agent content", None),
            (Result(1, None, error="boom"), "agent content", None),
        ):
            with self.subTest(result=result, given=given):
                if result is not None:
                    view._complete(result)
                with mock.patch.object(agent, "content", return_value=given) as content:
                    self.assertEqual(
                        view.content(),
                        expected if expected is not None else view.status().to_line(),
                    )
                    self.assertEqual(content.call_count, int(expected is not None))
                self.assertTrue(drive.running())

    def test_tui_content_without_its_agent_falls_back_to_status(self):
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        record = self.make(spec=spec).detach()
        HarnessSession.live[record.sessions[0]].stop()
        view = DrivenDelegate.attach(record)
        self.assertEqual(view.content(), view.status().to_line())

    def test_its_state_is_working_while_the_run_lives_else_gone(self):
        record = self.make().detach()
        view = DrivenDelegate.attach(self.work, session_cls=HarnessSession)
        self.assertEqual((view.status().state, view.status().hidden), ("working", False))
        self.stop(record)
        self.assertEqual((view.status().alive, view.status().state), (False, "gone"))

    def test_an_ended_tui_runs_state_is_its_agents_word_until_it_is_gone(self):
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        record = self.make(spec=spec).detach()
        view = DrivenDelegate.attach(record)
        view._complete(Result(0, Outcome("done", "T", "S")))
        FakeControl(self.metadata()["drive"]).close()
        agent = HarnessSession.live[record.sessions[0]]
        for word in ("done", "working", "blocked"):
            with (
                self.subTest(word=word),
                mock.patch.object(
                    agent, "status", return_value=replace(agent.status(), state=word)
                ),
            ):
                status = view.status()
                self.assertEqual(
                    (status.alive, status.state, status.outcome), (False, word, "done")
                )
                self.assertFalse(view.alive())
        agent.stop()
        self.assertEqual((view.status().alive, view.status().state), (False, "gone"))

    def test_a_tui_run_ended_without_an_outcome_shows_its_agents_word(self):
        view = self.make(spec=Spec("input", self.work, mode="tui", client="fake", sid=SID))
        gives_up()
        self.assertIsNone(view.run().outcome)
        status = view.status()
        self.assertEqual((status.alive, status.state, status.outcome), (False, "working", None))
        agent = HarnessSession.live[self.metadata()["agent_name"]]
        blocked = replace(agent.status(), state="blocked")
        with mock.patch.object(agent, "status", return_value=blocked):
            self.assertEqual(view.status().state, "blocked")


class EndingHarnessTest(HarnessCase):
    """A harness whose sessions were killed (tmux kill-session) still ends its run, under the run
    lock, after they are gone: it is live until it lets go.
    """

    def killed(self, mode):
        HarnessSession.reset()
        self.work = os.path.join(self.home, f"run-{mode}")
        spec = Spec("input", self.work, mode=mode, client="fake", sid=SID)
        record = self.make(spec=spec).detach()
        self.stop(record)
        if mode == Mode.TUI:
            HarnessSession.live[record.sessions[0]].stop()
        return record, DrivenDelegate.attach(record)

    def test_it_is_alive_and_resume_starts_nothing_until_its_run_lock_is_let_go(self):
        for mode in (Mode.TUI, Mode.HEADLESS):
            with self.subTest(mode=mode):
                record, view = self.killed(mode)
                with RunState(Layout(self.work)).run_lock():
                    status = view.status()
                    self.assertEqual(
                        (view.alive(), status.alive, status.state), (True, True, "working")
                    )
                    launches = len(harness_launches())
                    self.assertEqual(view.resume(), record)
                    self.assertEqual(len(harness_launches()), launches)
                self.assertEqual((view.alive(), view.status().alive), (False, False))
                self.assertEqual(view.resume().sid, SID)
                self.assertEqual(len(harness_launches()), launches + 1)

    def rows(self, state):
        row = SessionRow(
            self.metadata()["agent_name"], SID, "fake", state, None, None, None, own=False
        )
        return mock.patch.object(RecordingHost, "rows", return_value=[row], create=True)

    def test_resume_refuses_busy_while_its_host_lists_its_worker_ownerless(self):
        """Its harness gone, its container still reaped (WDRC-365): never the dying Record."""
        for mode in (Mode.TUI, Mode.HEADLESS):
            with self.subTest(mode=mode):
                record, view = self.killed(mode)
                busy = str(
                    SessionBusy(
                        self.metadata()["agent_name"],
                        "previous worker generation (live or unknown)",
                    )
                )
                launches = len(harness_launches())
                with RunState(Layout(self.work)).run_lock():
                    for state in (StatusState.UNKNOWN, StatusState.DEAD):
                        for kwargs in ({}, {"input": "the answer"}):
                            with self.rows(state), self.assertRaises(SessionError) as refused:
                                view.resume(**kwargs)
                            self.assertEqual(str(refused.exception), busy)
                    self.assertEqual(
                        (len(harness_launches()), self.metadata()["attempt"]), (launches, 1)
                    )
                    with self.rows(StatusState.WORKING):
                        self.assertEqual(view.resume(), record)
                with self.rows(StatusState.DEAD):
                    self.assertEqual(view.resume().sid, SID)
                self.assertEqual(len(harness_launches()), launches + 1)


class UnansweredTest(HarnessCase):
    """An agent attach that did not answer is no proof its run ended."""

    def unanswered(self):
        return mock.patch.object(
            HarnessSession, "attach", side_effect=Unanswered("docker ps: timed out")
        )

    def gave_up(self):
        """A tui run whose worker gave up without an outcome, its agent session left open."""
        view = self.make(spec=Spec("input", self.work, mode="tui", client="fake", sid=SID))
        gives_up()
        self.assertIsNone(view.run().outcome)
        return view

    def test_a_live_run_whose_agent_did_not_answer_is_unknown_not_gone(self):
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        record = self.make(spec=spec).detach()
        view = DrivenDelegate.attach(record)
        with self.unanswered():
            status = view.status()
            self.assertEqual((status.alive, status.state), (True, "unknown"))
            self.assertEqual(view.content(), status.to_line())
        self.stop(record)
        with self.unanswered():
            self.assertEqual((view.status().alive, view.status().state), (False, "unknown"))

    def test_an_agent_confirmed_gone_is_gone(self):
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        record = self.make(spec=spec).detach()
        view = DrivenDelegate.attach(record)
        HarnessSession.live[record.sessions[0]].stop()
        FakeControl(self.metadata()["drive"]).close()  # its drive ends, its worker gone
        self.assertEqual((view.status().alive, view.status().state), (False, "gone"))

    def test_a_start_waits_through_an_attach_that_did_not_answer(self):
        attach, calls = HarnessSession.attach, []

        def flaky(*args, **kw):
            calls.append(args)
            if len(calls) == 1:
                raise Unanswered("docker ps: timed out")
            return attach(*args, **kw)

        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        with mock.patch.object(HarnessSession, "attach", side_effect=flaky):
            record = self.make(spec=spec).detach()
        self.assertGreater(len(calls), 1)
        self.assertTrue(HarnessSession.live[record.sessions[0]].alive())

    def test_show_hide_and_attach_command_say_its_liveness_is_unknown(self):
        HarnessSession.reset(showable=True)
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        self.make(spec=spec).detach()
        view = DrivenDelegate.attach(self.work, session_cls=HarnessSession, viewer=FakeViewer())
        unknown = "^delegate agent session liveness unknown: docker ps: timed out$"
        with self.unanswered():
            for call in (view.show, view.hide, view.attach_command):
                with self.subTest(call=call.__name__), self.assertRaisesRegex(Unanswered, unknown):
                    call()
            self.assertEqual((view.status().state, view.status().attach), ("unknown", None))
        self.assertEqual((FakeViewer.shown, FakeViewer.hidden), ([], []))

    def test_close_stops_the_drive_then_says_the_agent_was_not_checked(self):
        HarnessSession.reset(showable=True)
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        record = self.make(spec=spec).detach()
        view = DrivenDelegate.attach(self.work, session_cls=HarnessSession, viewer=FakeViewer())
        (agent,) = record.sessions
        with self.unanswered(), self.assertRaises(Unanswered) as caught:
            view.close()
        self.assertEqual(
            str(caught.exception),
            f"close {agent}: agent session not checked: docker ps: timed out",
        )
        self.assertFalse(FakeControl(self.metadata()["drive"]).running())
        self.assertEqual(FakeViewer.closed, [])
        self.assertTrue(HarnessSession.live[agent].alive())

    def test_a_failing_drive_stop_still_names_the_unchecked_agent(self):
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        record = self.make(spec=spec).detach()
        view = DrivenDelegate.attach(self.work, session_cls=HarnessSession, viewer=FakeViewer())
        (agent,) = record.sessions
        harness = self.metadata()["harness_name"]
        unchecked = "docker ps: timed out"
        refused = PermissionError(errno.EPERM, "close refused")
        with (
            self.unanswered(),
            mock.patch.object(FakeControl, "terminate", side_effect=refused),
            self.assertRaises(SessionError) as caught,
        ):
            view.close()
        self.assertEqual(
            str(caught.exception),
            f"close {harness}: close refused; close {agent}: agent session not checked: {unchecked}",
        )

    def test_replace_resume_and_clean_refuse_while_the_agent_does_not_answer(self):
        view = self.gave_up()
        before = self.metadata()
        agent = HarnessSession.live[before["agent_name"]]
        refusal = f"^{self.work}: agent session liveness unknown: docker ps: timed out$"
        with self.unanswered():
            with self.assertRaisesRegex(ConfigError, refusal):
                self.make()
            with self.assertRaisesRegex(ConfigError, refusal):
                view.resume()
            with self.assertRaisesRegex(ConfigError, refusal):
                DrivenDelegate.clean(self.work, scratch=True)
        self.assertEqual(self.metadata(), before)
        self.assertTrue(agent.alive())


class ViewTest(HarnessCase):
    def setUp(self):
        super().setUp()
        HarnessSession.reset(showable=True)

    def test_show_and_hide_touch_only_the_agent(self):
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        self.make(spec=spec).detach()
        view = DrivenDelegate.attach(self.work, session_cls=HarnessSession, viewer=FakeViewer())
        agent = self.metadata()["agent_name"]
        view.show()
        self.assertEqual([name for name, _, _ in FakeViewer.shown], [agent])
        self.assertFalse(view.status().hidden)
        self.assertIsNone(view.hide())
        self.assertEqual(FakeViewer.hidden, [agent])
        self.assertTrue(view.status().hidden)
        view.show()
        self.assertEqual([name for name, _, _ in FakeViewer.shown], [agent] * 2)

    def test_show_without_a_live_agent_is_not_showable_and_opens_no_harness(self):
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        self.make(spec=spec).detach()
        view = DrivenDelegate.attach(self.work, session_cls=HarnessSession, viewer=FakeViewer())
        HarnessSession.live[self.metadata()["agent_name"]].stop()
        with self.assertRaisesRegex(NotShowable, "delegate has no live showable agent session"):
            view.show()
        self.assertEqual(FakeViewer.shown, [])

    def test_close_closes_no_viewer_view_for_the_drive(self):
        for showable in (True, False):
            with self.subTest(showable=showable):
                HarnessSession.reset(showable=showable)
                spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
                self.make(spec=spec).detach()
                DrivenDelegate.attach(
                    self.work, session_cls=HarnessSession, viewer=FakeViewer()
                ).close()
                self.assertEqual(FakeViewer.closed, [])
                self.assertFalse(FakeControl(self.metadata()["drive"]).running())

    def test_hide_after_close_is_not_showable(self):
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        self.make(spec=spec).detach()
        view = DrivenDelegate.attach(self.work, session_cls=HarnessSession, viewer=FakeViewer())
        view.close()
        with self.assertRaisesRegex(NotShowable, "delegate has no live showable session"):
            view.hide()
        self.assertEqual(FakeViewer.hidden, [])


class CrashTest(HarnessCase):
    def crashed(self, cwd=None):
        """A tui run whose process died mid-run: no outcome stored, no run lock held, its agent
        session left live as the run started it.
        """
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID, cwd=cwd)
        self.make(spec=spec)
        state = self.metadata()
        agent = SessionSpec(
            workdir=self.work,
            access=Access(cwd=self.work),
            client="fake",
            mode=Mode.TUI,
            sid=SID,
            name=state["agent_name"],
        )
        HarnessSession.start(agent, host=state["host"])
        return DrivenDelegate.attach(self.work, session_cls=HarnessSession), state["agent_name"]

    def test_a_tui_run_whose_process_died_is_not_alive(self):
        view, name = self.crashed()
        self.assertEqual(HarnessSession.live[name].alive(), True)
        self.assertFalse(view.alive())
        self.assertFalse(view.status().alive)

    def test_its_status_is_unknown_while_its_agent_sessions_host_cannot_tell(self):
        view, name = self.crashed()
        agent = HarnessSession.live[name]
        self.assertEqual((view.status().state, view.status().hidden), ("working", False))
        agent.liveness = Liveness.UNKNOWN
        self.assertEqual(view.status().state, "unknown")

    def test_resume_closes_the_left_agent_session_and_launches_the_next_attempt(self):
        view, name = self.crashed()
        left = HarnessSession.live[name]
        launches = len(harness_launches())
        record = view.resume()
        self.assertEqual(len(harness_launches()), launches + 1)
        self.assertEqual(left.alive(), False)
        self.assertEqual(record.sid, SID)
        self.assertEqual(self.metadata()["attempt"], 2)

    def test_a_resume_after_its_agent_dir_was_pruned_makes_it_again(self):
        view, _name = self.crashed()
        shutil.rmtree(Layout(self.work).agent)
        view.resume()
        self.assertTrue(os.path.isdir(Layout(self.work).agent))
        self.assertEqual(self.metadata()["attempt"], 2)

    def test_a_resume_refused_for_an_untrusted_cwd_leaves_the_agent_session_live(self):
        project = os.path.join(self.home, "project")
        os.mkdir(project)
        self.settings(trusted_dirs=(project,))
        _view, name = self.crashed(cwd=project)
        self.settings()
        left, launches = HarnessSession.live[name], len(harness_launches())
        view = DrivenDelegate.attach(self.work, session_cls=HarnessSession)
        with self.assertRaisesRegex(ConfigError, "no longer in trusted_dirs"):
            view.resume()
        self.assertEqual(left.alive(), True)
        self.assertEqual(len(harness_launches()), launches)

    def test_a_resume_its_preflight_refuses_leaves_the_agent_session_live(self):
        _view, name = self.crashed()
        left, launches = HarnessSession.live[name], len(harness_launches())
        HarnessSession.preflight_error = SessionConfigError("fake: no client binary")
        view = DrivenDelegate.attach(self.work, session_cls=HarnessSession)
        with self.assertRaisesRegex(ConfigError, "no client binary"):
            view.resume()
        self.assertEqual((left.alive(), len(harness_launches())), (True, launches))

    def test_resume_preflights_the_live_agents_name_under_a_probe_name(self):
        view, name = self.crashed()
        view.resume()
        self.assertNotEqual(HarnessSession.preflights[-1]["spec"].name, name)
        self.assertEqual(self.metadata()["plan"]["session"]["name"], name)

    def test_resume_refuses_a_run_replaced_since_its_attach(self):
        view, _name = self.crashed()
        other = "0b6f2c1e-6d0a-4c1b-9a51-3f1f6b0e2a7d"
        self.make(spec=Spec("input", self.work, task="echo", sid=other))
        launches = len(harness_launches())
        with self.assertRaisesRegex(ConfigError, f"run {SID} was replaced by {other}"):
            view.resume()
        self.assertEqual(
            (len(harness_launches()), self.metadata()["spec"]["sid"]), (launches, other)
        )

    def test_the_status_of_an_agent_session_another_process_started_comes_from_its_plan(self):
        view, _name = self.crashed()
        self.assertEqual(view.session_status(), HarnessSession.history)
        (inspected,) = HarnessSession.inspected
        self.assertEqual((inspected.sid, inspected.mode, inspected.client), (SID, "tui", "fake"))


class SweepTest(HarnessCase):
    """A callback group left by a dead harness (orphans.Orphan) is killed before anything starts."""

    def setUp(self):
        super().setUp()
        Clock(self)
        self.killpg = self.enterContext(mock.patch("os.killpg"))
        self.seen = []

    def orphan(self) -> Orphan:
        orphan = Orphan(self, RunState(Layout(self.work)).callback_lock, f"{ORPHAN} 5151\n")

        def killpg(pid, signum):
            if signum == signal.SIGKILL:
                self.seen.append((self.metadata()["attempt"], len(harness_launches())))
            orphan.killpg(pid, signum)

        self.killpg.side_effect = killpg
        return orphan

    def record(self) -> bytes:
        with open(self.state_record(), "rb") as file:
            return file.read()

    def kills(self, orphan) -> list:
        return [call for call in orphan.calls if call[1] == signal.SIGKILL]

    def test_a_new_run_kills_the_left_group_before_its_record_replaces_the_old(self):
        self.make()
        orphan = self.orphan()
        self.make()
        self.assertEqual(self.kills(orphan), [(ORPHAN, signal.SIGKILL)])
        self.assertEqual(self.seen, [(1, 0)])
        self.assertEqual(self.metadata()["attempt"], 2)

    def test_a_busy_preflight_over_an_earlier_run_leaves_its_record(self):
        self.make(spec=Spec("input", self.work, mode="tui", client="fake", sid=SID)).run()
        left = HarnessSession.live[self.metadata()["agent_name"]]
        orphan, before = self.orphan(), self.record()
        HarnessSession.preflight_error = SessionBusy("dummy-lock", "ensemble-x-1 (owner x)")
        with self.assertRaises(SessionError) as caught:
            self.make()
        self.assertIs(type(caught.exception), SessionError)
        self.assertEqual(self.record(), before)
        self.assertFalse(left.alive())
        self.assertEqual(self.kills(orphan), [(ORPHAN, signal.SIGKILL)])

    def test_resume_kills_the_left_group_before_it_launches_a_harness(self):
        view = self.crashed()
        orphan = self.orphan()
        launches = len(harness_launches())
        view.resume()
        self.assertEqual(self.seen, [(1, launches)])
        self.assertEqual(len(harness_launches()), launches + 1)
        self.assertEqual(self.kills(orphan), [(ORPHAN, signal.SIGKILL)])

    def test_detach_kills_the_left_group_before_it_launches_a_harness(self):
        delegate = self.make()
        orphan = self.orphan()
        delegate.detach()
        self.assertEqual(self.seen, [(1, 0)])
        self.assertEqual(len(harness_launches()), 1)
        self.assertEqual(self.kills(orphan), [(ORPHAN, signal.SIGKILL)])

    def test_a_group_that_will_not_end_refuses_each_start_changing_nothing(self):
        view = self.crashed()
        orphan = self.orphan()
        orphan.dies = False
        before, launches = self.record(), len(harness_launches())
        left = HarnessSession.live[self.metadata()["agent_name"]]
        for start in (view.resume, DrivenDelegate.attach(self.work).detach, self.make):
            with self.subTest(start=start), self.assertRaisesRegex(ConfigError, "still exists"):
                start()
            self.assertEqual((self.record(), len(harness_launches())), (before, launches))
            if start == view.resume:
                self.assertEqual(left.alive(), True)

    def test_a_held_run_lock_refuses_a_new_run_though_its_record_says_completed(self):
        self.make().run()
        self.assertTrue(self.metadata()["completed"])
        self.orphan()
        before = self.record()
        with (
            RunState(Layout(self.work)).run_lock(),
            self.assertRaisesRegex(ConfigError, "delegate is still alive"),
        ):
            self.make()
        self.killpg.assert_not_called()
        self.assertEqual(self.record(), before)

    def crashed(self) -> HarnessDelegate:
        """A tui run whose harness died, its agent session left live."""
        spec = Spec("input", self.work, mode="tui", client="fake", sid=SID)
        self.make(spec=spec)
        state = self.metadata()
        agent = SessionSpec(
            workdir=self.work,
            access=Access(cwd=self.work),
            client="fake",
            mode=Mode.TUI,
            sid=SID,
            name=state["agent_name"],
        )
        HarnessSession.start(agent, host=state["host"])
        return DrivenDelegate.attach(self.work, session_cls=HarnessSession)


AGENT = f"dummy-tester-{SID[:8]}"
HARNESS = f"{AGENT}-drive"
SCREEN = "OpenAI Codex\n\n  Booting\n\naccount/read failed during TUI bootstrap\n\n"
TAIL = "OpenAI Codex | Booting | account/read failed during TUI bootstrap"


class StartedTest(HarnessCase):
    """detach's wait for a tui run's agent session: the start limit bounds it, its SessionError
    names why, and a start it gives up on while the drive may live is saved before its stop.
    """

    def setUp(self):
        super().setUp()
        FakeControl.harness_starts_agent = False
        self.now, self.host = 0.0, "agent"

    def tui(self, *, docker=False):
        """A tui run whose agent is on `agent`, else (docker) on the Docker Host `box`."""
        self.host = "agent"
        if docker:
            self.host = "box"
            HarnessSession.hosts["box"] = RecordingHost("box")
            with open(self.local, "a", encoding="utf-8") as file:
                file.write(
                    "[session.hosts.box]\n"
                    'implementation = "ensemble.session.hosts.docker.host:DockerHost"\n'
                    'docker = "/usr/local/bin/docker"\n'
                )
        text = "Reference: T\nRepo: acme/ensemble\n\nHi." if docker else "input"
        spec = Spec(text, self.work, mode="tui", client="fake", sid=SID)
        return self.make(spec=spec, host=self.host)

    def ends(self, ending=None):
        """The drive has ended by the time its spawn returns, after `ending` ran."""

        FakeControl.ends_at_start, FakeControl.ending = True, ending

    def start_agent(self):
        HarnessSession.start(
            SessionSpec(
                workdir=self.home,
                access=Access(cwd=self.home),
                client="fake",
                mode=Mode.TUI,
                sid=SID,
                name=AGENT,
            ),
            host=self.host,
        )

    def detach(self, delegate, *, live_at=None):
        """detach on a fake clock 5 s on per poll; the agent session goes live at `live_at` s."""

        def sleep(_seconds):
            self.now += 5
            if live_at is not None and self.now >= live_at and AGENT not in HarnessSession.live:
                self.start_agent()

        with (
            mock.patch("time.monotonic", side_effect=lambda: self.now),
            mock.patch("time.sleep", side_effect=sleep),
        ):
            return delegate.detach()

    def failed(self, delegate, **kw) -> str:
        with self.assertRaises(SessionError) as caught:
            self.detach(delegate, **kw)
        return str(caught.exception)

    def drive(self):
        return FakeControl.named(HARNESS)

    def end_with(self, result):
        """The stored run ended with `result`, as its harness records it."""
        data = self.metadata()
        data.update(completed=True, result=asdict(result))
        with open(self.state_record(), "w", encoding="utf-8") as file:
            json.dump(data, file)

    def published(self):
        return HarnessSession.host(self.host).process(AGENT).record()

    def test_a_started_agent_session_carries_the_runs_record(self):
        record = self.detach(self.tui(), live_at=5)
        self.assertEqual(self.published(), record)

    def test_a_started_docker_agent_session_carries_the_runs_record(self):
        record = self.detach(self.tui(docker=True), live_at=5)
        self.assertEqual(self.published(), record)

    def test_a_record_its_host_refuses_is_said_and_the_start_stands(self):
        refused = SessionError(f"session {AGENT}: its host cannot store a Record after launch")
        with (
            mock.patch.object(RecordingProcess, "publish", side_effect=refused),
            contextlib.redirect_stderr(io.StringIO()) as err,
        ):
            record = self.detach(self.tui(), live_at=5)
        self.assertEqual(record.sessions, (AGENT,))
        self.assertTrue(self.drive().running())
        self.assertIsNone(self.published())
        self.assertEqual(err.getvalue(), f"delegate: Record not published: {refused}\n")

    def test_a_docker_start_live_at_35_s_is_not_failed(self):
        record = self.detach(self.tui(docker=True), live_at=35)
        self.assertEqual(record.sessions, (AGENT,))
        self.assertTrue(self.drive().running())
        self.assertIsNone(self.metadata()["start_failure"])

    def test_a_docker_start_not_live_by_120_s_fails_and_stops_the_drive(self):
        message = self.failed(self.tui(docker=True))
        self.assertEqual(message, f"tui session {AGENT} did not start in 120 s")
        self.assertEqual(self.now, 120)
        self.assertFalse(self.drive().running())

    def test_a_tmux_start_not_live_by_30_s_fails(self):
        message = self.failed(self.tui())
        self.assertEqual(message, f"tui session {AGENT} did not start in 30 s")
        self.assertEqual(self.now, 30)
        self.assertFalse(self.drive().running())

    def test_a_harness_ended_first_fails_at_once_naming_its_recorded_end(self):
        self.ends(lambda: self.end_with(Result(1, None, "stopped: SessionError: no relay")))
        message = self.failed(self.tui())
        self.assertEqual(
            message,
            f"tui session {AGENT} did not start: its harness ended: exit 1: "
            "stopped: SessionError: no relay",
        )
        self.assertEqual(self.now, 0)
        self.assertIsNone(self.metadata()["start_failure"])
        self.assertEqual(self.metadata()["result"]["returncode"], 1)

    def test_a_harness_ended_without_a_result_says_only_that_it_ended(self):
        self.ends()
        message = self.failed(self.tui())
        self.assertEqual(message, f"tui session {AGENT} did not start: its harness ended")
        self.assertIsNone(self.metadata()["start_failure"])
        self.assertNotIn("signals", self.drive().data())

    def test_a_timeout_saves_its_cause_before_closing(self):
        seen = []
        terminate = FakeControl.terminate

        def closing(control):
            seen.append(self.metadata()["start_failure"])
            terminate(control)

        with mock.patch.object(FakeControl, "terminate", closing):
            message = self.failed(self.tui())
        self.assertEqual(seen, [message])
        self.assertEqual(self.metadata()["start_failure"], message)

    def test_a_host_error_is_the_cause(self):
        self.start_agent()

        def failing(_session):
            if self.now >= 10:
                raise SessionError("tmux:\n no server")
            return False

        with mock.patch.object(HarnessSession, "alive", failing):
            message = self.failed(self.tui())
        self.assertEqual(message, f"tui session {AGENT} did not start: tmux: no server")
        self.assertEqual(self.metadata()["start_failure"], message)
        self.assertEqual(self.drive().data()["signals"], ["terminate"])

    def test_an_unsaved_start_failure_still_names_the_cause(self):
        delegate = self.tui()
        write = delegate.store.write

        def refusing(payload):
            if '"start_failure": "tui' in payload:
                raise OSError("disk full")
            write(payload)

        with mock.patch.object(delegate.store, "write", refusing):
            message = self.failed(delegate)
        self.assertEqual(message, f"tui session {AGENT} did not start in 30 s")
        self.assertIsNone(self.metadata()["start_failure"])
        self.assertFalse(self.drive().running())

    def test_a_new_launch_clears_the_start_failure(self):
        self.failed(self.tui())
        self.assertIsNotNone(self.metadata()["start_failure"])
        FakeControl.harness_starts_agent = True
        DrivenDelegate.attach(self.work).resume()
        self.assertIsNone(self.metadata()["start_failure"])

    def test_an_unanswered_agent_is_reprobed_and_closed(self):
        self.start_agent()
        agent = HarnessSession.live[AGENT]
        attach = HarnessSession.attach

        late = []

        def probing(name, **kw):
            late.extend([self.now] if self.now >= 30 else [])
            if len(late) < 2:
                raise Unanswered("docker ps: timed out")
            return attach(name, **kw)

        with (
            mock.patch.object(HarnessSession, "attach", side_effect=probing),
            mock.patch.object(HarnessSession, "screen", return_value=SCREEN, create=True),
        ):
            message = self.failed(self.tui())
        self.assertEqual(message, f"tui session {AGENT} did not start in 30 s; last lines: {TAIL}")
        self.assertEqual(self.metadata()["start_failure"], message)
        self.assertEqual(len(late), 2)
        self.assertFalse(agent.alive())

    def test_a_still_unanswered_agent_is_named(self):
        with mock.patch.object(
            HarnessSession, "attach", side_effect=Unanswered("docker ps: timed out")
        ):
            message = self.failed(self.tui())
        self.assertEqual(
            message,
            f"tui session {AGENT} did not start in 30 s; "
            "agent session not checked: docker ps: timed out",
        )

    def test_an_agent_whose_client_exited_is_not_started(self):
        self.start_agent()
        status = HarnessSession.status

        def dead(session):
            return replace(status(session), alive=False, state=StatusState.DEAD)

        with mock.patch.object(HarnessSession, "status", dead):
            message = self.failed(self.tui())
        self.assertEqual(message, f"tui session {AGENT} did not start in 30 s")
        self.assertFalse(self.drive().running())

    def exited(self):
        """The agent session left by a client that exited: its pane kept, its status dead, its
        screen refused, its Host row holding SCREEN.
        """
        self.start_agent()
        status = HarnessSession.status
        row = SessionRow(AGENT, SID, "fake", StatusState.DEAD, None, None, SCREEN, own=False)
        return (
            mock.patch.object(
                HarnessSession,
                "status",
                lambda session: replace(status(session), alive=False, state=StatusState.DEAD),
            ),
            mock.patch.object(
                HarnessSession, "screen", side_effect=SessionError("not running"), create=True
            ),
            mock.patch.object(RecordingHost, "rows", return_value=[row], create=True),
        )

    def test_an_exited_clients_last_lines_join_the_error(self):
        self.ends(lambda: self.end_with(Result(3, None, "the client exited 3")))
        dead, refused, rows = self.exited()
        agent = HarnessSession.live[AGENT]
        with dead, refused, rows:
            message = self.failed(self.tui())
        self.assertEqual(
            message,
            f"tui session {AGENT} did not start: its harness ended: exit 3: the client exited 3"
            f"; last lines: {TAIL}",
        )
        self.assertIsNone(self.metadata()["start_failure"])
        self.assertFalse(agent.alive())

    def test_an_unanswered_liveness_probe_keeps_waiting(self):
        self.start_agent()
        alive = HarnessSession.alive

        def probing(session):
            if self.now < 10:
                raise Unanswered("docker ps: timed out")
            return alive(session)

        with mock.patch.object(HarnessSession, "alive", probing):
            record = self.detach(self.tui())
        self.assertEqual(record.sessions, (AGENT,))
        self.assertEqual(self.now, 10)
        self.assertIsNone(self.metadata()["start_failure"])

    def test_an_unreadable_tail_is_left_out(self):
        self.ends()
        dead, refused, _rows = self.exited()
        with (
            dead,
            refused,
            mock.patch.object(
                RecordingHost, "rows", side_effect=SessionError("docker ps: timed out"), create=True
            ),
        ):
            message = self.failed(self.tui())
        self.assertEqual(message, f"tui session {AGENT} did not start: its harness ended")

    def test_a_failed_drive_stop_clears_the_start_failure(self):
        FakeControl.stubborn = "kill"
        message = self.failed(self.tui())
        self.assertEqual(message, f"tui session {AGENT} did not start in 30 s")
        self.assertIsNone(self.metadata()["start_failure"])


class StartFailureEndTest(HarnessCase):
    """A run stopped by a signal ends with the start failure detach saved for its attempt, keeping
    its exit code; without one it stays a plain stop.
    """

    FAILURE = f"tui session {AGENT} did not start in 120 s"

    def stopped(self, start_failure=None, *, attempt=1):
        """The stored Result and the callback's last line after a SIGHUP at the run's Begun,
        detach having saved this state meanwhile.
        """
        view = self.make(callback=("callback",))
        lines = []

        def hung_up(event):
            if isinstance(event, Begun):
                data = self.metadata()
                data.update(start_failure=start_failure, attempt=attempt)
                with open(self.state_record(), "w", encoding="utf-8") as file:
                    json.dump(data, file)
                raise Stopped(129)

        with (
            mock.patch("subprocess.Popen", side_effect=lambda _argv, **_kw: Child(lines)),
            mock.patch("sys.stderr", new_callable=io.StringIO),
            self.assertRaises(Stopped),
        ):
            view.run(hung_up)
        return self.metadata()["result"], json.loads(lines[-1])

    def test_a_stop_after_a_start_failure_records_the_cause_and_no_129(self):
        result, sent = self.stopped(self.FAILURE)
        self.assertEqual(result, asdict(Result(1, None, f"stopped: {self.FAILURE}")))
        self.assertEqual(sent["returncode"], 1)
        with open(os.path.join(Layout(self.work).run, "run.jsonl"), encoding="utf-8") as file:
            rows = {row["kind"]: row for row in map(json.loads, file)}
        self.assertEqual(
            (rows["result"]["error"], rows["end"]["rc"]), (f"stopped: {self.FAILURE}", 1)
        )
        self.assertEqual(sent["error"], f"stopped: {self.FAILURE}")

    def test_an_outside_stop_stays_an_interruption(self):
        result, sent = self.stopped()
        self.assertEqual(result, asdict(Result(129, None, "stopped: Stopped: 129")))
        self.assertEqual(sent["error"], "stopped: Stopped: 129")

    def test_another_attempts_start_failure_is_ignored(self):
        _result, sent = self.stopped(self.FAILURE, attempt=2)
        self.assertEqual(sent["error"], "stopped: Stopped: 129")


class Child:
    """subprocess.Popen for the run's callback: takes its event line, exits 0."""

    def __init__(self, lines):
        self.lines, self.pid, self.returncode = lines, 4321, 0

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def communicate(self, line, timeout=None):
        self.lines.append(line)
        return "", ""


ACCEPTANCE = """
[delegate.environments.dummy]
image = "dummy-env:test"
plugins = ["dummy-plugin"]
lock = "dummy-lock"

[[delegate.environments.dummy.secrets]]
item = "ensemble-broker-env/dummy"
file = "dummy.txt"

[delegate.roles.dummy-tester.environment]
name = "dummy"
prefetch = true
"""
TARGETED = "Reference: T-1\nRepo: acme/app\nRef: release/1\n\nHello."


class EnvironmentTest(HarnessCase):
    """The delegate config's environments resolved at construction (WDRC-109 FR-57, FR-59, FR-60,
    FR-62, NFR-2, NFR-4).
    """

    def setUp(self):
        super().setUp()
        for name in ("docker", "tmux"):
            HarnessSession.hosts[name] = RecordingHost(name)

    def append(self, text):
        with open(self.local, "a", encoding="utf-8") as file:
            file.write(text)

    def spec(self, text=TARGETED, **kw):
        return Spec(text, self.work, client="fake", sid=SID, **{"task": "echo", **kw})

    def preview(self, role="dummy-tester", host="docker", text=TARGETED, **kw):
        spec = self.spec(text, task="build" if role == "engineer" else "echo")
        placement = {"session_cls": HarnessSession, "host": host}
        return DrivenDelegate.preview(role, spec, **placement | kw)

    def test_tmux_host_without_default_refuses_at_construction(self):
        self.append(ACCEPTANCE)
        for host in ("tmux", "agent"):
            with self.subTest(host=host), self.assertRaises(ConfigError) as caught:
                self.make(spec=self.spec(), host=host)
            self.assertIn(f"[environment.{host}]", str(caught.exception))
        self.assertFalse(os.path.exists(self.work))
        self.assertEqual((HarnessSession.preflights, HarnessSession.started), ([], []))
        self.assertEqual(RecordingHost.launches, [])

    def test_docker_kind_d2_without_default_names_it(self):
        HarnessSession.hosts["d2"] = RecordingHost("d2")
        self.append(
            ACCEPTANCE
            + '[session.hosts.d2]\nimplementation = "ensemble.session.hosts.docker.host:DockerHost"\n'
        )
        with self.assertRaises(ConfigError) as caught:
            self.make(spec=self.spec(), host="d2")
        self.assertIn("[environment.d2]", str(caught.exception))
        self.assertFalse(os.path.exists(self.work))

    def test_engineer_passes_no_environment_on_docker_and_tmux(self):
        self.assertIsNone(self.preview("engineer").plan.session.environment)
        self.append('[delegate.environment.docker]\nplugins = ["p"]\n')
        self.assertIsNone(self.preview("engineer", "tmux").plan.session.environment)
        made = self.make(role="engineer", spec=self.spec(task="build"), host="tmux")
        made.close()
        self.assertIsNone(self.metadata()["plan"]["session"]["environment"])

    def test_local_default_gateway_reaches_engineer(self):
        self.append('[delegate.environment.docker]\ngateway = "x"\n')
        self.assertEqual(
            self.preview("engineer").plan.session.environment,
            EnvironmentSpec(image="@worker", gateway="x"),
        )

    def test_orchestrator_overlay_with_role_environment_is_refused(self):
        overlay = {"roles": {"dummy-tester": {"environment": {"name": "dummy"}}}}
        self.append(ACCEPTANCE)
        with self.assertRaises(ConfigError) as caught:
            self.make(spec=self.spec(), host="docker", overlays=(overlay,))
        self.assertIn("[overlay.roles.dummy-tester.environment]", str(caught.exception))
        self.assertFalse(os.path.exists(self.work))

    def test_issue_text_names_change_nothing(self):
        named = (
            "Reference: T-1\nRepo: acme/app\nRef: release/1\nEnvironment: evil\nImage: evil:1\n"
            "Gateway: evil\nLock: evil\n\nRun in environment evil on image evil:1, network evil."
        )
        self.assertIsNone(self.preview("engineer", text=named).plan.session.environment)
        self.append(ACCEPTANCE)
        self.assertEqual(
            self.preview(text=named).plan.session.environment,
            self.preview().plan.session.environment,
        )

    def test_delegate_json_round_trips_environment(self):
        self.append(ACCEPTANCE)
        self.make(spec=self.spec(), host="docker").close()
        found = self.metadata()["plan"]["session"]["environment"]
        want = EnvironmentSpec(
            image="dummy-env:test",
            gateway="workers",
            plugins=("dummy-plugin",),
            secrets=({"item": "ensemble-broker-env/dummy", "file": "dummy.txt"},),
            lock="dummy-lock",
            prefetch={"repo": "acme/app", "ref": "release/1"},
        )
        self.assertEqual(found, want.persisted())
        view = DrivenDelegate.attach(self.work, session_cls=HarnessSession)
        self.assertEqual(view._state.plan.session.environment, want)

    def test_pre_upgrade_state_parses_without_environment(self):
        self.make(role="engineer", spec=self.spec(task="build"), host="docker").close()
        data = self.metadata()
        del data["plan"]["session"]["environment"]
        with open(self.state_record(), "w", encoding="utf-8") as file:
            json.dump(data, file)
        view = DrivenDelegate.attach(self.work, session_cls=HarnessSession)
        self.assertIsNone(view._state.plan.session.environment)

    def test_delegate_json_round_trips_handlers_and_reads_none_before_them(self):
        self.make(role="engineer", spec=self.spec(task="build"), host="docker").close()
        data = self.metadata()
        self.assertEqual(data["plan"]["session"]["handlers"], [])
        handler = {"module": "pkg.handler", "state": {"k": 1}, "roots": ["/r"]}
        for stored, want in (
            ([handler], (HookCallback("pkg.handler", {"k": 1}, ("/r",)),)),
            (None, ()),
        ):
            with self.subTest(stored=stored):
                if stored is None:
                    del data["plan"]["session"]["handlers"]
                else:
                    data["plan"]["session"]["handlers"] = stored
                with open(self.state_record(), "w", encoding="utf-8") as file:
                    json.dump(data, file)
                view = DrivenDelegate.attach(self.work, session_cls=HarnessSession)
                self.assertEqual(view._state.plan.session.handlers, want)

    def answered(self, view, text="The answer."):
        view._complete(Result(0, Outcome("needs_input", "Q", "A", questions=["Which?"])))
        return view.resume(input=text)

    def test_resume_keeps_stored_environment_after_config_change(self):
        self.append(ACCEPTANCE)
        view = self.make(spec=self.spec(), host="docker")
        stored = self.metadata()["plan"]["session"]["environment"]
        self.append('[delegate.environment.docker]\ngateway = "x"\n')
        with open(self.local, encoding="utf-8") as file:
            changed = file.read().replace("dummy-env:test", "other-env:test")
        with open(self.local, "w", encoding="utf-8") as file:
            file.write(changed)
        self.stop(self.answered(view))
        self.assertEqual(self.metadata()["plan"]["session"]["environment"], stored)
        for mode in ("headless", "tui"):
            with self.subTest(mode=mode):
                view = DrivenDelegate.attach(self.work, session_cls=HarnessSession)
                self.stop(view.resume(mode=mode))
                self.assertEqual(self.metadata()["plan"]["session"]["environment"], stored)

    def test_pre_upgrade_state_resumes_without_environment(self):
        view = self.make(role="engineer", spec=self.spec(task="build"), host="docker")
        data = self.metadata()
        del data["plan"]["session"]["environment"]
        with open(self.state_record(), "w", encoding="utf-8") as file:
            json.dump(data, file)
        self.append('[delegate.environment.docker]\ngateway = "x"\n')
        view = DrivenDelegate.attach(self.work, session_cls=HarnessSession)
        self.stop(self.answered(view))
        self.assertIsNone(self.metadata()["plan"]["session"]["environment"])

    def test_dummy_tester_acceptance_config_end_to_end(self):
        self.append(ACCEPTANCE)
        echoed = {"status": "done", "title": "Echo", "summary": "echoed", "deliverable": TARGETED}
        HarnessSession.script = (Reply(block(echoed)), Exited(0))
        view = self.make(spec=self.spec(mode="headless"), host="docker")
        self.assertEqual(
            HarnessSession.preflights[-1]["spec"].environment,
            EnvironmentSpec(
                image="dummy-env:test",
                gateway="workers",
                plugins=("dummy-plugin",),
                secrets=({"item": "ensemble-broker-env/dummy", "file": "dummy.txt"},),
                lock="dummy-lock",
                prefetch={"repo": "acme/app", "ref": "release/1"},
            ),
        )
        order = []
        with mock.patch.object(
            HarnessSession, "close", autospec=True, side_effect=lambda _s: order.append("close")
        ):
            result = view.run(lambda e: isinstance(e, Finished) and order.append("finished"))
        assert result.outcome is not None
        self.assertEqual(result.outcome.deliverable.strip(), TARGETED)
        self.assertEqual(order, ["close", "finished"])


class OwnedRun(HarnessCase):
    """An owned tui run (its preflight takes the handler): run() starts the worker, then waits for
    the attempt's Result its handler stores, ending the attempt itself only while opening past the
    idle timeout or once its worker is gone. Its worker sends no event: a test stores what its
    handler would.
    """

    NEEDS = Result(0, Outcome(OutcomeStatus.NEEDS_INPUT, "T", "S", questions=["Q"]))

    def setUp(self):
        super().setUp()
        HarnessSession.script, HarnessSession.lingers = (), True
        LockingSession.reset()
        LockingSession.lingers = True
        self.lines: list[str] = []
        self.enterContext(
            mock.patch("subprocess.Popen", side_effect=lambda _argv, **_kw: Child(self.lines))
        )
        self.err = self.enterContext(mock.patch("sys.stderr", new_callable=io.StringIO))
        self.events_file = os.path.join(self.home, "events.log")
        self.record = RunRecord(Layout(self.work))

    def owned(self, *, idle=600.0, lock=None, work=None, sid=SID, session_cls=HarnessSession):
        spec = Spec(
            "input",
            work or self.work,
            task="echo",
            sid=sid,
            client="fake",
            mode="tui",
            events=self.events_file,
        )
        view = self.make(spec=spec, callback=("callback",), session_cls=session_cls)
        p = view._state.plan
        session = p.session
        if lock is not None:
            session = replace(
                session, environment=EnvironmentSpec(image="img", gateway="gw", lock=lock)
            )
        view._state = replace(
            view._state,
            plan=replace(p, session=session, run=replace(p.run, idle_timeout=idle)),
        )
        view._save(view._state)
        return view

    def kinds(self) -> list[str]:
        with open(os.path.join(Layout(self.work).run, "run.jsonl"), encoding="utf-8") as f:
            return [json.loads(line)["kind"] for line in f]

    def sent(self) -> list[str]:
        return [json.loads(line)["kind"] for line in self.lines]

    def at_begun(self, act):
        seen = []

        def on(event):
            seen.append(type(event).__name__)
            if isinstance(event, Begun):
                act()

        return seen, on

    def test_a_tui_run_a_harness_owns_is_planned_with_the_handler(self):
        view = self.owned()
        (handler,) = view._state.plan.session.handlers
        self.assertEqual(
            (handler.module, dict(handler.state), handler.roots),
            (HANDLER, {"workdir": self.work, "worker": 1}, module_roots()),
        )
        self.assertEqual(self.metadata()["plan"]["session"]["handlers"][0]["state"]["worker"], 1)
        self.work = os.path.join(self.home, "headless")
        spec = Spec("input", self.work, task="echo", sid=SID, client="fake", mode="headless")
        self.assertEqual(self.make(spec=spec)._state.plan.session.handlers, ())
        HarnessSession.owns = False
        self.work = os.path.join(self.home, "refused")
        spec = Spec("input", self.work, task="echo", sid=SID, client="fake", mode="tui")
        with self.assertRaisesRegex(ConfigError, "^handlers: fake tui has no session harness yet$"):
            self.make(spec=spec)
        self.assertFalse(os.path.exists(Layout(self.work).run))

    def test_run_returns_the_result_the_handler_stored_and_stops_nothing(self):
        view = self.owned()

        def handled():
            self.assertTrue(view.alive())
            self.assertTrue(self.record.bind(1, 1))
            self.assertTrue(self.record.complete(1, self.NEEDS, sid=SID))

        seen, on = self.at_begun(handled)
        self.assertEqual(view.run(on), self.NEEDS)
        self.assertEqual(seen, ["Begun", "Finished"])
        self.assertEqual(self.sent(), ["begun"])
        self.assertTrue(view.driver.session.alive())
        self.assertFalse(view.alive())
        self.assertFalse(view.status().alive)
        self.assertEqual(view.status().outcome, "needs_input")

    def test_run_returns_its_attempts_result_once_a_follow_up_opened(self):
        view = self.owned()

        def followed():
            self.record.bind(1, 1)
            self.record.complete(1, self.NEEDS, sid=SID)
            self.assertTrue(self.record.follow_up(1, 1))

        _seen, on = self.at_begun(followed)
        self.assertEqual(view.run(on), self.NEEDS)
        self.assertEqual(self.record.read().attempt, 2)

    def test_a_replaced_attempt_is_a_config_error(self):
        view = self.owned()
        _seen, on = self.at_begun(lambda: self.record.save(replace(self.record.read(), attempt=3)))
        with self.assertRaisesRegex(ConfigError, "delegate attempt was replaced"):
            view.run(on)

    def test_an_exception_after_the_start_leaves_the_attempt_and_worker_alone(self):
        view = self.owned()

        def interrupted():
            self.record.bind(1, 1)
            raise Stopped(129)

        _seen, on = self.at_begun(interrupted)
        with self.assertRaises(Stopped):
            view.run(on)
        state = self.record.read()
        self.assertEqual((state.completed, state.result, state.bound), (False, None, True))
        self.assertTrue(view.driver.session.alive())
        self.assertEqual(self.sent(), ["begun"])
        self.assertTrue(view.alive())

    def assert_stopped_start_left_nothing(self, view: HarnessDelegate) -> None:
        """Its stopped ending stored and sent once, its worker closed, a resume starts again."""
        state = self.record.read()
        stopped = Result(129, None, "stopped: Stopped: 129")
        self.assertEqual((state.completed, state.result), (True, stopped))
        self.assertEqual(self.sent(), ["finished"])
        agent = view.driver.session
        assert agent is not None
        self.assertFalse(agent.alive())
        self.assertFalse(view.alive())
        DrivenDelegate.attach(self.work, session_cls=HarnessSession).resume()
        self.assertEqual(self.metadata()["attempt"], 2)

    def test_a_stop_during_the_tui_start_stores_it_and_closes_the_worker(self):
        view = self.owned()
        with (
            mock.patch.object(
                HarnessSession, "showable", new_callable=mock.PropertyMock, side_effect=Stopped(129)
            ),
            self.assertRaises(Stopped),
        ):
            view.run()
        self.assert_stopped_start_left_nothing(view)

    def test_a_locked_stop_during_the_tui_start_claims_release_once_it_closed(self):
        view = self.owned(lock="dummy-lock")
        with (
            mock.patch.object(
                HarnessSession, "showable", new_callable=mock.PropertyMock, side_effect=Stopped(129)
            ),
            self.assertRaises(Stopped),
        ):
            view.run()
        self.assertFalse(view.driver.session.alive())
        self.assertEqual(self.kinds().count("release"), 1)
        self.assertEqual(self.err.getvalue().count("closed (lock dummy-lock)"), 1)

    def test_a_stop_before_the_start_returns_closes_the_drivers_session(self):
        view = self.owned()

        class Stopping(AgentDriver):
            def start(self, spec, **kw):
                super().start(spec, **kw)
                raise Stopped(129)

        view.driver = Stopping(HarnessSession, display=None)
        with self.assertRaises(Stopped):
            view.run()
        self.assert_stopped_start_left_nothing(view)

    def test_an_opening_attempt_past_its_idle_timeout_is_given_up(self):
        Clock(self)
        view = self.owned(idle=5)
        result = view.run()
        self.assertEqual(result, Result(0, None, "the agent run returned no outcome"))
        self.assertEqual(self.kinds()[-3:], ["give_up", "result", "end"])
        self.assertIn(
            f"no event for 5 s; session {view._state.agent_name} left open", self.err.getvalue()
        )
        self.assertEqual(self.sent(), ["begun", "finished"])
        with open(self.events_file, encoding="utf-8") as f:
            self.assertTrue(f.read().rstrip().endswith("outcome error"))
        self.assertTrue(view.driver.session.alive())

    def test_a_bound_attempt_is_never_given_up_by_its_drive(self):
        clock = Clock(self)
        view = self.owned(idle=5)
        advance = clock.advance

        def later(seconds):
            advance(seconds)
            if clock.now >= 20 and not self.record.read().completed:
                self.record.complete(1, self.NEEDS, sid=SID)

        self.enterContext(mock.patch("time.sleep", side_effect=later))
        _seen, on = self.at_begun(lambda: self.record.bind(1, 1))
        self.assertEqual(view.run(on), self.NEEDS)
        self.assertNotIn("give_up", self.kinds())

    def test_a_gone_worker_ends_the_attempt_after_the_drain_wait(self):
        clock = Clock(self)
        view = self.owned()

        def gone():
            self.record.bind(1, 1)
            view.driver.session.liveness = Liveness.ABSENT

        _seen, on = self.at_begun(gone)
        result = view.run(on)
        self.assertEqual(result, Result(1, None, "stopped: session harness ended without a Result"))
        self.assertGreaterEqual(clock.now, DRAIN_WAIT)
        self.assertEqual(self.record.read().result, result)
        self.assertEqual(self.sent(), ["begun", "finished"])

    def test_a_locked_give_up_leaves_the_session_open(self):
        Clock(self)
        view = self.owned(idle=5, lock="dummy-lock")
        view.run()
        self.assertTrue(view.driver.session.alive())
        self.assertIn(f"session {view._state.agent_name} left open", self.err.getvalue())
        self.assertNotIn("closed (lock", self.err.getvalue())
        self.assertNotIn("release", self.kinds())
        self.assertEqual(self.sent(), ["begun", "finished"])

    def test_a_locked_needs_input_is_waiting_and_attachable(self):
        """A crew entry is waiting on an outcome needs_input while not alive (crew._reconciled)."""
        HarnessSession.reset(showable=True)
        HarnessSession.lingers = True
        view = self.owned(lock="dummy-lock")
        _seen, on = self.at_begun(
            lambda: (self.record.bind(1, 1), self.record.complete(1, self.NEEDS, sid=SID))
        )
        self.assertEqual(view.run(on), self.NEEDS)
        self.assertTrue(view.driver.session.alive())
        again = DrivenDelegate.attach(self.work, session_cls=HarnessSession, viewer=FakeViewer())
        self.assertFalse(again.alive())
        status = again.status()
        self.assertEqual((status.alive, status.outcome), (False, "needs_input"))
        self.assertNotIn(status.state, (StatusState.GONE, StatusState.DEAD))
        self.assertEqual(status.attach, f"agent attach {view._state.agent_name}")
        self.assertNotIn("release", self.kinds())

    def test_a_locked_runs_answer_goes_to_its_live_worker(self):
        view = self.owned(lock="dummy-lock")
        _seen, on = self.at_begun(
            lambda: (self.record.bind(1, 1), self.record.complete(1, self.NEEDS, sid=SID))
        )
        view.run(on)
        agent, launches = view.driver.session, len(harness_launches())
        DrivenDelegate.attach(self.work, session_cls=HarnessSession).resume(input="the answer")
        self.assertEqual(agent.sent, ["the answer"])
        self.assertTrue(agent.alive())
        self.assertEqual(len(harness_launches()), launches)
        self.assertNotIn("release", self.kinds())

    def waiting(self, work: str, sid: str) -> HarnessDelegate:
        """A LockingSession run on dummy-lock, its attempt ended needs_input, its worker live."""
        view = self.owned(lock="dummy-lock", work=work, sid=sid, session_cls=LockingSession)
        record = RunRecord(Layout(work))
        _seen, on = self.at_begun(
            lambda: (record.bind(1, 1), record.complete(1, self.NEEDS, sid=sid))
        )
        self.assertEqual(view.run(on), self.NEEDS)
        return view

    def busy_until(self, end) -> None:
        first = self.waiting(self.work, SID)
        holder = first._state.agent_name
        second = self.owned(
            lock="dummy-lock",
            work=os.path.join(self.home, "second"),
            sid=str(uuid.uuid4()),
            session_cls=LockingSession,
        )
        result = second.run()
        self.assertEqual(
            result,
            Result(75, None, f"busy: lock dummy-lock is held by {holder} (owner {holder})"),
        )
        self.assertEqual(report(result, None), (75, None))
        end(first)
        self.waiting(os.path.join(self.home, "third"), str(uuid.uuid4()))

    def test_a_second_run_on_a_waiting_runs_lock_is_busy_until_its_user_closes_it(self):
        self.busy_until(lambda first: first.close())
        self.assertEqual(self.kinds().count("release"), 1)

    def test_a_second_run_on_a_waiting_runs_lock_is_busy_until_its_client_exits(self):
        def exits(first):
            first.driver.session.liveness = Liveness.ABSENT

        self.busy_until(exits)
        first = DrivenDelegate.attach(self.work, session_cls=LockingSession)
        first.close()  # its exited worker: the third run holds the lock now
        self.assertNotIn("release", self.kinds())
        self.assertNotIn(f"session {first._state.agent_name} closed (lock", self.err.getvalue())

    def test_closing_a_locked_run_of_unknown_liveness_claims_release_once_it_closed(self):
        first = self.waiting(self.work, SID)
        first.driver.session.liveness = Liveness.UNKNOWN
        DrivenDelegate.attach(self.work, session_cls=LockingSession).close()
        self.assertEqual(first.driver.session.liveness, Liveness.ABSENT)
        self.assertEqual(self.kinds().count("release"), 1)
        self.assertIn(
            f"session {first._state.agent_name} closed (lock dummy-lock)", self.err.getvalue()
        )

    def test_closing_a_live_locked_run_whose_status_lost_its_host_claims_release(self):
        first = self.waiting(self.work, SID)
        agent = first.driver.session
        gone = replace(agent.status(), alive=False, state=StatusState.GONE)
        with mock.patch.object(agent, "status", return_value=gone):
            DrivenDelegate.attach(self.work, session_cls=LockingSession).close()
        self.assertEqual(agent.liveness, Liveness.ABSENT)
        self.assertEqual(self.kinds().count("release"), 1)

    def test_a_closed_locked_run_resumes_by_taking_its_lock_again(self):
        first = self.waiting(self.work, SID)
        DrivenDelegate.attach(self.work, session_cls=LockingSession).close()
        other = self.waiting(os.path.join(self.home, "other"), str(uuid.uuid4()))
        holder = other._state.agent_name
        again = DrivenDelegate.attach(self.work, session_cls=LockingSession)
        with self.assertRaisesRegex(
            SessionError, f"busy: lock dummy-lock is held by {holder} \\(owner {holder}\\)"
        ):
            again.resume(input="the answer")
        self.assertEqual(self.metadata()["attempt"], 1)
        other.driver.session.close()
        DrivenDelegate.attach(self.work, session_cls=LockingSession).resume(input="the answer")
        self.assertEqual(self.metadata()["attempt"], 2)
        resumed = LockingSession.live[first._state.agent_name]
        self.assertTrue(resumed.alive())
        self.assertEqual(resumed.spec.environment.lock, "dummy-lock")

    def test_resume_without_input_closes_a_live_locked_worker_and_claims_release(self):
        view = self.owned(lock="dummy-lock", session_cls=LockingSession)
        failed = Result(0, None, "the client's turn failed: server_overloaded (529)")
        _seen, on = self.at_begun(
            lambda: (self.record.bind(1, 1), self.record.complete(1, failed, sid=SID))
        )
        view.run(on)
        old = view.driver.session
        self.assertTrue(old.alive())
        DrivenDelegate.attach(self.work, session_cls=LockingSession).resume()
        self.assertFalse(old.alive())
        self.assertEqual(self.kinds().count("release"), 1)
        self.assertIn(
            f"session {view._state.agent_name} closed (lock dummy-lock)", self.err.getvalue()
        )
        self.assertEqual(self.metadata()["attempt"], 2)

    def test_a_start_failure_is_stored_announced_and_finished_once(self):
        view = self.owned()
        view.driver = AgentDriver(FailingSession, display=None)
        result = view.run()
        self.assertEqual(result, Result(1, None, "tui: fake spawn refused"))
        self.assertEqual(self.record.read().result, result)
        self.assertEqual(self.sent(), ["finished"])
        self.assertEqual(self.kinds()[-2:], ["result", "end"])
        with open(self.events_file, encoding="utf-8") as f:
            self.assertEqual([line.split()[-2:] for line in f], [["outcome", "error"]])

    def failing_start(self, during: Callable[[], object]) -> HarnessDelegate:
        """An owned run whose start fails after `during` ran (another writer meanwhile)."""
        view = self.owned()

        class Racing(FailingSession):
            @classmethod
            def start(cls, spec, *, display=None, host=None, idle_timeout=None):
                during()
                return super().start(spec, display=display, host=host, idle_timeout=idle_timeout)

        view.driver = AgentDriver(Racing, display=None)
        return view

    def test_a_start_failure_that_loses_returns_the_stored_result(self):
        view = self.failing_start(lambda: self.record.stop(CLOSED, sid=SID))
        finished = []
        result = view.run(lambda event: finished.append(event))
        self.assertEqual(result, CLOSED)
        self.assertEqual(self.record.read().result, CLOSED)
        self.assertEqual(finished, [Finished(CLOSED)])
        self.assertEqual(self.sent(), [])
        self.assertNotIn("result", self.kinds())

    def test_a_start_failure_after_a_bind_is_stored(self):
        view = self.failing_start(lambda: self.record.bind(1, 1))
        result = view.run()
        self.assertEqual(result, Result(1, None, "tui: fake spawn refused"))
        state = self.record.read()
        self.assertEqual((state.completed, state.result), (True, result))
        self.assertEqual(self.sent(), ["finished"])

    def stale_out(self) -> HarnessDelegate:
        """An owned run whose earlier out cannot be set aside: an out under a file."""
        view = self.owned()
        blocker = os.path.join(self.home, "file")
        with open(blocker, "w", encoding="utf-8"):
            pass
        p = view._state.plan
        view._state = replace(
            view._state, plan=replace(p, spec=replace(p.spec, out=os.path.join(blocker, "out.md")))
        )
        view._save(view._state)
        return view

    def test_a_stale_out_ending_is_stored_before_its_stop(self):
        view = self.stale_out()
        stop, lost = HarnessSession.stop, []

        def handled(session):
            self.record.bind(1, 1)
            lost.append(
                self.record.complete(1, Result(129, None, "stopped: Stopped: 129"), sid=SID)
            )
            stop(session)

        with mock.patch.object(HarnessSession, "stop", handled):
            result = view.run()
        self.assertEqual(lost, [False])
        self.assertTrue(result.error.startswith("cannot remove the stale out: "), result.error)
        state = self.record.read()
        self.assertEqual((state.result, state.closing), (result, True))
        self.assertEqual(self.sent(), ["finished"])
        self.assertEqual(self.kinds()[-2:], ["result", "end"])

    def test_a_stale_out_ending_that_loses_stops_and_returns_the_stored_one(self):
        view = self.stale_out()
        start = HarnessSession.start.__func__

        def handled(cls, spec, **kw):
            session = start(cls, spec, **kw)
            self.record.bind(1, 1)
            self.record.complete(1, self.NEEDS, sid=SID)
            return session

        with mock.patch.object(HarnessSession, "start", classmethod(handled)):
            result = view.run()
        self.assertEqual(result, self.NEEDS)
        self.assertEqual(self.record.read().result, self.NEEDS)
        agent = view.driver.session
        assert agent is not None
        self.assertFalse(agent.alive())
        self.assertNotIn("result", self.kinds())
        self.assertEqual(self.sent(), [])

    def test_resume_input_on_a_live_worker_only_sends_it(self):
        view = self.owned()
        _seen, on = self.at_begun(
            lambda: (self.record.bind(1, 1), self.record.complete(1, self.NEEDS, sid=SID))
        )
        view.run(on)
        agent, before, launches = view.driver.session, self.metadata(), len(harness_launches())
        again = DrivenDelegate.attach(self.work, session_cls=HarnessSession)
        record = again.resume(input="the answer")
        self.assertEqual(record.sid, SID)
        self.assertEqual(agent.sent, ["the answer"])
        self.assertEqual(self.metadata(), before)
        self.assertEqual(len(harness_launches()), launches)
        self.assertEqual(self.kinds()[-1], "input")
        agent.send = lambda _text: (_ for _ in ()).throw(SessionError("pane gone"))
        with self.assertRaisesRegex(SessionError, "pane gone"):
            again.resume(input="again")

    def test_resume_input_reaches_a_live_worker_while_its_drive_winds_down(self):
        view = self.owned()
        _seen, on = self.at_begun(
            lambda: (self.record.bind(1, 1), self.record.complete(1, self.NEEDS, sid=SID))
        )
        view.run(on)
        agent = view.driver.session
        with RunState(Layout(self.work)).run_lock():
            DrivenDelegate.attach(self.work, session_cls=HarnessSession).resume(input="the answer")
        self.assertEqual(agent.sent, ["the answer"])

    def stopped_run_whose_drive_holds_the_run_lock(self, result: Result | None = None) -> int:
        """A completed owned run (default: stopped), its worker gone, its run lock held here: the
        descriptor.
        """
        view = self.owned()
        result = result or Result(129, None, "stopped: Stopped: 129")
        _seen, on = self.at_begun(
            lambda: (self.record.bind(1, 1), self.record.complete(1, result, sid=SID))
        )
        view.run(on)
        view.driver.session.liveness = Liveness.ABSENT
        fd = os.open(os.path.join(Layout(self.work).run, "delegate.run.lock"), os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def test_resume_waits_for_a_completed_runs_drive_to_return(self):
        fd = self.stopped_run_whose_drive_holds_the_run_lock()
        again = DrivenDelegate.attach(self.work, session_cls=HarnessSession)
        status = again.status()
        self.assertEqual((status.alive, status.state), (True, StatusState.WORKING))
        self.assertFalse(again.alive())  # not alive from its Finished on
        threading.Timer(0.3, os.close, (fd,)).start()
        again.resume()
        self.assertEqual(self.metadata()["attempt"], 2)

    def test_resume_input_waits_for_a_drive_whose_worker_exited(self):
        view = self.owned()
        _seen, on = self.at_begun(
            lambda: (self.record.bind(1, 1), self.record.complete(1, self.NEEDS, sid=SID))
        )
        view.run(on)
        view.driver.session.liveness = Liveness.ABSENT
        fd = os.open(os.path.join(Layout(self.work).run, "delegate.run.lock"), os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        threading.Timer(0.3, os.close, (fd,)).start()
        DrivenDelegate.attach(self.work, session_cls=HarnessSession).resume(input="the answer")
        data = self.metadata()
        self.assertEqual((data["attempt"], data["spec"]["input"]), (2, "the answer"))

    def assert_resume_refused_on_a_drive_that_never_returns(self, result=None, **kwargs):
        fd = self.stopped_run_whose_drive_holds_the_run_lock(result)
        self.addCleanup(os.close, fd)
        again = DrivenDelegate.attach(self.work, session_cls=HarnessSession)
        clock = itertools.count(0.0, 100.0)
        with (
            mock.patch("time.monotonic", side_effect=lambda: next(clock)),
            self.assertRaisesRegex(ConfigError, "delegate is still alive"),
        ):
            again.resume(**kwargs)
        status = again.status()
        self.assertEqual((status.alive, status.state), (True, StatusState.WORKING))
        return self.metadata()

    def test_status_and_resume_agree_on_a_drive_that_never_returns(self):
        self.assertEqual(self.assert_resume_refused_on_a_drive_that_never_returns()["attempt"], 1)

    def test_status_and_resume_input_agree_on_a_drive_that_never_returns(self):
        data = self.assert_resume_refused_on_a_drive_that_never_returns(
            self.NEEDS, input="the answer"
        )
        self.assertEqual(data["attempt"], 1)
        self.assertNotEqual(data["spec"]["input"], "the answer")

    def test_a_detached_owned_run_reads_the_run_lock(self):
        fd = self.stopped_run_whose_drive_holds_the_run_lock()
        gone = os.path.join(self.home, "gone-drive")
        self.record.save(replace(self.record.read(), detached=True, drive=gone))
        again = DrivenDelegate.attach(self.work, session_cls=HarnessSession)
        self.assertEqual(again.status().state, StatusState.WORKING)
        os.close(fd)
        self.assertEqual(again.status().state, StatusState.GONE)
        again.resume()
        self.assertEqual(self.metadata()["attempt"], 2)

    def test_resume_of_an_ended_worker_registers_the_next_worker(self):
        view = self.owned()
        _seen, on = self.at_begun(lambda: (_ for _ in ()).throw(Stopped(129)))
        with self.assertRaises(Stopped):
            view.run(on)
        view.driver.session.liveness = Liveness.ABSENT
        DrivenDelegate.attach(self.work, session_cls=HarnessSession).resume()
        data = self.metadata()
        self.assertEqual(
            (data["attempt"], data["bound"], data["previous"]),
            (2, False, None),
        )
        self.assertEqual(data["plan"]["session"]["handlers"][0]["state"]["worker"], 2)

    def test_resume_follows_a_draining_harness_out_of_the_mutex_before_its_sweep(self):
        """Its handler still delivers the gone worker's Finished (both callback locks held); the
        agent's close, answered once that harness drained, lets them go.
        """
        view = self.owned()
        stopped = Result(129, None, "stopped: Stopped: 129")
        _seen, on = self.at_begun(
            lambda: (self.record.bind(1, 1), self.record.complete(1, stopped, sid=SID))
        )
        view.run(on)
        view.driver.session.liveness = Liveness.ABSENT
        store = RunState(Layout(self.work))
        held = []
        for lock in (store.callback_lock, store.callback_parent_lock):
            fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            held.append(fd)
        os.write(held[0], f"4321 {os.getpid()}\n".encode())
        close, mutexed = HarnessSession.close, []

        def drained(session):
            mutexed.append(store.held(os.path.join(Layout(self.work).run, "delegate.lock")))
            for fd in held:
                os.close(fd)
            held.clear()
            close(session)

        self.addCleanup(lambda: [os.close(fd) for fd in held])
        with mock.patch.object(HarnessSession, "close", drained):
            DrivenDelegate.attach(self.work, session_cls=HarnessSession).resume()
        self.assertEqual(mutexed, [False])
        self.assertEqual(self.metadata()["attempt"], 2)

    def test_settle_retires_nothing_while_a_run_holds_it_or_its_attempt_opens(self):
        view = self.owned()
        _seen, on = self.at_begun(lambda: (_ for _ in ()).throw(Stopped(129)))
        with self.assertRaises(Stopped):
            view.run(on)
        view.driver.session.liveness = Liveness.ABSENT
        again = DrivenDelegate.attach(self.work, session_cls=HarnessSession)
        with mock.patch.object(HarnessSession, "retire") as retire:
            again._settle()  # its attempt opening: a start may be on its way
            with RunState(Layout(self.work)).run_lock():
                self.record.bind(1, 1)
                again._settle()  # a run holds it
            retire.assert_not_called()
            again._settle()
        retire.assert_called_once_with(again._state.agent_name, sid=SID, host=again._state.host)

    def test_settle_retires_under_the_run_lock_only_the_attempt_it_checked(self):
        view = self.owned()
        stopped = Result(129, None, "stopped: Stopped: 129")
        _seen, on = self.at_begun(
            lambda: (self.record.bind(1, 1), self.record.complete(1, stopped, sid=SID))
        )
        view.run(on)
        view.driver.session.liveness = Liveness.ABSENT
        again, store = DrivenDelegate.attach(self.work, session_cls=HarnessSession), self.record
        held = []
        with mock.patch.object(
            HarnessSession,
            "retire",
            side_effect=lambda *_a, **_k: held.append(store.store.running()),
        ):
            again._settle()
            self.assertEqual(held, [True])  # no start can begin a new generation meanwhile
            read = again._read

            def replaced():  # another attempt saved between its check and its lock
                state = read()
                again._read = read
                return replace(state, attempt=state.attempt + 1)

            again._read = replaced
            again._settle()
        self.assertEqual(held, [True])
