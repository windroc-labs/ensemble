"""HostedSession: its configured classes and Setup, its class methods (start, attach, list,
usage, layout, writable_dirs, display, host, class_roots, inspect) and a session's own methods, each
test on a configured class (self.session) with caller-owned dependencies.
"""

import contextlib
import dataclasses
import errno
import fcntl
import gc
import io
import itertools
import json
import logging
import os
import select
import shlex
import shutil
import signal
import stat
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest import mock

from ensemble.session import Host, NoRecordedSession, NotShowable, Viewer
from ensemble.session.clients import common as selection
from ensemble.session.clients.base import Client
from ensemble.session.clients.claude import ClaudeClient
from ensemble.session.clients.codex import CodexClient
from ensemble.session.clients.codex.common import SessionStore
from ensemble.session.engine import hosted_session, worker
from ensemble.session.engine import types as engine_types
from ensemble.session.engine.hosted_session import HostedSession
from ensemble.session.engine.records import RunLog
from ensemble.session.engine.types import (
    Access,
    Activity,
    Blocked,
    ClientBound,
    ConfigError,
    Exited,
    Hosts,
    IdleTimeout,
    Key,
    KeyPress,
    Liveness,
    Mode,
    Paste,
    Reply,
    SessionError,
    SessionInfo,
    SessionRow,
    SessionStatus,
    Spec,
    Stderr,
    TurnEnd,
    TurnFailed,
    Unanswered,
    Usage,
    Window,
)
from ensemble.session.harness.channel import Channel
from ensemble.session.harness.types import Op
from ensemble.session.harness.view import OwnedSession
from ensemble.session.hosts.base import Piped
from ensemble.session.hosts.docker.host import DockerHost
from ensemble.session.hosts.docker.worker import DockerWorker
from ensemble.session.hosts.managed import ManagedProcess
from ensemble.session.hosts.tmux import TmuxHost
from ensemble.session.modes import tui
from ensemble.session.viewer import hidden as hidden_store
from ensemble.utils import ACCOUNT_ENV, Record, StatusState, exhausted, inode_lock, load_class
from tests.session import fake_client
from tests.session.case import HOSTS, REPO, SPAWNS, SRC, Case
from tests.session.fakes.container_host import HOMED as HOMED_HOST
from tests.session.fakes.container_host import PATH as CONTAINER_HOST
from tests.session.fakes.host import FakeHost
from tests.session.fakes.terminal import MemoryHost, MemoryViewer, ScreenHost
from tests.session.hosted_support import (
    LAUNCHED,
    PYTHONPATH,
    SID,
    WAIT,
    Base,
    HostedEnvBase,
    HostedTuiBase,
    ProbeClient,
    TuiBase,
    fake_tmux,
    listed,
)
from tests.session.threads import capture_errors
from tests.utils.sandbox import owned_directory

UNCONFIGURED = "session not configured: call Session.configure first"
FAKE_HOSTS = {"fake": {"implementation": "tests.session.fakes.host:FakeHost"}}


class CapacityPiped(Piped):
    def __init__(self):
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        self.code = None

    def wait(self, timeout):
        return self.code

    def stop(self, *, ended):
        self.code = 0


class CapacityHost(ScreenHost):
    close_failure = None
    inventory_failure = None
    launch_failure = None

    def create(self, *args, **kwargs):
        terminal = super().create(*args, **kwargs)
        terminal.calls.close.side_effect = self.close_failure
        if self.inventory_failure is not None:
            self.calls.list.side_effect = self.inventory_failure
        return terminal

    def terminal(self, *args, **kwargs):
        terminal = super().terminal(*args, **kwargs)
        if self.launch_failure is not None:
            raise self.launch_failure
        return terminal

    def pipe(self, name, launch, *, metadata=None):
        self.calls.pipe(name, launch, metadata=metadata)
        return CapacityPiped()


class ClientCapacityTest(HostedTuiBase):
    def setUp(self):
        super().setUp()
        self.host = CapacityHost("tmux")
        self.configure(tui=True, max_runs=1)

    def _other_configuration(self):
        setup = self.session._config()
        other = ScreenHost("elsewhere")
        return HostedSession.configured(
            clients=setup.registry.settings.clients,
            hosts=parsed(
                {"elsewhere": {"implementation": "tests.session.fakes.terminal:ScreenHost"}},
                "elsewhere",
            ),
            class_loader=setup.class_loader,
            host_factory=lambda _kind, _name, _settings: other,
            viewer_factory=MemoryViewer,
        )

    def test_independent_configurations_share_capacity_and_attached_closes_release_it(self):
        for action in ("original", "attached-close", "attached-stop", "retire"):
            with self.subTest(action=action):
                other = self._other_configuration()
                first = self.start(mode="tui", name="one", sid="s1")
                spec = self.spec(client="fake", mode="tui", name="two", sid="s2")
                with self.assertRaisesRegex(SessionError, "one"):
                    other.start(spec)
                if action == "original":
                    first.close()
                elif action == "retire":
                    self.session.retire("one", sid="s1")
                else:
                    attached = self.session.attach("one", sid="s1")
                    attached.close() if action == "attached-close" else attached.stop()
                other.start(spec).close()

    def test_a_stale_attached_stop_keeps_the_replacement_reservation(self):
        first = self.start(mode="tui", name="one", sid="same")
        attached = self.session.attach("one", sid="same")
        first.close()
        replacement = self.start(mode="tui", name="one", sid="same")
        attached.stop()
        other = self._other_configuration()
        with self.assertRaisesRegex(SessionError, "one"):
            other.start(self.spec(client="fake", mode="tui", name="two"))
        replacement.close()

    def test_launch_and_publication_failures_release_the_reserved_slot(self):
        for mode, boundary in (
            ("headless", self.host.calls.pipe),
            ("tui", self.host.calls.terminal),
            ("tui", self.host.calls.close_view),
        ):
            with self.subTest(mode=mode, boundary=boundary):
                boundary.side_effect = SessionError("fake launch failure")
                with self.assertRaisesRegex(SessionError, "fake launch failure"):
                    self.start(mode=mode, name="one", sid="s1")
                boundary.side_effect = None
                self.start(mode=mode, name="one", sid="s2").close()

    def test_unconfirmed_failed_shutdown_keeps_the_durable_reservation(self):
        for phase in ("publication", "handoff", "handoff-visible"):
            with self.subTest(phase=phase):
                other = self._other_configuration()
                self.host.close_failure = SessionError("shutdown unconfirmed")
                self.host.inventory_failure = (
                    None if phase == "handoff-visible" else SessionError("host unavailable")
                )
                self.host.launch_failure = (
                    None if phase == "publication" else SessionError("handoff failed")
                )
                self.host.calls.close_view.side_effect = SessionError("publication failed")
                with self.assertRaises(SessionError):
                    self.start(mode="tui", name="one", sid="s1")
                spec = self.spec(client="fake", mode="tui", name="two", sid="s2")
                with self.assertRaisesRegex(SessionError, "one"):
                    admitted = other.start(spec)
                    self.addCleanup(admitted.close)
                self.host.calls.list.side_effect = None
                self.host.calls.close_view.side_effect = None
                self.host.processes["one"].calls.close.side_effect = None
                self.session.retire("one", sid="s1")
                other.start(spec).close()

    def test_each_mode_and_resumed_start_uses_the_same_client_capacity(self):
        for mode, name in (("tui", "one"), ("headless", None), ("headless", "one")):
            with self.subTest(mode=mode, name=name):
                first = self.start(mode=mode, name=name, sid="s1")
                for next_mode, next_name in (("headless", None), ("tui", "two")):
                    with self.assertRaisesRegex(SessionError, "client fake.*limit 1"):
                        self.start(mode=next_mode, name=next_name, sid="s2", resume=True)
                first.close()
                again = self.start(mode=mode, name=name, sid="s3", resume=True)
                again.close()

    def test_refusal_happens_before_the_session_record_and_host_launch(self):
        first = self.start(mode="tui", name="one", sid="s1")
        before, calls = self.written(), len(self.host.calls.mock_calls)
        with self.assertRaisesRegex(SessionError, "one"):
            self.start(mode="headless", sid="s2")
        self.assertEqual(self.written(), before)
        self.assertEqual(self.host.calls.pipe.call_count, 0)
        self.assertGreaterEqual(len(self.host.calls.mock_calls), calls)
        first.close()

    def test_preflight_does_not_reserve_and_other_clients_remain_available(self):
        spec = self.spec(client="fake", mode="headless", prompt="hi")
        self.session.preflight(spec)
        first = self.start()
        self.start(client="quiet").close()
        first.stop()
        self.start().close()

    def test_rejected_validation_leaves_no_capacity_state(self):
        for existing in (True, False):
            if not existing:
                os.rmdir(self.ensemble)
            before = sorted(str(p.relative_to(self.home)) for p in Path(self.home).rglob("*"))
            with (
                self.subTest(existing=existing),
                self.assertRaisesRegex(ConfigError, "events: tui mode only"),
            ):
                self.start(events=os.path.join(self.home, "events"))
            self.assertEqual(
                sorted(str(p.relative_to(self.home)) for p in Path(self.home).rglob("*")), before
            )

    def test_in_process_completion_without_events_and_stop_release_capacity(self):
        reached, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        self.configure(client="checked", max_runs=1)
        self.steps({"sleep": 1})

        def sleep(_seconds):
            reached.set()
            release.wait(5)

        with mock.patch("time.sleep", side_effect=sleep):
            first = self.start(client="checked", mode="in_process", sid="a")
            self.assertTrue(reached.wait(5))
            with self.assertRaisesRegex(SessionError, "client checked"):
                self.start(client="checked", mode="in_process", sid="b")
            release.set()
            self.assertTrue(first._capacity.finished.wait(5))
            self.start(client="checked", mode="in_process", sid="c").close()


PLAIN = cast(Spec, SimpleNamespace(environment=None))  # all _owned reads of a spec
ENVIRONED = cast(Spec, SimpleNamespace(environment=mock.Mock(spec=engine_types.EnvironmentSpec)))


def parsed(tables, default) -> Hosts:
    return Host.parse_hosts({**tables, "default": default})


class AliasedClientCapacityTest(TuiBase):
    def test_closing_through_an_alias_releases_the_original_reservation_for_disjoint_hosts(self):
        for action in ("close", "stop", "retire"):
            with self.subTest(action=action):
                self.configure(tui=True, max_runs=1)
                setup = self.session._config()
                self.host.checked.return_value = SimpleNamespace(stdout="/fake/server.sock\n")
                offline = ScreenHost("offline")

                def build(kind, name, table, *, unavailable=offline):
                    return unavailable if name == "offline" else kind(name, table, tmux=self.host)

                self.session = HostedSession.configured(
                    clients=setup.registry.settings.clients,
                    hosts=parsed(
                        {
                            "tmux": HOSTS["tmux"],
                            "alias": HOSTS["tmux"],
                            "offline": {
                                "implementation": "tests.session.fakes.terminal:ScreenHost"
                            },
                        },
                        "tmux",
                    ),
                    class_loader=setup.class_loader,
                    host_factory=build,
                    viewer_factory=lambda settings: Viewer(settings, tmux=self.host),
                )
                other = ScreenHost("elsewhere")
                disjoint = HostedSession.configured(
                    clients=setup.registry.settings.clients,
                    hosts=parsed(
                        {
                            "elsewhere": {
                                "implementation": "tests.session.fakes.terminal:ScreenHost"
                            }
                        },
                        "elsewhere",
                    ),
                    class_loader=setup.class_loader,
                    host_factory=lambda _kind, _name, _table, target=other: target,
                    viewer_factory=MemoryViewer,
                )
                self.start(mode="tui", name="one", sid=SID)
                offline.calls.list.side_effect = SessionError("unrelated host offline")
                if action == "retire":
                    self.session.retire("one", sid=SID, host="alias")
                else:
                    attached = self.session.attach("one", sid=SID, host="alias")
                    attached.close() if action == "close" else attached.stop()
                next_session = disjoint.start(self.spec(client="fake", mode="tui", name="two"))
                next_session.close()

    def test_two_tmux_aliases_count_one_existing_tui_and_still_enforce_the_limit(self):
        self.configure(client="checked", max_runs=2)
        setup = self.session._config()
        self.host.checked.return_value = SimpleNamespace(stdout="/fake/server.sock\n")
        self.host.listing.return_value = [listed("existing", client="checked", sid="s1")]
        self.session = HostedSession.configured(
            clients=setup.registry.settings.clients,
            hosts=parsed(
                {"tmux": HOSTS["tmux"], "alias": {**HOSTS["tmux"], "status_line": False}},
                "tmux",
            ),
            class_loader=setup.class_loader,
            host_factory=lambda kind, name, table: kind(name, table, tmux=self.host),
            viewer_factory=MemoryViewer,
        )
        reached, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        self.steps({"sleep": 1})

        def sleep(_seconds):
            reached.set()
            release.wait(WAIT)

        with mock.patch("time.sleep", side_effect=sleep):
            second = self.start(client="checked", mode="in_process", sid="s2")
            self.assertTrue(reached.wait(WAIT))
            with self.assertRaisesRegex(SessionError, "client checked.*limit 2.*existing.*s2"):
                self.start(client="checked", mode="in_process", sid="s3")
            release.set()
            self.assertTrue(second._capacity.finished.wait(WAIT))


class ConfiguredTest(Case):
    def test_raw_configuration_parses_hosts_and_copies_client_and_host_tables(self):
        own = type("Own", (HostedSession,), {})
        config = {
            "hosts": {"default": "fake", **FAKE_HOSTS},
            "clients": {"fake": {"implementation": "tests.session.fake_client:FakeClient"}},
        }
        own.configure_from(config)
        config["hosts"]["default"] = "missing"
        config["clients"]["fake"]["implementation"] = "missing.module:Missing"
        self.assertIsInstance(own.host(), FakeHost)
        self.assertIsNone(own.usage("fake"))

    def test_raw_configuration_accepts_preparsed_hosts(self):
        own = type("Own", (HostedSession,), {})
        hosts = parsed(FAKE_HOSTS, "fake")
        own.configure_from({"hosts": None}, hosts=hosts)
        self.assertIsInstance(own.host(), FakeHost)

    def test_raw_configuration_keeps_parser_and_client_errors(self):
        own = type("Own", (HostedSession,), {})
        with self.assertRaises(ConfigError) as caught:
            own.configure_from({})
        self.assertEqual(str(caught.exception), "[hosts]: missing")
        with self.assertRaises(ConfigError) as caught:
            own.configure_from({"clients": []}, hosts=parsed(FAKE_HOSTS, "fake"))
        self.assertEqual(
            str(caught.exception), "clients: want a mapping of [clients.<name>] tables"
        )

    def test_each_configured_class_has_its_own_setup_and_none_changes_hosted_session(self):
        first = HostedSession.configured(clients={}, hosts=parsed(HOSTS, "tmux"))
        second = HostedSession.configured(clients={}, hosts=parsed(FAKE_HOSTS, "fake"))
        self.assertTrue(issubclass(first, HostedSession))
        self.assertIsNot(first, second)
        self.assertIsInstance(first.host(), TmuxHost)
        self.assertIsInstance(second.host(), FakeHost)
        self.assertIsNone(HostedSession._setup)
        with self.assertRaises(ConfigError) as caught:
            HostedSession.host()
        self.assertEqual(str(caught.exception), UNCONFIGURED)

    def test_a_subclass_of_a_configured_class_starts_unconfigured(self):
        configured = HostedSession.configured(clients={}, hosts=parsed(HOSTS, "tmux"))
        later = type("Later", (configured,), {})
        with self.assertRaises(ConfigError) as caught:
            later.host()
        self.assertEqual(str(caught.exception), UNCONFIGURED)

    def test_an_unconfigured_class_refuses_each_class_method(self):
        fresh = type("Fresh", (HostedSession,), {})
        spec = Spec(
            workdir=self.home, access=Access(cwd=self.home), client="fake", mode=Mode.HEADLESS
        )
        for name, call in (
            ("list", fresh.list),
            ("list display ''", lambda: fresh.list(display="")),
            ("start", lambda: fresh.start(spec)),
            ("attach", lambda: fresh.attach("w1")),
            ("inspect", lambda: fresh.inspect(spec)),
            ("usage", lambda: fresh.usage("fake")),
            ("layout", fresh.layout),
            ("writable_dirs", lambda: fresh.writable_dirs(spec)),
            ("display", fresh.display),
            ("host", fresh.host),
            ("class_roots", lambda: fresh.class_roots("fake")),
        ):
            with self.subTest(name=name), self.assertRaises(ConfigError) as caught:
                call()
            self.assertEqual(str(caught.exception), UNCONFIGURED)

    def test_configure_takes_parsed_hosts_only(self):
        with self.assertRaises(ConfigError) as caught:
            HostedSession.configured(clients={}, hosts={**HOSTS, "default": "tmux"})
        self.assertEqual(str(caught.exception), "hosts: want parsed Hosts (Host.parse_hosts)")


class InspectTest(Base):
    def inspect(self, spec):
        with contextlib.ExitStack() as stack:
            starts = [
                stack.enter_context(mock.patch(target, side_effect=AssertionError(target)))
                for target in SPAWNS
            ]
            answer = self.session.inspect(spec)
        for start in starts:
            start.assert_not_called()
        return answer

    def test_historical_status_resolves_the_recorded_cwd_inside_session(self):
        os.makedirs(self.workdir)
        log = RunLog(self.workdir)
        info = SessionInfo(sid=SID, cwd=self.dir, project=False, transcript="", resume="")
        log.note("session", **info.persisted())
        log.note(
            "session_metadata",
            sid=SID,
            transcript=os.path.join(self.dir, "transcript"),
            resume="saved hint",
        )
        log.note("session", **dataclasses.replace(info, sid="another", cwd=self.home).persisted())
        spec = Spec(workdir=self.workdir, client="probe", sid=SID, access=Access(cwd=self.home))
        self.assertEqual(self.inspect(spec), SessionStatus("yes", 1.5, f"probe {SID} {self.dir}"))
        self.assertFalse(os.path.exists(self.log))

    def test_inspection_passes_folded_transcript_and_resume_metadata_to_the_client(self):
        self.configure(
            tables={
                "history": {
                    "implementation": "ensemble.session.clients.fake:InspectClient",
                    "scenario": self.scenario,
                    "log": self.log,
                }
            }
        )
        os.makedirs(self.workdir)
        log = RunLog(self.workdir)
        info = SessionInfo(
            sid=SID, cwd=self.dir, project=False, transcript="/old/transcript", resume="old resume"
        )
        log.note("session", **info.persisted())
        log.note("session_metadata", sid=SID, transcript="/saved/transcript", resume="saved resume")
        self.assertEqual(
            self.inspect(
                Spec(
                    workdir=self.workdir, access=Access(cwd=self.workdir), client="history", sid=SID
                )
            ),
            SessionStatus("yes", 1.5, f"{SID} {self.dir} /saved/transcript saved resume"),
        )

    def test_a_sid_without_a_record_is_an_error_without_starting_a_process(self):
        spec = Spec(workdir=self.workdir, access=Access(cwd=self.workdir), client="probe", sid=SID)
        with self.assertRaisesRegex(NoRecordedSession, "no recorded session"):
            self.inspect(spec)
        self.assertFalse(os.path.exists(self.workdir))

    def recorded(self, client="probe") -> Spec:
        """`client`'s inspection of session s1, recorded in self.workdir with cwd self.dir."""
        os.makedirs(self.workdir)
        RunLog(self.workdir).note(
            "session", sid="s1", cwd=self.dir, project=False, transcript="", resume=""
        )
        return Spec(workdir=self.workdir, access=Access(cwd=self.workdir), client=client, sid="s1")

    def test_a_clients_driver_error_propagates_as_raised(self):
        error = SessionError("inspection refused")
        spec = self.recorded()
        with (
            mock.patch.object(ProbeClient, "session_status", side_effect=error),
            self.assertRaises(SessionError) as caught,
        ):
            self.inspect(spec)
        self.assertIs(caught.exception, error)

    def test_an_os_error_from_the_client_is_unknown(self):
        spec = self.recorded()
        with mock.patch.object(
            ProbeClient, "session_status", side_effect=PermissionError(13, "denied")
        ):
            self.assertEqual(self.inspect(spec), SessionStatus("unknown", None, ""))

    def test_inspection_gives_the_client_the_specs_launch_env(self):
        spec = dataclasses.replace(
            self.recorded(),
            access=Access(cwd=self.workdir, env={"A": "access", "B": "access"}),
            env={"B": "spec"},
        )
        with mock.patch.object(
            ProbeClient,
            "session_status",
            autospec=True,
            return_value=SessionStatus("no", None, ""),
        ) as status:
            self.inspect(spec)
        self.assertEqual(status.call_args.kwargs["env"], {"A": "access", "B": "spec"})

    def test_an_unknown_client_is_the_registrys_config_error(self):
        spec = self.recorded("nope")
        with self.assertRaises(ConfigError) as caught:
            self.inspect(spec)
        self.assertEqual(str(caught.exception), "no [clients.nope] table in the configured clients")

    def test_inspection_requires_client_and_sid(self):
        for spec, message in (
            (
                Spec(workdir=self.workdir, access=Access(cwd=self.workdir), sid=SID),
                "spec.client: required to inspect",
            ),
            (
                Spec(workdir=self.workdir, access=Access(cwd=self.workdir), client="probe"),
                "spec.sid: required to inspect",
            ),
        ):
            with self.subTest(spec=spec), self.assertRaises(ConfigError) as caught:
                self.inspect(spec)
            self.assertEqual(str(caught.exception), message)


class ClientResumeTest(Base):
    def test_a_client_without_a_resume_command_is_a_config_error(self):
        with self.assertRaisesRegex(ConfigError, "^this client has no resume command$"):
            self.session.client_resume("fake", ["x"])


class HistoricalRecordTest(Case):
    def setUp(self):
        directory = owned_directory(self, os.environ.get("ENSEMBLE_TEST_TMP", REPO))
        self.root = Path(directory).resolve()
        self.enterContext(mock.patch.dict(os.environ, {"HOME": str(self.root)}))
        self.work = self.root / "work"
        self.cwd = self.work / "project ' quoted"
        self.cwd.mkdir(parents=True)

    def forged(self, client, transcript, *, expected):
        log = RunLog(str(self.work))
        info = SessionInfo(sid=SID, cwd=str(self.cwd), project=True, transcript="", resume="")
        log.note("session", **info.persisted())
        log.note("session_metadata", sid=SID, transcript=str(transcript), resume="rm -rf ~")
        request = Spec(
            workdir=str(self.work), access=Access(cwd=str(self.work)), client=client, sid=SID
        )
        self.assertEqual(self.session_cls.inspect(request), expected)
        log.note("session_metadata", sid=SID, transcript="", resume="printf forged-command")
        fresh = self.session_cls.inspect(request)
        self.assertEqual(fresh, SessionStatus("yes", 100.0, expected.resume_command))
        self.assertNotIn("forged-command", fresh.resume_command)

    def test_claude_resume_is_built_fresh_and_an_outside_recorded_transcript_is_ignored(self):
        config = {"env": {"CLAUDE_CONFIG_DIR": str(self.root / "claude")}}
        self.configure_session(
            {
                "claude": {
                    "implementation": "ensemble.session.clients.claude:ClaudeClient",
                    **config,
                }
            }
        )
        client = ClaudeClient(config)
        transcript = Path(client.common.transcript(str(self.cwd), SID))
        transcript.parent.mkdir(parents=True)
        transcript.write_text("{}\n", encoding="utf-8")
        os.utime(transcript, (100, 100))
        outside = self.root / "outside.jsonl"
        outside.write_text("{}\n", encoding="utf-8")
        os.utime(outside, (9000, 9000))
        command = client.common.resume(str(self.cwd), SID)
        self.forged("claude", outside, expected=SessionStatus("yes", 100.0, command))

    def test_codex_resume_is_built_fresh_and_a_forged_outside_transcript_is_refused(self):
        home = self.root / ".codex"
        home.mkdir()
        config = {"env": {"CODEX_HOME": str(home)}}
        self.configure_session(
            {"codex": {"implementation": "ensemble.session.clients.codex:CodexClient", **config}}
        )
        client = CodexClient(config)
        spec = Spec(workdir=str(self.work), sid=SID, access=Access(cwd=str(self.cwd), project=True))
        client.common.save(client.common.prepare(spec))
        client.common.bind(str(self.work), SID, SID)
        transcript = home / "sessions" / "2026" / "10" / "11" / f"rollout-test-{SID}.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text(
            json.dumps({"type": "session_meta", "payload": {"id": SID, "cwd": str(self.cwd)}}),
            encoding="utf-8",
        )
        os.utime(transcript, (100, 100))
        outside = self.root / f"rollout-forged-{SID}.jsonl"
        outside.write_text(
            json.dumps({"type": "session_meta", "payload": {"id": SID, "cwd": str(self.root)}}),
            encoding="utf-8",
        )
        os.utime(outside, (9000, 9000))
        locators = [SessionStore().root, str(self.work), SID, SID]
        argv = ["ensemble-session", "client-resume", "codex", *locators]
        command = f"cd {shlex.quote(str(self.cwd))} && {shlex.join(argv)}"
        self.forged("codex", outside, expected=SessionStatus("unknown", None, command))


class HostTest(Case):
    def test_the_default_host_built_once_per_class_from_its_table_less_implementation(self):
        built = []

        def factory(cls, name, settings):
            built.append((cls, name, settings))
            return cls(name, settings)

        tables = {"tmux": {**HOSTS["tmux"], "status_line": True}, **FAKE_HOSTS}
        configured = HostedSession.configured(
            clients={}, hosts=parsed(tables, "tmux"), host_factory=factory
        )
        default = configured.host()
        self.assertIs(configured.host(), default)
        self.assertIs(configured.host("tmux"), default)
        self.assertIsInstance(configured.host("fake"), FakeHost)
        self.assertEqual(
            [(cls, name, settings) for cls, name, settings in built],
            [(TmuxHost, "tmux", {"status_line": True}), (FakeHost, "fake", {})],
        )
        self.assertEqual((default.name, default.settings), ("tmux", {"status_line": True}))
        other = HostedSession.configured(clients={}, hosts=parsed(tables, "tmux"))
        self.assertIsNot(other.host(), default)

    def test_the_viewer_built_once_per_class_from_its_viewer_table(self):
        built = []

        def factory(settings):
            built.append(settings)
            return mock.sentinel.viewer

        configured = HostedSession.configured(
            clients={},
            hosts=parsed(HOSTS, "tmux"),
            viewer={"grid_retile": "off"},
            viewer_factory=factory,
        )
        self.assertIs(configured.viewer(), mock.sentinel.viewer)
        self.assertIs(configured.viewer(), mock.sentinel.viewer)
        self.assertEqual(built, [{"grid_retile": "off"}])
        with self.assertRaisesRegex(ConfigError, r"^\[viewer\]: want a table$"):
            HostedSession.configured(clients={}, hosts=parsed(HOSTS, "tmux"), viewer=[])

    def test_each_built_host_gets_its_own_settings_copy(self):
        hosts = parsed({"fake": {**FAKE_HOSTS["fake"], "sizes": [1]}}, "fake")
        first, second = (HostedSession.configured(clients={}, hosts=hosts).host() for _ in "ab")
        first.settings["sizes"].append(2)
        self.assertEqual(second.settings, {"sizes": [1]})
        self.assertEqual(hosts.tables["fake"]["sizes"], (1,))

    def test_an_unknown_host_is_refused(self):
        configured = HostedSession.configured(clients={}, hosts=parsed(HOSTS, "tmux"))
        for name in ("nope", 5):
            with self.subTest(name=name), self.assertRaises(ConfigError) as caught:
                configured.host(name)
            self.assertEqual(str(caught.exception), f"unknown host {name}")

    def test_an_implementation_that_is_no_host_is_refused(self):
        tables = {"x": {"implementation": "ensemble.session.engine.types:Spec"}}
        configured = HostedSession.configured(clients={}, hosts=parsed(tables, "x"))
        with self.assertRaises(ConfigError) as caught:
            configured.host()
        self.assertEqual(
            str(caught.exception),
            "[hosts.x].implementation: ensemble.session.engine.types:Spec must be a concrete"
            " Host subclass",
        )

    def test_the_class_loader_is_the_setups(self):
        loaded = []

        def loader(path, base):
            loaded.append((path, base))
            return FakeHost

        configured = HostedSession.configured(
            clients={}, hosts=parsed(HOSTS, "tmux"), class_loader=loader
        )
        self.assertIsInstance(configured.host(), FakeHost)
        self.assertEqual(loaded, [("ensemble.session.hosts.tmux:TmuxHost", Host)])


class ClassRootsTest(Case):
    CLIENTS = {
        "claude": {"implementation": "ensemble.session.clients.claude:ClaudeClient"},
        "fake": {"implementation": "tests.session.fake_client:FakeClient"},
        "broken": {"implementation": "tests.session.absent_module:Nothing"},
    }

    def configured(self, tables=None, **setup):
        tables = {**HOSTS, **FAKE_HOSTS} if tables is None else tables
        return HostedSession.configured(clients=self.CLIENTS, hosts=parsed(tables, "tmux"), **setup)

    def test_the_client_root_then_each_configured_hosts_deduplicated(self):
        configured = self.configured()
        self.assertNotIn(REPO, SRC.values())
        self.assertEqual(configured.class_roots("claude"), (SRC["session"], REPO))
        self.assertEqual(configured.class_roots("fake"), (REPO, SRC["session"]))

    def test_classes_load_through_the_setups_loader_and_no_host_is_built(self):
        loaded, built = [], []

        def loader(path, base):
            loaded.append((path, base))
            return load_class(path, base)

        configured = self.configured(
            class_loader=loader, host_factory=lambda *args: built.append(args)
        )
        configured.class_roots("fake")
        self.assertEqual(
            loaded,
            [
                ("tests.session.fake_client:FakeClient", Client),
                ("ensemble.session.hosts.tmux:TmuxHost", Host),
                ("tests.session.fakes.host:FakeHost", Host),
            ],
        )
        self.assertEqual(built, [])

    def test_an_unknown_client_or_a_class_it_cannot_load_is_a_config_error(self):
        absent = "tests.session.absent_module"
        missing = f"{absent}:Nothing: cannot import {absent}: No module named '{absent}'"
        for client, tables, message in (
            ("absent", None, "no [clients.absent] table in the configured clients"),
            ("Bad", None, "unsafe client name 'Bad': want a lowercase identifier"),
            ("broken", None, f"[clients.broken].implementation: {missing}"),
            (
                "fake",
                {**HOSTS, "x": {"implementation": f"{absent}:Nothing"}},
                f"[hosts.x].implementation: {missing}",
            ),
            (
                "fake",
                {**HOSTS, "x": {"implementation": "ensemble.session.engine.types:Spec"}},
                "[hosts.x].implementation: ensemble.session.engine.types:Spec must be a"
                " concrete Host subclass",
            ),
        ):
            with self.subTest(client=client, tables=tables):
                with self.assertRaises(ConfigError) as caught:
                    self.configured(tables).class_roots(client)
                self.assertEqual(str(caught.exception), message)

    def test_a_class_whose_module_has_no_source_file_is_a_config_error(self):
        sourceless = type("Sourceless", (FakeHost,), {"__module__": "builtins"})

        def loader(path, base):
            return sourceless if base is Host else load_class(path, base)

        with self.assertRaises(ConfigError) as caught:
            self.configured(class_loader=loader).class_roots("fake")
        self.assertEqual(
            str(caught.exception), "[hosts.tmux].implementation: builtins has no source file"
        )


class DisplayTest(HostedTuiBase):
    def test_a_non_tmux_host_accepts_an_opaque_display_ending_in_semicolon(self):
        self.enterContext(mock.patch.object(ScreenHost, "attachable", new=False))
        self.assertEqual(self.session.display("opaque;"), "opaque;")
        session = self.tui(name="w1", display="opaque;")
        self.assertEqual(self.host.calls.terminal.call_args.kwargs["display"], "opaque;")
        self.assertEqual(session.name, "w1")

    def test_display_is_completed_by_the_viewer_for_its_host(self):
        self.session.viewer().placement.opener.return_value = "mgr"
        for value, completed in (
            ("/below", "mgr/below"),
            (None, "mgr"),
            ("other/right", "other/right"),
            ("foreign value", "foreign value"),
        ):
            self.assertEqual(self.session.display(value, host="tmux"), completed)
        self.assertEqual(self.host.calls.mock_calls, [])

    def test_a_display_not_printable_or_ending_in_a_semicolon_is_refused(self):
        transport = fake_tmux(self)
        selected = TmuxHost("tmux", tmux=transport)
        given = self.session._config()
        self.session = HostedSession.configured(
            clients=given.registry.settings.clients,
            hosts=given.hosts,
            class_loader=given.class_loader,
            probe=given.probe,
            clock=given.clock,
            host_factory=lambda *_args: selected,
        )
        for bad in ("a\tb", "a\nb", "x;"):
            for name, call in (
                ("display", lambda bad=bad: self.session.display(bad)),
                ("layout", lambda bad=bad: self.session.layout(display=bad)),
                ("start", lambda bad=bad: self.tui(name="w1", display=bad)),
            ):
                with self.subTest(bad=bad, name=name), self.assertRaises(ConfigError) as caught:
                    call()
                self.assertEqual(
                    str(caught.exception),
                    f"display {bad!r}: want printable text not ending in ;",
                )
        self.assertEqual(transport.mock_calls, [])
        self.assertFalse(os.path.lexists(self.root))

    def test_layout_is_the_default_hosts_completed_display(self):
        placement = self.session.viewer().placement
        placement.opener.return_value = "mgr"
        self.assertEqual(self.session.layout(display="/below"), "mgr/below")
        placement.anchor.assert_called_once_with(None)
        self.assertEqual(self.host.calls.mock_calls, [])

    def test_layout_without_a_showable_default_host_is_none(self):
        configured = HostedSession.configured(clients={}, hosts=parsed(FAKE_HOSTS, "fake"))
        self.assertIsNone(configured.layout(display="/below"))

    def test_a_positional_display_is_refused(self):
        with self.assertRaises(TypeError):
            self.session.layout("/below")

    def test_a_tui_start_stores_its_display_as_given(self):
        self.tui(name="w1", display="mgr/below")
        self.assertEqual(self.host.calls.terminal.call_args.kwargs["display"], "mgr/below")
        self.tui(name="w2")
        self.assertIsNone(self.host.calls.terminal.call_args.kwargs["display"])

    def test_a_display_is_tui_only(self):
        self.refused(ConfigError, "display: tui mode only", lambda: self.start(display="mgr/below"))


class SessionTest(Base):
    def test_on_a_host_that_cannot_show_it_raises_not_showable(self):
        table = {"scenario": self.scenario, "log": self.log}
        configured = HostedSession.configured(
            clients={
                "fake": {
                    "implementation": "ensemble.session.clients.fake:InProcessFakeClient",
                    **table,
                }
            },
            hosts=parsed({**HOSTS, **FAKE_HOSTS}, "tmux"),
            class_loader=self.session_dependencies["class_loader"],
        )
        session = configured.start(
            self.spec(client="fake", mode=Mode.IN_PROCESS, prompt="hi"), host="fake"
        )
        self.addCleanup(session.stop)
        self.assertFalse(session.showable)
        for name, call in (
            ("show", session.show),
            ("hide", session.hide),
            ("attach_command", session.attach_command),
            ("screen", session.screen),
        ):
            with self.subTest(name=name), self.assertRaises(NotShowable) as caught:
                call()
            self.assertEqual(str(caught.exception), "host fake cannot show")
        self.assertFalse(session.status().hidden)
        self.assertEqual(self.drain(session)[-1], Exited(0))


class InProcessTest(Base):
    def setUp(self):
        super().setUp()
        self.configure(implementation="ensemble.session.clients.fake:InProcessFakeClient")

    def run_in_process(self, **kwargs) -> tuple[HostedSession, list]:
        """An in_process session started and drained, each of SPAWNS failing meanwhile and never
        called.
        """
        with contextlib.ExitStack() as stack:
            spawns = [
                stack.enter_context(mock.patch(target, side_effect=AssertionError(target)))
                for target in SPAWNS
            ]
            session = self.start(mode="in_process", **kwargs)
            found = self.drain(session)
        for spawn in spawns:
            spawn.assert_not_called()
        return session, found

    def logged(self) -> list[dict]:
        with open(self.log, encoding="utf-8") as f:
            return [json.loads(line) for line in f]

    def test_cleanup_joins_a_stopped_runs_thread_before_removing_its_files(self):
        self.steps({"sleep": 0.3})
        before = set(threading.enumerate())
        self.start(mode="in_process").stop()
        self.doCleanups()
        self.assertEqual([t for t in threading.enumerate() if t not in before], [])
        self.assertFalse(os.path.exists(self.dir))

    def test_an_in_process_session_has_no_terminal_to_press_keys_into(self):
        session, _ = self.run_in_process()
        with self.assertRaisesRegex(SessionError, "^press: a in_process session has no terminal$"):
            session.press((KeyPress("Escape"),))

    def test_a_spec_started_in_process_yields_reply_turn_end_then_exited_fr_12(self):
        self.steps(
            {"out": {"type": "message", "text": "hi"}},
            {"out": {"type": "result", "text": "done"}},
        )
        session = self.session.start(
            Spec(
                workdir=self.workdir,
                access=Access(cwd=self.workdir),
                client="fake",
                mode=Mode.IN_PROCESS,
                prompt="hello",
            )
        )
        self.addCleanup(session.stop)
        self.assertEqual(self.drain(session), [Reply("hi"), TurnEnd("done", 0), Exited(0)])
        self.assertEqual(self.logged()[0]["prompt"], "hello")

    def test_flags_and_env_reach_the_in_process_client(self):
        LAUNCHED.clear()
        spec = self.spec(flags=("--custom", "v"), env={"CUSTOM": "value"})
        _, found = self.run_in_process(client="checked", spec=spec)
        self.assertEqual(found, [Exited(0)])
        self.assertEqual((LAUNCHED[0].flags, dict(LAUNCHED[0].env)), (spec.flags, dict(spec.env)))

    def test_records_the_session_its_cwd_the_access_cwd_then_runs_it(self):
        cwd = os.path.join(self.dir, "cwd")
        os.mkdir(cwd)
        for project in (False, True):
            with self.subTest(project=project):
                self.workdir = os.path.join(self.dir, f"work-{project}")
                session, found = self.run_in_process(
                    spec=self.spec(access=Access(cwd=cwd, project=project)), sid=SID
                )
                self.assertEqual(found, [Exited(0)])
                self.assertEqual(
                    (session.sid, session.client, session.mode, session.name),
                    (SID, "fake", "in_process", None),
                )
                info = SessionInfo(SID, cwd, project, "", "")
                self.assertEqual(session.info(), info)
                (line,) = self.written()
                self.assertEqual(
                    {k: v for k, v in line.items() if k != "ts"},
                    {
                        "kind": "session",
                        "sid": SID,
                        "cwd": info.cwd,
                        "project": project,
                        "transcript": "",
                        "resume": "",
                    },
                )

    def test_runs_in_this_process_on_another_thread_with_no_child_process(self):
        self.steps({"out": {"type": "message", "text": "hi"}})
        _, found = self.run_in_process()
        self.assertEqual(found, [Reply("hi"), Exited(0)])
        (entry,) = self.logged()
        self.assertEqual(entry["pid"], os.getpid())
        self.assertNotEqual(entry["thread"], threading.get_ident())

    def test_its_events_then_exited_recording_client_session_and_stderr_a_yielded_exited_dropped(
        self,
    ):
        self.steps(
            {"out": {"type": "session", "id": "n1"}},
            {"out": {"type": "message", "text": "hi"}},
            {"err": "warn"},
            {"out": {"type": "exit"}},
            {"out": {"type": "result", "text": "done"}},
        )
        session, found = self.run_in_process()
        self.assertEqual(found, [Reply("hi"), Stderr("warn"), TurnEnd("done", 0), Exited(0)])
        self.assertEqual(
            [(e["kind"], e.get("client_id"), e.get("text")) for e in self.written()],
            [("session", None, None), ("client_session", "n1", None), ("stderr", None, "warn")],
        )
        self.assertEqual(session.reply, "done")

    def test_the_client_note_follows_the_prompt_only_with_commands(self):
        for commands, prompt in (((), "hello"), (("git",), "hello\n\nFAKE NOTE")):
            with self.subTest(commands=commands):
                self.run_in_process(
                    spec=self.spec(access=Access(cwd=self.workdir, commands=commands))
                )
                self.assertEqual(self.logged()[-1]["prompt"], prompt)

    def test_resume_continues_that_session(self):
        session, _ = self.run_in_process(sid=SID, resume=True)
        (entry,) = self.logged()
        self.assertEqual((session.sid, entry["sid"], entry["resume"]), (SID, SID, True))
        self.assertEqual(self.written()[0]["sid"], SID)

    def test_the_in_process_fake_supports_no_other_mode(self):
        for mode in ("headless", "tui"):
            with self.subTest(mode=mode):
                self.refused(
                    ConfigError,
                    f"client 'fake' does not support {mode} mode",
                    lambda *, mode=mode: self.start(mode=mode),
                )

    def test_claude_and_codex_do_not_support_it(self):
        self.configure_session(
            {
                "claude": {"implementation": "ensemble.session.clients.claude:ClaudeClient"},
                "codex": {"implementation": "ensemble.session.clients.codex:CodexClient"},
            }
        )
        for client in ("claude", "codex"):
            with self.subTest(client=client):
                self.refused(
                    ConfigError,
                    f"client {client!r} does not support in_process mode",
                    lambda *, client=client: self.start(client=client, mode="in_process"),
                )

    def test_mode_is_validated_before_codex_launch_configuration(self):
        self.configure_session(
            {"codex": {"implementation": "ensemble.session.clients.codex:CodexClient"}}
        )
        spec = self.spec(client="codex", mode=Mode.IN_PROCESS, flags=("--sandbox", "invalid"))
        for call in (self.session.preflight, self.session.start):
            with (
                self.subTest(call=call.__name__),
                self.assertRaisesRegex(ConfigError, "does not support in_process mode"),
            ):
                call(spec)
        self.assertFalse(os.path.exists(self.workdir))

    def test_a_run_idle_past_idle_timeout_yields_idle_timeout_and_goes_on(self):
        self.steps(
            {"out": {"type": "message", "text": "a"}},
            {"sleep": 0.5},
            {"out": {"type": "message", "text": "b"}},
        )
        _, found = self.run_in_process(idle_timeout=0.1)
        self.assertEqual(found[0], Reply("a"))
        self.assertEqual(set(found[1:-2]), {IdleTimeout(0.1)})
        self.assertEqual(found[-2:], [Reply("b"), Exited(0)])


class Clock:
    """The session's clock and time.monotonic's stand-in: `now` moves only when a StubMode
    waits.
    """

    now = 0.0

    def monotonic(self) -> float:
        return self.now


class StubMode:
    """A BaseMode on a Clock: poll(timeout) gives the next script item (at, events, ended) once the
    clock reaches `at`, else ([], False), moving the clock to `at` or on by timeout, whichever is
    sooner; past LATE s it raises, so a session that never ends fails. returncode() gives `code`;
    every call is logged in `calls`.
    """

    LATE = 60

    def __init__(self, clock, script, code):
        self.clock, self.script, self.code, self.calls = clock, list(script), code, []
        self.failure = AssertionError("polled past the script")

    def poll(self, timeout):
        self.calls.append(("poll", timeout))
        if self.clock.now > self.LATE:
            raise self.failure
        if self.script and self.script[0][0] <= self.clock.now + timeout:
            at, events, ended = self.script.pop(0)
            self.clock.now = max(self.clock.now, at)
            return list(events), ended
        self.clock.now += timeout
        return [], False

    def returncode(self, timeout):
        self.calls.append(("returncode", timeout))
        return self.code

    def state(self):
        self.calls.append(("state",))
        return "unknown"

    def client_alive(self):
        self.calls.append(("client_alive",))
        return False

    def send(self, text):
        self.calls.append(("send", text))

    def screen(self, lines=None):
        self.calls.append(("screen", lines))
        return "pane"

    def stop(self):
        self.calls.append(("stop",))


class EventsOverAModeTest(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.clock = Clock()
        self.configured = HostedSession.configured(
            clients={}, hosts=parsed(HOSTS, "tmux"), clock=self.clock.monotonic
        )

    def session(self, *script, code=0, idle_timeout=None, **kwargs) -> HostedSession:
        self.mode = StubMode(self.clock, script, code)
        return self.configured(
            "fake",
            kwargs.pop("mode", Mode.TUI),
            Spec(
                workdir=self.dir,
                access=Access(cwd=self.dir),
                sid="s1",
                name=kwargs.pop("name", None),
            ),
            info=SessionInfo(
                sid="s1",
                cwd=self.dir,
                project=False,
                transcript="",
                resume="",
            ),
            idle_timeout=idle_timeout,
            running=self.mode,
            host=self.configured.host(),
            **{"client": fake_client.FakeClient({}), **kwargs},
        )

    def events(self, session) -> list:
        found = list(itertools.islice(session.events(), 50))
        self.assertLess(len(found), 50, found)
        return found

    def test_opening_pending_is_its_modes(self):
        session = self.session()
        for pending in ("/color", None):
            with self.subTest(pending=pending):
                self.mode.opening_pending = pending
                self.assertIsInstance(session, worker._Opens)
                self.assertIs(session.opening_pending, pending)

    def test_its_dialog_folds_the_events_it_yielded_then_those_it_queued(self):
        session = self.session(
            (0, [Reply("r"), Blocked("ask"), Stderr("x")], False),
            (1, [Activity("")], False),
            (2, [], True),
        )
        self.assertIsInstance(session, worker._Dialogs)
        events = session.events()
        self.assertIsNone(session.dialog)
        self.assertEqual(next(events), Reply("r"))
        self.assertEqual(session.dialog, "ask")  # its Blocked still queued
        self.assertEqual([next(events), next(events)], [Blocked("ask"), Stderr("x")])
        self.assertEqual(session.dialog, "ask")
        self.assertEqual(next(events), Activity(""))
        self.assertIsNone(session.dialog)

    def test_binding_and_late_metadata_preserve_completion(self):
        client = fake_client.FakeClient({})
        resolver = mock.Mock(side_effect=lambda info: dataclasses.replace(info, resume="resume n1"))
        client.session_metadata = resolver
        session = self.session((0, [ClientBound("n1")], True), client=client)
        RunLog(self.dir).note("session", sid="s1", cwd=self.dir, project=False)
        self.assertEqual(self.events(session), [Exited(0)])
        self.assertEqual(session.info().resume, "resume n1")
        self.assertEqual(RunLog(self.dir).session("s1").resume, "resume n1")
        RunLog(self.dir).note("result", outcome={"status": "done"})
        resolver.side_effect = lambda info: dataclasses.replace(info, transcript="/late")
        self.assertEqual(session.info().transcript, "/late")
        self.assertEqual(RunLog(self.dir).session("s1").transcript, "/late")
        self.assertEqual(sum(e["kind"] == "session" for e in RunLog(self.dir).read()), 1)
        previous = session.info()
        resolver.side_effect = OSError("metadata unavailable")
        self.assertEqual(session.info(), previous)

    def test_late_metadata_keeps_a_hosts_resume_command_in_a_named_tui_or_headless_run(self):
        tmp = self.dir
        for mode in (Mode.TUI, Mode.HEADLESS):
            with self.subTest(mode=mode):
                self.dir = os.path.join(tmp, mode)
                os.mkdir(self.dir)
                client = fake_client.FakeClient({})
                client.session_metadata = lambda info: dataclasses.replace(info, resume="resume n1")
                session = self.session(
                    (0, [ClientBound("n1")], True), client=client, name="w1", mode=mode
                )
                RunLog(self.dir).note("session", sid="s1", cwd=self.dir, project=False)
                argv = ("ensemble-session", "resume", "w1")
                wanted = f"cd {shlex.quote(self.dir)} && ensemble-session resume w1"
                with mock.patch.object(session._host, "_resume_argv", return_value=argv):
                    self.assertEqual(self.events(session), [Exited(0)])
                    self.assertEqual(session.info().resume, wanted)
                self.assertEqual(RunLog(self.dir).session("s1").resume, wanted)

    def test_binding_record_failure_remains_fatal(self):
        client = fake_client.FakeClient({})
        client.session_metadata = mock.Mock(side_effect=lambda info: info)
        session = self.session((0, [ClientBound("n1")], True), client=client)
        with (
            mock.patch("os.open", side_effect=OSError("binding failed")),
            self.assertRaisesRegex(OSError, "binding failed"),
        ):
            self.events(session)
        self.assertIn(("stop",), self.mode.calls)
        session.info()
        client.session_metadata.assert_not_called()

    def test_a_client_binding_is_one_client_session_line_each_never_an_event(self):
        session = self.session(
            (0, [ClientBound("n1"), Activity("a")], False),
            (1, [ClientBound("n2")], False),
            (2, [TurnEnd("done")], True),
        )
        self.assertEqual(self.events(session), [Activity("a"), TurnEnd("done"), Exited(0)])
        lines = RunLog(self.dir).read()
        self.assertEqual(
            [(e["kind"], e.get("sid"), e.get("client_id")) for e in lines],
            [("client_session", "s1", "n1"), ("client_session", "s1", "n2")],
        )
        self.assertEqual([list(e) for e in lines], [["ts", "kind", "sid", "client_id"]] * 2)

    def test_metadata_resolver_cannot_replace_session_identity_or_effective_project(self):
        client = fake_client.FakeClient({})
        client.session_metadata = lambda info: dataclasses.replace(
            info, sid="other", cwd="/other", project=True, transcript="/t"
        )
        session = self.session((0, [ClientBound("n1")], True), client=client)
        self.events(session)
        self.assertEqual(
            session.info(),
            SessionInfo(sid="s1", cwd=self.dir, project=False, transcript="/t", resume=""),
        )

    def test_reply_is_the_latest_reply_or_non_empty_turn_end_text(self):
        for events, reply in (
            ([], None),
            ([TurnEnd("")], None),
            ([Reply("hi"), TurnEnd("")], "hi"),
            ([TurnEnd("r1"), TurnEnd(""), Activity("")], "r1"),
            ([Reply("hi"), TurnEnd("done")], "done"),
        ):
            with self.subTest(events=events):
                session = self.session((0, events, True))
                self.events(session)
                self.assertEqual(session.reply, reply)

    def test_events_in_order_the_modes_exited_dropped_exited_last_in_poll_slices(self):
        session = self.session(
            (0, [Activity("a"), Exited(9)], False),
            (1, [TurnEnd("done")], False),
            (2, [Stderr("e")], True),
            code=3,
        )
        self.assertEqual(
            self.events(session),
            [Activity("a"), TurnEnd("done"), Stderr("e"), Exited(3)],
        )
        self.assertEqual(self.events(session), [])
        self.assertEqual(session.reply, "done")
        self.assertEqual({call[1] for call in self.mode.calls if call[0] == "poll"}, {0.5})
        self.assertIn(("returncode", 0.5), self.mode.calls)

    def test_each_idle_span_yields_idle_timeout_but_none_while_blocked(self):
        for case, script, idle_timeout, expected in (
            (
                "idle spans",
                ((0, [Activity()], False), (5, [Reply("r")], False), (5.5, [], True)),
                2,
                [Activity(), IdleTimeout(2), IdleTimeout(2), Reply("r"), Exited(0)],
            ),
            (
                "stderr keeps a block",
                (
                    (0, [Blocked("ask")], False),
                    (0.5, [Stderr("x")], False),
                    (3, [Activity()], False),
                    (4.5, [], True),
                ),
                1,
                [Blocked("ask"), Stderr("x"), Activity(), IdleTimeout(1), Exited(0)],
            ),
            (
                "a reply keeps a block",
                (
                    (0, [Blocked("ask")], False),
                    (0.5, [Reply("r")], False),
                    (3, [Activity()], False),
                    (4.5, [], True),
                ),
                1,
                [Blocked("ask"), Reply("r"), Activity(), IdleTimeout(1), Exited(0)],
            ),
            (
                "a failed turn ends a block",
                (
                    (0, [Blocked("ask")], False),
                    (0.5, [TurnFailed("overloaded")], False),
                    (2, [], True),
                ),
                1,
                [Blocked("ask"), TurnFailed("overloaded"), IdleTimeout(1), Exited(0)],
            ),
            (
                "a client binding keeps a block",
                (
                    (0, [Blocked("ask")], False),
                    (1, [ClientBound("n1")], False),
                    (2, [Activity("a")], False),
                    (3, [], True),
                ),
                1.5,
                [Blocked("ask"), Activity("a"), Exited(0)],
            ),
        ):
            with self.subTest(case=case):
                self.clock.now = 0.0
                session = self.session(*script, idle_timeout=idle_timeout)
                self.assertEqual(self.events(session), expected)

    def test_an_exception_from_the_mode_stops_it_then_propagates(self):
        session = self.session()
        with (
            mock.patch.object(self.mode, "poll", side_effect=RuntimeError("boom")),
            self.assertRaisesRegex(RuntimeError, "boom"),
        ):
            self.events(session)
        self.assertEqual(self.mode.calls, [("stop",)])
        self.assertEqual(self.events(session), [])

    def test_a_failed_stop_on_an_exception_from_the_mode_notes_it_on_that_exception(self):
        session = self.session()
        with (
            mock.patch.object(self.mode, "poll", side_effect=RuntimeError("boom")),
            mock.patch.object(self.mode, "stop", side_effect=SessionError("stop failed")),
            self.assertRaisesRegex(RuntimeError, "boom") as raised,
        ):
            self.events(session)
        self.assertEqual(raised.exception.__notes__, ["stop: stop failed"])

    def test_alive_send_and_stop_are_the_modes_and_name_defaults_to_none(self):
        session = self.session()
        self.assertIsNone(session.name)
        self.assertEqual((session.alive(), session.status().state), (False, "unknown"))
        self.assertFalse(session.client_alive())
        session.send("hi")
        session.stop()
        self.assertEqual(
            [call for call in self.mode.calls if call != ("state",)],
            [("client_alive",), ("send", "hi"), ("stop",)],
        )
        self.assertEqual(self.session(name="w1").name, "w1")


class UsageTest(Base):
    def test_a_configured_non_executable_program_is_a_configuration_error(self):
        program = os.path.join(self.dir, "not-executable")
        with open(program, "w", encoding="utf-8") as file:
            file.write("data")
        os.chmod(program, 0o600)
        session = HostedSession.configured(
            clients={
                "opencode": {
                    "implementation": "ensemble.session.clients.opencode:OpenCodeClient",
                    "model": "ollama/local",
                    "executable": program,
                }
            },
            hosts=parsed(FAKE_HOSTS, "fake"),
        )
        with self.assertRaisesRegex(ConfigError, "missing or not executable"):
            session.usage("opencode")

    def test_missing_builtin_executables_raise_configuration_errors_before_probing(self):
        probe = mock.Mock(return_value=None)
        session = HostedSession.configured(
            clients={
                "claude": {"implementation": "ensemble.session.clients.claude:ClaudeClient"},
                "codex": {"implementation": "ensemble.session.clients.codex:CodexClient"},
                "opencode": {
                    "implementation": "ensemble.session.clients.opencode:OpenCodeClient",
                    "model": "ollama/local",
                },
            },
            hosts=parsed(FAKE_HOSTS, "fake"),
            probe=probe,
        )
        for name in ("claude", "codex", "opencode"):
            with (
                self.subTest(name=name),
                mock.patch("shutil.which", return_value=None),
                self.assertRaisesRegex(ConfigError, "missing or not executable"),
            ):
                session.usage(name)
        probe.assert_not_called()
        with mock.patch("shutil.which", return_value="/bin/client"):
            self.assertIsNone(session.usage("claude"))
        self.assertEqual(probe.call_count, 1)

    def test_default_provider_reaches_the_container_host_without_a_host_probe(self):
        class ProviderHost(ScreenHost):
            def _client_context(self):
                return dataclasses.replace(
                    super()._client_context(),
                    container=engine_types.Container("/bin", "/python", "/source", "/state"),
                )

            def _provider_usage(self, request):
                self.request = request
                return self.available

        host = ProviderHost("tmux")
        probe = mock.Mock(return_value='{"models": []}')
        self.session = HostedSession.configured(
            clients={
                "opencode": {
                    "implementation": "ensemble.session.clients.opencode:OpenCodeClient",
                    "model": "strata/remote",
                    "tiers": {"1": "ollama/local"},
                    "providers": {"strata": {"base_url": "https://strata.test/v1", "models": []}},
                }
            },
            hosts=parsed(FAKE_HOSTS, "fake"),
            probe=probe,
            host_factory=lambda *_args: host,
        )
        for available in (True, False):
            host.available = available
            self.assertEqual(
                self.session.usage("opencode"),
                Usage(None, None) if available else None,
            )
            self.assertEqual(
                (host.request.provider, host.request.base_url), ("strata", "https://strata.test/v1")
            )
        probe.assert_not_called()

    def test_default_ollama_ignores_a_remote_tier_and_needs_no_host_opencode(self):
        class ContainerHost(ScreenHost):
            def _client_context(self):
                return dataclasses.replace(
                    super()._client_context(),
                    container=engine_types.Container("/bin", "/python", "/source", "/state"),
                )

            def _provider_usage(self, request):
                raise AssertionError("the default Ollama model needs no provider request")

        probe = mock.Mock(return_value='{"models": []}')
        self.session = HostedSession.configured(
            clients={
                "opencode": {
                    "implementation": "ensemble.session.clients.opencode:OpenCodeClient",
                    "model": "ollama/local",
                    "tiers": {"1": "strata/remote"},
                    "providers": {"strata": {"base_url": "https://strata.test/v1", "models": []}},
                }
            },
            hosts=parsed(FAKE_HOSTS, "fake"),
            probe=probe,
            host_factory=lambda *_args: ContainerHost("tmux"),
        )
        with mock.patch("shutil.which", return_value=None) as executable:
            self.assertEqual(self.session.usage("opencode"), Usage(None, None))
        self.assertNotIn("opencode", [call.args[0] for call in executable.call_args_list])
        self.assertEqual(probe.call_count, 1)

    def probing(self, out="42.5", **launch) -> mock.Mock:
        """Client probe configured, its probe the Launch of `launch` (argv ("probe-cmd", "-x")
        unless given), the Setup's probe a mock giving `out`.
        """
        probe = mock.Mock(return_value=out)
        self.session_dependencies = {**self.session_dependencies, "probe": probe}
        self.configure("probe", probe={"argv": ("probe-cmd", "-x"), **launch})
        return probe

    def usage(self, out="42.5", **launch):
        probe = self.probing(out, **launch)
        return self.session.usage("probe"), probe

    def test_no_probe_is_none_and_runs_nothing(self):
        probe = self.probing()
        self.assertIsNone(self.session.usage("fake"))
        probe.assert_not_called()
        self.assertFalse(os.path.exists(self.probe_dir))

    def test_the_probes_stdout_as_the_client_parses_it(self):
        found, probe = self.usage("42.5")
        self.assertEqual(found, Usage(Window(42.5, None), None))
        probe.assert_called_once_with(
            ("probe-cmd", "-x"),
            cwd=self.probe_dir,
            env=mock.ANY,
            timeout=120,
        )

    def test_a_spawn_error_is_none(self):
        self.configure("probe", probe={"argv": ("probe-cmd",)})
        with mock.patch("subprocess.Popen", side_effect=OSError("no exec")) as popen:
            self.assertIsNone(self.session.usage("probe"))
        popen.assert_called_once()

    def test_a_timeout_is_none(self):
        found, probe = self.usage(None)
        self.assertIsNone(found)
        self.assertEqual(probe.call_args.kwargs["timeout"], 120)

    def test_a_reading_without_windows_is_a_usage(self):
        with mock.patch.object(ProbeClient, "usage", return_value=engine_types.Usage(None, None)):
            found, probe = self.usage()
        self.assertEqual(found, Usage(None, None))
        probe.assert_called_once()

    def test_no_reading_is_none(self):
        with mock.patch.object(ProbeClient, "usage", return_value=None):
            self.assertIsNone(self.usage()[0])

    def test_an_exception_from_the_clients_parse_is_none(self):
        for out in ("", "garbage"):
            with self.subTest(out=out):
                self.assertIsNone(self.usage(out)[0])

    def test_usage_parse_failure_keeps_its_fallback_with_logging_disabled(self):
        disabled = logging.root.manager.disable
        try:
            logging.disable(logging.CRITICAL)
            self.assertIsNone(self.usage("garbage")[0])
        finally:
            logging.disable(disabled)

    def test_a_probe_without_a_command_is_none(self):
        found, probe = self.usage(argv=())
        self.assertIsNone(found)
        probe.assert_not_called()

    def test_the_probe_dir_is_made_0700_under_home(self):
        self.usage()
        mode = os.lstat(self.probe_dir).st_mode
        self.assertTrue(stat.S_ISDIR(mode))
        self.assertEqual(stat.S_IMODE(mode), 0o700)
        found, probe = self.usage()
        self.assertEqual(found, Usage(Window(42.5, None), None))
        self.assertEqual(probe.call_args.kwargs["cwd"], self.probe_dir)

    def test_a_file_at_the_probe_dir_is_none_and_runs_nothing(self):
        open(self.probe_dir, "w").close()
        found, probe = self.usage()
        self.assertIsNone(found)
        probe.assert_not_called()

    def test_the_probe_gets_the_child_environment_in_launch_cwd(self):
        cwd = os.path.join(self.dir, "cwd")
        os.mkdir(cwd)
        with mock.patch.dict(os.environ, {"API_DROP": "x", "API_KEEP": "k"}):
            _, probe = self.usage(env={"API_ADD": "a"}, unset=("API_DROP",), cwd=cwd)
        env = probe.call_args.kwargs["env"]
        self.assertEqual(
            (env.get("API_DROP"), env["API_KEEP"], env["API_ADD"], env["PWD"]),
            (None, "k", "a", cwd),
        )
        self.assertEqual(probe.call_args.kwargs["cwd"], cwd)
        self.assertFalse(os.path.exists(self.probe_dir))

    def test_a_registry_error_raises(self):
        with self.assertRaises(ConfigError):
            self.session.usage("nope")


class WritablePathsTest(HostedTuiBase):
    def test_cwd_and_grants_over_the_state_root_and_hook_paths_start_in_every_mode(self):
        for mode, resume in itertools.product(Mode, (False, True)):
            self.workdir = os.path.join(self.dir, f"state-{mode}-{resume}")
            spec = self.spec(
                access=Access(cwd=self.dir, dirs=(self.workdir, self.ensemble)),
                events=os.path.join(self.dir, "events") if mode == Mode.TUI else None,
                sid=SID,
            )
            with (
                self.subTest(mode=mode, resume=resume),
                mock.patch(
                    "subprocess.Popen",
                    return_value=mock.Mock(stdout=io.StringIO(), stderr=io.StringIO()),
                ),
            ):
                session = self.start(
                    client="checked" if mode == Mode.IN_PROCESS else "fake",
                    mode=mode,
                    spec=spec,
                    resume=resume,
                )
            self.assertEqual(session.info().cwd, self.dir)
            session.stop()


class AccountPoolTest(Base):
    """A client with two or more accounts on a Host running containers: Session.usage is the best
    account's reading, and a start selecting none runs on it.
    """

    def setUp(self):
        super().setUp()
        self.outputs = {"/d": "70,10", "/w": "20,30", "/x": "-1,-1"}
        self.probe = mock.Mock(side_effect=lambda argv, **_: self.outputs[argv[-1]])
        self.session_dependencies = {**self.session_dependencies, "probe": self.probe}
        self.pool({"default": "/d", "work": "/w"})
        self.cache = os.path.join(self.ensemble, "usage", "pool.json")

    def pool(self, accounts, *, container=True):
        settings = {"implementation": CONTAINER_HOST} if container else None
        self.configure("pool", settings, probe={"argv": ("probe-cmd", "/x")}, accounts=accounts)

    def started_account(self, **env):
        """The account a headless start with `env` hands its (recording) container Host."""
        self.configure(
            "pool",
            {"implementation": HOMED_HOST},
            probe={"argv": ("probe-cmd", "/x")},
            accounts={"default": "/d", "work": "/w"},
        )
        spec = self.spec(client="pool", mode=Mode.HEADLESS, name="w1", env=env)
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaisesRegex(SessionError, "refused by the test Host"),
        ):
            self.session.start(spec)
        (metadata,) = self.session.host().piped
        return metadata.get("home_account")

    def test_a_start_selecting_none_hands_the_host_the_best_account(self):
        self.assertEqual(self.started_account(), "work")

    def test_a_selected_account_reaches_the_host_unprobed(self):
        self.assertEqual(self.started_account(**{ACCOUNT_ENV: "default"}), "default")
        self.probe.assert_not_called()

    def usage(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            found = self.session.usage("pool")
        self.err = err.getvalue()
        return found

    def started_env(self, **env):
        """The env a start of a spec with `env` hands its mode."""
        setup = self.session._config()
        spec = self.spec(client="pool", mode=Mode.HEADLESS, env=env)
        client = setup.registry.client("pool")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            picked = hosted_session._picked(setup, spec, "pool", client, self.session.host())
        self.err = err.getvalue()
        return dict(picked.env)

    def probed(self):
        return sorted(call.args[0][-1] for call in self.probe.call_args_list)

    def test_usage_is_the_reading_of_the_account_with_most_left(self):
        self.assertEqual(self.usage(), Usage(Window(20, None), Window(30, None)))
        self.assertEqual(self.probed(), ["/d", "/w"])
        self.assertTrue(os.path.isfile(self.cache))

    def test_an_account_over_a_threshold_is_passed_over(self):
        self.outputs = {"/d": "92,5", "/w": "50,60"}
        self.assertEqual(self.usage(), Usage(Window(50, None), Window(60, None)))

    def test_every_account_over_is_an_exhausted_reading(self):
        self.outputs = {"/d": "95,10", "/w": "10,100"}
        self.assertTrue(exhausted(self.usage()))

    def test_no_reading_known_is_none(self):
        self.probe.side_effect = None
        self.probe.return_value = None
        self.assertIsNone(self.usage())

    def test_a_second_reading_within_the_ttl_probes_nothing(self):
        first = self.usage()
        self.probe.reset_mock()
        self.assertEqual(self.usage(), first)
        self.probe.assert_not_called()

    def test_a_start_selecting_none_runs_on_the_best_account(self):
        self.assertEqual(self.started_env()[ACCOUNT_ENV], "work")
        self.assertIn("pool account work: picked (five_hour=20% weekly=30%)", self.err)
        self.probe.reset_mock()
        self.assertEqual(self.started_env()[ACCOUNT_ENV], "work")
        self.probe.assert_not_called()

    def test_a_selected_account_is_kept_and_nothing_probed(self):
        self.assertEqual(self.started_env(**{ACCOUNT_ENV: "default"})[ACCOUNT_ENV], "default")
        self.probe.assert_not_called()

    def test_with_no_reading_known_a_start_selects_none(self):
        self.probe.side_effect = None
        self.probe.return_value = None
        self.assertNotIn(ACCOUNT_ENV, self.started_env())

    def test_one_account_or_no_container_is_todays_single_probe(self):
        for accounts, container in (
            ({"default": "/d"}, True),
            ({"default": "/d", "work": "/w"}, False),
        ):
            with self.subTest(accounts=accounts, container=container):
                self.pool(accounts, container=container)
                self.probe.reset_mock()
                self.assertEqual(self.usage(), Usage(Window(-1, None), Window(-1, None)))
                self.assertEqual(self.probed(), ["/x"])
                self.assertNotIn(ACCOUNT_ENV, self.started_env())
                self.assertFalse(os.path.exists(self.cache))


class WritableDirsTest(Base):
    def dirs(self, **kwargs):
        return mock.patch.object(ProbeClient, "writable_dirs", autospec=True, **kwargs)

    def test_the_fake_client_claims_none(self):
        self.assertEqual(self.session.writable_dirs(self.spec(client="fake")), ())

    def test_the_clients_dirs_as_a_tuple_for_the_spec_as_given(self):
        spec = self.spec(access=Access(dirs=(self.dir,), cwd=self.dir), tier=2, client="probe")
        given = (
            spec,
            dataclasses.replace(spec, sid="r1", resume=True),
            dataclasses.replace(spec, sid="s1"),
        )
        with self.dirs(return_value=[self.dir, self.workdir]) as dirs:
            for each in given:
                self.assertEqual(self.session.writable_dirs(each), (self.dir, self.workdir))
        self.assertEqual(
            [(c.args[1:], c.kwargs) for c in dirs.call_args_list], [((each,), {}) for each in given]
        )

    def test_refuses_a_spec_without_client_or_an_unknown_client(self):
        for spec, message in (
            (self.spec(), "spec.client: required"),
            (self.spec(client="nope"), "no [clients.nope] table in the configured clients"),
        ):
            with (
                self.subTest(message=message),
                self.dirs() as dirs,
                self.assertRaises(ConfigError) as caught,
            ):
                self.session.writable_dirs(spec)
            self.assertEqual(str(caught.exception), message)
            dirs.assert_not_called()

    def test_an_os_error_from_the_client_is_a_config_error(self):
        with (
            self.dirs(side_effect=PermissionError(13, "denied")),
            self.assertRaises(ConfigError) as caught,
        ):
            self.session.writable_dirs(self.spec(client="probe"))
        self.assertEqual(str(caught.exception), "probe's writable dirs: [Errno 13] denied")
        self.assertIsInstance(caught.exception.__cause__, PermissionError)


class ListTest(HostedTuiBase):
    def test_a_row_per_listed_session_in_its_order_shown_where_the_viewer_placed_it(self):
        self.session.viewer()._tmux.listing.side_effect = lambda _match=None, *, views=False: (
            [] if views else [{"name": "w2", "shown": "shown"}]
        )
        first = self.host.create("w2", SID)
        second = self.host.create("w1", "other")
        first.calls.row.return_value = SessionRow(
            "w2", SID, "fake", StatusState.DONE, None, None, "bye", own=False
        )
        second.calls.row.return_value = SessionRow(
            "w1",
            "other",
            "unknown-client",
            StatusState.WORKING,
            None,
            None,
            None,
            own=False,
        )
        self.assertEqual(
            self.session.list(), [second.row(), dataclasses.replace(first.row(), shown="shown")]
        )
        self.host.calls.list.assert_called_once_with()

    def test_display_keeps_the_sessions_stored_with_it_and_empty_none(self):
        for name, display in (("w1", "mgr/below"), ("w2", "mgr"), ("w3", None)):
            self.host.create(
                name,
                receipt=Record(
                    1,
                    "fake",
                    name,
                    (name,),
                    None,
                    None,
                    ("ensemble-session",),
                    ("ensemble-session", "resume", name),
                    display,
                    None,
                ),
            )
        self.assertEqual([r.name for r in self.session.list(display="mgr/below")], ["w1", "w2"])
        with mock.patch.object(ScreenHost, "attachable", new=False):
            self.assertEqual([r.name for r in self.session.list(display="mgr/below")], ["w1"])
        self.assertEqual([r.name for r in self.session.list(display=None)], ["w1", "w2", "w3"])
        self.host.calls.reset_mock()
        self.assertEqual(self.session.list(display=""), [])
        self.assertEqual(self.host.calls.mock_calls, [])

    def test_match_is_compiled_for_the_listing_to_fullmatch(self):
        for name in ("w1", "w12", "xw1", "w1x"):
            self.host.create(name)
        self.assertEqual([r.name for r in self.session.list(r"w[0-9]+")], ["w1", "w12"])
        self.host.calls.list.assert_called_once_with()

    def test_no_server_is_no_rows_without_reading_tmux_pane(self):
        self.assertEqual(self.session.list(), [])
        self.assertEqual(self.host.calls.mock_calls, [mock.call.list()])

    def test_a_non_str_or_invalid_match_is_refused(self):
        for bad, message in (
            (5, "match must be a regular expression, got 5"),
            (b"w", "match must be a regular expression, got b'w'"),
            ("(", "match '(': missing ), unterminated subpattern at position 0"),
            ("[", "match '[': unterminated character set at position 0"),
        ):
            with self.subTest(bad=bad), self.assertRaises(ConfigError) as caught:
                self.session.list(bad)
            self.assertEqual(str(caught.exception), message)
        self.nothing_hosted()

    def test_tmux_failing_or_a_bad_tmux_pane_is_a_driver_error_never_no_rows(self):
        for message in ("host listing unavailable", "host display unavailable"):
            self.host.calls.list.side_effect = SessionError(message)
            with self.subTest(message=message), self.assertRaises(SessionError) as caught:
                self.session.list()
            self.assertEqual(str(caught.exception), message)

    def test_a_row_is_frozen_with_these_fields(self):
        self.assertEqual(
            [f.name for f in dataclasses.fields(SessionRow)],
            ["name", "sid", "client", "state", "display", "shown", "last_lines", "own"],
        )
        found = SessionRow(
            name="w1",
            sid=None,
            client=None,
            state="working",
            display=None,
            shown=None,
            last_lines=None,
            own=False,
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            found.state = "done"


class TuiStartTest(HostedTuiBase):
    def test_binds_the_client_session_selector_for_new_and_resumed_sessions(self):
        self.configure(
            tables={"claude": {"implementation": "ensemble.session.clients.claude:ClaudeClient"}}
        )
        for resumed, word in ((False, "--session-id"), (True, "--resume")):
            with self.subTest(resumed=resumed):
                self.tui(client="claude", name=f"w{resumed}", sid=SID, resume=resumed)
                argv = self.host.calls.terminal.call_args.args[1].argv
                self.assertEqual(tuple(argv[:3]), ("claude", word, SID))

    def test_effective_project_override_reaches_info_record_and_tui_state(self):
        original = fake_client.FakeTui.launch
        for project in (True, False):

            def launch(run, *args, project=project, **kwargs):
                return dataclasses.replace(original(run, *args, **kwargs), project=project)

            with (
                self.subTest(project=project),
                mock.patch.object(fake_client.FakeTui, "launch", launch),
            ):
                session = self.tui(
                    name=f"project-{project}",
                    spec=self.spec(access=Access(cwd=self.workdir, project=not project)),
                )
            self.assertIs(session.info().project, project)
            self.assertIs(RunLog(self.workdir).session(session.sid).project, project)
            self.assertIs(tui.TuiMode.snapshot(session.name)["project"], project)

    def test_prepares_the_session_dir_records_the_session_then_begins_it_tagged_with_no_pane(self):
        order = []
        self.host.calls.terminal.side_effect = lambda *_args, **_kwargs: order.append(
            [e["kind"] for e in self.written()]
        )
        session = self.tui(name="w1", sid=SID, events=self.events)
        self.assertEqual(order, [["session"]])
        self.assertEqual(
            (session.name, session.sid, session.client, session.mode), ("w1", SID, "fake", "tui")
        )
        self.assertEqual(
            session.info(),
            SessionInfo(sid=SID, cwd=self.workdir, project=False, transcript="", resume=""),
        )
        (call,) = self.host.calls.terminal.call_args_list
        self.assertEqual(call.args[0], "w1")
        hook = tui.TuiStore.attach("w1", host=self.host).hook_argv()
        launch = call.args[1]
        self.assertEqual(launch.argv[-(1 + len(hook)) :], ("hello", *hook))
        self.assertEqual(launch.cwd, self.workdir)
        self.assertEqual(call.kwargs["events"], self.events)
        self.assertIsNone(call.kwargs["display"])
        self.assertIsNone(call.kwargs["record"])
        self.assertIsNone(session.record())
        self.assertEqual(call.kwargs["metadata"]["sid"], SID)
        self.assertEqual(call.kwargs["metadata"]["client"], "fake")
        self.assertEqual(
            call.kwargs["metadata"]["generation"], tui.TuiMode.snapshot("w1")["generation"]
        )
        self.assertEqual(launch.env["PWD"], self.workdir)
        self.assertEqual(stat.S_IMODE(os.stat(self.events).st_mode), 0o600)
        self.assertEqual(
            {
                k: v
                for k, v in tui.TuiMode.snapshot("w1").items()
                if k in ("client", "sid", "events", "words", "host")
            },
            {
                "client": "fake",
                "sid": SID,
                "events": self.events,
                "words": {"stop": "done", "ask": "blocked", "tool": "working"},
                "host": "tmux",
            },
        )

    def test_content_prints_the_current_screen(self):
        session = self.tui(name="w1")
        self.host.calls.screen.return_value = "current pane\ntext"
        self.assertEqual(session.content(), "current pane\ntext")
        self.host.calls.screen.assert_called_once_with(self.host.processes["w1"], None)

    def test_alive_send_stop_and_screen_are_the_tui_modes_attach_command_its_hosts(self):
        session = self.tui(name="w1")
        terminal = self.host.processes["w1"]
        self.assertTrue(session.showable)
        self.host.calls.screen.return_value = "pane text"
        self.host.calls.attach_command.return_value = "terminal attach w1"
        self.assertEqual((session.alive(), session.screen(5)), (True, "pane text"))
        self.host.calls.screen.assert_called_once_with(terminal, 5)
        self.assertEqual(session.attach_command(), "terminal attach w1")
        session.send("hi")
        self.assertEqual(
            terminal.calls.send.call_args_list, [mock.call(Paste("hi")), mock.call(Key("Enter"))]
        )
        session.stop()
        terminal.calls.close.assert_called_once_with()
        self.assertFalse(os.path.lexists(os.path.join(self.root, "w1")))

    def test_a_tui_status_carries_its_cwd_then_is_gone_once_stopped(self):
        session = self.tui(name="w1")
        found = session.status()
        self.assertEqual((found.alive, found.cwd), (True, session.info().cwd))
        session.stop()
        self.assertEqual(session.status().state, "gone")

    def test_send_and_screen_refuse_a_dead_pane(self):
        session = self.tui(name="w1")
        terminal = self.host.processes["w1"]
        for code in (2, -15):
            terminal.calls.alive.return_value = False
            terminal.calls.returncode.return_value = code
            for call in (lambda: session.send("more"), session.screen):
                with self.subTest(code=code, call=call), self.assertRaises(SessionError) as caught:
                    call()
                self.assertEqual(str(caught.exception), "tui session w1 is not running")
        terminal.calls.send.assert_not_called()
        self.host.calls.screen.assert_not_called()

    def test_show_opens_it_at_the_display_given_else_its_stored_one(self):
        session = self.tui(name="w1", display="mgr/below")
        self.host.calls.show.return_value = None
        self.assertIsNone(session.show())
        self.assertIsNone(session.show(display="other/right/m1"))
        self.assertIsNone(session.show(command="open {{session}}"))
        with self.assertRaises(TypeError):
            session.show("other/right/m1")
        self.assertEqual(
            self.host.calls.show.call_args_list,
            [
                mock.call("w1", display="mgr/below", command=None),
                mock.call("w1", display="other/right/m1", command=None),
                mock.call("w1", display="mgr/below", command="open {{session}}"),
            ],
        )

    def test_hide_asks_the_viewer_and_status_reports_the_hidden_store(self):
        session = self.tui(name="w1", sid=SID)
        session.hide()
        self.host.calls.hide.assert_called_once_with("w1")
        sid = self.host.processes["w1"].session_identity()[0]
        self.assertIsNotNone(sid)
        for flag in (True, False):
            hidden_store.write("w1", sid, hidden=flag, live=None)
            self.assertEqual(session.status().hidden, flag)

    def test_its_record_is_its_hosts(self):
        session = self.tui(name="w1")
        terminal = self.host.processes["w1"]
        self.assertEqual(session.record(), terminal.record())
        terminal.calls.record.assert_called()

    def test_close_kills_it_by_name_and_removes_its_dir(self):
        session = self.tui(name="w1")
        terminal = self.host.processes["w1"]
        self.host.calls.reset_mock()
        session.close()
        terminal.calls.close.assert_called_once_with()
        self.assertEqual(
            self.host.calls.mock_calls,
            [mock.call.list(), mock.call.attach("w1"), mock.call.close_view("w1")],
        )
        self.assertFalse(os.path.lexists(os.path.join(self.root, "w1")))

    def test_a_start_with_display_none_is_hidden_after_any_leftover_view_closes(self):
        session = self.tui(name="w1", sid=SID, display="none")
        self.assertIn(mock.call.close_view("w1"), self.host.calls.mock_calls)
        self.assertTrue(session.status().hidden)
        self.assertFalse(self.tui(name="w2", sid="other").status().hidden)

    def test_a_bad_viewer_table_refuses_preflight_and_start_before_launch(self):
        refused = ConfigError("grid_retile: want one of all, open-close, off")
        self.session_dependencies["viewer_factory"] = mock.Mock(side_effect=refused)
        self.configure(tui=True)
        for call in (
            lambda: self.session.preflight(self.spec(client="fake", mode=Mode.TUI)),
            lambda: self.tui(name="w1"),
        ):
            with self.assertRaises(ConfigError) as caught:
                call()
            self.assertIs(caught.exception, refused)
        self.host.calls.terminal.assert_not_called()

    def test_a_failed_started_mark_closes_the_session_it_started(self):
        store = os.path.join(self.ensemble, "hidden")
        os.makedirs(store, 0o500)
        self.addCleanup(os.chmod, store, 0o700)
        with self.assertRaisesRegex(SessionError, "^tui session w1: hidden state: "):
            self.tui(name="w1", sid=SID, display="none")
        self.host.calls.terminal.assert_called_once()
        self.assertNotIn("w1", self.host.processes)

    def test_a_session_keeps_its_client_and_tmux_when_its_class_is_configured_again(self):
        self.configure("probe", tui=True)
        session, other = self.tui(client="probe", name="w1"), ScreenHost("other")
        terminal = self.host.processes["w1"]
        self.session.configure(
            clients={},
            hosts=parsed(HOSTS, "tmux"),
            host_factory=lambda _kind, _name, _settings: other,
        )
        self.host.calls.reset_mock()
        self.assertEqual(
            session.session_status(),
            SessionStatus("yes", 1.5, f"probe {session.sid} {self.workdir}"),
        )
        session.close()
        terminal.calls.close.assert_called_once_with()
        self.assertFalse(os.path.lexists(os.path.join(self.root, "w1")))
        with self.assertRaisesRegex(ConfigError, r"\[clients\.probe\]"):
            self.tui(client="probe", name="w2")
        self.assertEqual(other.calls.mock_calls, [])

    def test_a_hosts_resume_command_replaces_the_clients_in_its_status(self):
        self.configure("probe", tui=True)
        session = self.tui(client="probe", name="w1")
        argv = ("ensemble-session", "resume", "w1")
        with mock.patch.object(self.host, "_resume_argv", return_value=argv):
            found = session.session_status()
        wanted = f"cd {shlex.quote(session.info().cwd)} && ensemble-session resume w1"
        self.assertEqual(found, SessionStatus("yes", 1.5, wanted))

    def test_a_started_sessions_status_gives_the_client_its_launch_env(self):
        self.configure("probe", tui=True)
        spec = self.spec(access=Access(cwd=self.workdir, env={"A": "access"}), env={"B": "spec"})
        session = self.tui(client="probe", name="w1", spec=spec)
        with mock.patch.object(
            ProbeClient,
            "session_status",
            autospec=True,
            return_value=SessionStatus("no", None, ""),
        ) as status:
            session.session_status()
        self.assertEqual(status.call_args.kwargs["env"], {"A": "access", "B": "spec"})


class SessionEnvTest(HostedEnvBase):
    def test_a_tui_session_gets_it(self):
        self.tui(client="env", name="w1", spec=self.spec(access=self.access))
        (call,) = self.host.calls.terminal.call_args_list
        self.assertEqual(call.args[1].env["PYTHONPATH"], PYTHONPATH)


class AttachTest(HostedTuiBase):
    def started(self, *hooked, start=True) -> None:
        """Tui session w1 started (sid SID; start False: already) on pane %1, then a hooks.jsonl
        record per (label, payload) in `hooked`, its @sid SID; the host's calls cleared.
        """
        if start:
            self.tui(name="w1", sid=SID, events=self.events)
        with open(os.path.join(self.root, "w1", "hooks.jsonl"), "w") as f:
            f.writelines(
                json.dumps({"ts": "", "label": label, "payload": payload}) + "\n"
                for label, payload in hooked
            )
        self.host.calls.reset_mock()

    def test_a_session_with_its_reply_and_next_records_events(self):
        self.started(("tool", {"session_id": "n1"}), ("stop", {"last_message": "r1"}))
        session = self.session.attach("w1", idle_timeout=2)
        self.assertIsInstance(session, self.session)
        self.assertEqual(
            (session.name, session.sid, session.client, session.mode, session.reply),
            ("w1", SID, "fake", "tui", "r1"),
        )
        self.assertEqual(
            session.info(),
            SessionInfo(sid=SID, cwd=self.workdir, project=False, transcript="", resume=""),
        )
        self.assertEqual(
            self.host.calls.mock_calls,
            [mock.call.list(), mock.call.attach("w1")],
        )
        with open(os.path.join(self.root, "w1", "hooks.jsonl"), "a") as f:
            f.write(
                json.dumps({"ts": "", "label": "stop", "payload": {"last_message": "r2"}}) + "\n"
            )
        self.assertEqual(next(session.events()), TurnEnd("r2", 0))
        self.assertEqual(session.reply, "r2")

    def test_its_reply_is_its_last_turn_ends_even_empty_its_blocked_as_live(self):
        for hooked, reply, blocked in (
            ((), None, False),
            ((("stop", {"last_message": "r1"}), ("stop", {}), ("tool", {})), "", False),
            ((("stop", {}),), "", False),
            ((("tool", {}), ("ask", {})), None, True),
            ((("ask", {"session_id": "n1"}), ("other", {"session_id": "n2"})), None, True),
            ((("ask", {}), ("other", {})), None, True),
        ):
            with self.subTest(hooked=hooked):
                self.host.processes.clear()
                live = self.tui(name="w1", sid=SID, events=self.events)
                self.started(*hooked, start=False)
                if hooked:
                    events = live.events()
                    next(events)
                    while any(not isinstance(e, ClientBound) for e in live._queue):
                        next(events)
                attached = self.session.attach("w1")
                self.assertEqual((attached._blocked, attached.info()), (live._blocked, live.info()))
                self.assertEqual((attached.reply, attached._blocked), (reply, blocked))

    def test_after_attach_its_reply_follows_the_live_rule(self):
        self.started(("stop", {}))
        session = self.session.attach("w1")
        self.assertEqual(session.reply, "")
        with open(os.path.join(self.root, "w1", "hooks.jsonl"), "a") as f:
            for message in ("r1", ""):
                payload = {"last_message": message} if message else {}
                f.write(json.dumps({"ts": "", "label": "stop", "payload": payload}) + "\n")
        events = session.events()
        self.assertEqual([next(events), next(events)], [TurnEnd("r1", 0), TurnEnd("", 0)])
        self.assertEqual(session.reply, "r1")

    def test_attach_restores_its_one_clients_fresh_tui_once(self):
        built, restored, load = [], [], self.session_dependencies["class_loader"]

        def counted(path, base):
            found = load(path, base)
            if base is not Client:
                return found

            class Counted(found):
                def __init__(self, config):
                    built.append(self)
                    super().__init__(config)

                def tui(self):
                    client = self

                    class Restored(fake_client.FakeTui):
                        def restore(self, spec, state):
                            restored.append((client, self, spec.name))

                    return Restored(self)

            return Counted

        self.session_dependencies = {**self.session_dependencies, "class_loader": counted}
        self.configure(tui=True)
        self.started()
        for _ in range(2):
            built.clear()
            restored.clear()
            session = self.session.attach("w1")
            self.assertEqual(restored, [(built[0], session._running.run, "w1")])
            self.assertEqual(len(built), 1)

    def test_attach_refreshes_bound_metadata_without_starting_a_session(self):
        self.started(("tool", {"session_id": "n1"}))
        RunLog(self.workdir).note("result", outcome={"status": "done"})

        def metadata(_client, info):
            self.assertEqual(info.sid, SID)
            return dataclasses.replace(info, transcript="/attached", resume="resume n1")

        with mock.patch.object(fake_client.FakeClient, "session_metadata", metadata):
            session = self.session.attach("w1")
        self.assertEqual(session.info().transcript, "/attached")
        self.assertEqual(RunLog(self.workdir).session(SID).resume, "resume n1")
        self.assertEqual(sum(e["kind"] == "session" for e in self.written()), 1)

    def test_attached_while_its_terminal_cannot_tell_is_unknown(self):
        self.started()
        process = self.host.processes["w1"]
        process.calls.state.return_value = Liveness.UNKNOWN
        process.calls.activity.side_effect = SessionError("tmux: docker exec in c: timed out")
        session = self.session.attach("w1")
        self.assertEqual(
            (session.status().state, session.status().alive), (StatusState.UNKNOWN, False)
        )

    def test_attached_while_blocked_no_idle_timeout_until_its_next_event(self):
        """On a fake clock (the session's, time.monotonic, time.sleep), a tool record comes at
        10 s.
        """
        hooks = os.path.join(self.root, "w1", "hooks.jsonl")
        now = [0.0]
        self.session_dependencies = {**self.session_dependencies, "clock": lambda: now[0]}
        self.configure(tui=True)
        for hooked, state, first in (
            ((("tool", {}), ("ask", {})), None, (Activity(""), 10)),
            ((), "blocked", (Activity(""), 10)),
            ((("ask", {}), ("tool", {})), None, (IdleTimeout(1), 1)),
            ((("ask", {}), ("stop", {})), "working", (IdleTimeout(1), 1)),
        ):
            with self.subTest(hooked=hooked, state=state):
                self.host.processes.clear()
                self.started(*hooked)
                self.host.processes["w1"].calls.activity.return_value = state
                self.host.calls.screen.return_value = ""
                now[0] = 0.0

                failure = AssertionError("no event")

                def sleep(seconds, *, failure=failure):
                    if now[0] > 60:
                        raise failure
                    now[0] += seconds
                    if now[0] - seconds < 10 <= now[0]:
                        with open(hooks, "a") as f:
                            f.write(json.dumps({"ts": "", "label": "tool", "payload": {}}) + "\n")

                with (
                    mock.patch("time.monotonic", lambda: now[0]),
                    mock.patch("time.sleep", sleep),
                ):
                    event = next(self.session.attach("w1", idle_timeout=1).events())
                self.assertEqual((event, round(now[0])), first)

    def test_a_bad_name_or_idle_timeout_is_refused_before_tmux_or_a_path(self):
        self.name_refused(self.session.attach)
        for bad in (0, -1, True, "1"):
            with self.subTest(bad=bad), self.assertRaises(ConfigError) as caught:
                self.session.attach("w1", idle_timeout=bad)
            self.assertEqual(
                str(caught.exception),
                f"idle_timeout must be a positive number of seconds, got {bad!r}",
            )
        self.nothing_hosted()

    def test_an_unknown_host_is_refused_before_tmux_or_a_path(self):
        with self.assertRaises(ConfigError) as caught:
            self.session.attach("w1", host="nope")
        self.assertEqual(str(caught.exception), "unknown host nope")
        self.nothing_hosted()

    def test_no_session_dir_or_another_sid_is_no_tui_session(self):
        with self.assertRaises(SessionError) as caught:
            self.session.attach("w1")
        self.assertTrue(str(caught.exception).startswith("no tui session w1: "), caught.exception)
        self.nothing_hosted()
        self.started()
        for found in ("other", None):
            with self.subTest(found=found), self.assertRaises(SessionError) as caught:
                terminal = self.host.processes["w1"]
                terminal.calls.session_identity.return_value = (
                    found,
                    tui.TuiMode.snapshot("w1")["generation"],
                )
                self.session.attach("w1")
            self.assertEqual(str(caught.exception), "no tui session w1")

    def test_a_session_json_without_a_valid_identity_is_no_tui_session(self):
        self.started()
        path = os.path.join(self.root, "w1", "session.json")
        with open(path) as f:
            state = json.load(f)
        with open(path, "w") as f:
            json.dump({**state, "cwd": ""}, f)
        with self.assertRaises(SessionError) as caught:
            self.session.attach("w1")
        self.assertEqual(str(caught.exception), f"no tui session w1: {path}: bad cwd")
        self.host.calls.attach.assert_not_called()

    def test_a_client_without_tui_or_a_registry_error_is_a_driver_error(self):
        self.started()
        self.configure(tui=False)
        for message in (
            "no tui session w1: client 'fake' has no tui mode",
            "no tui session w1: no [clients.fake] table in the configured clients",
        ):
            with self.subTest(message=message), self.assertRaises(SessionError) as caught:
                self.session.attach("w1")
            self.assertEqual(str(caught.exception), message)
            self.configure_session({})
        self.host.calls.attach.assert_not_called()


class MultipleHostsTest(HostedTuiBase):
    def setUp(self):
        super().setUp()
        self.other = MemoryHost("second")
        self.hosts = {"tmux": self.host, "second": self.other}
        tables = {
            name: {"implementation": "tests.session.fakes.terminal:MemoryHost"}
            for name in self.hosts
        }
        self.session.configure(
            clients=self.session._config().registry.settings.clients,
            hosts=parsed(tables, "tmux"),
            class_loader=self.session_dependencies["class_loader"],
            host_factory=lambda _kind, name, _settings: self.hosts[name],
            viewer_factory=MemoryViewer,
        )

    def test_start_and_attach_use_the_named_host(self):
        session = self.tui(name="w1", sid=SID, host="second")
        self.host.calls.terminal.assert_not_called()
        self.other.calls.terminal.assert_called_once()
        self.assertIs(self.session.host("second"), self.other)
        attached = self.session.attach("w1", host="second")
        self.assertEqual((attached.sid, attached.name), (session.sid, session.name))
        self.assertFalse(attached.showable)
        with self.assertRaises(SessionError):
            self.session.attach("w1")

    def test_list_combines_all_hosts_sorted_and_matches_the_display_on_each(self):
        for host, name, display in (
            (self.host, "z1", "elsewhere:1.2"),
            (self.other, "a1", "elsewhere:1.2"),
            (self.other, "b1", "other"),
        ):
            host.create(
                name,
                receipt=Record(
                    1,
                    "fake",
                    name,
                    (name,),
                    None,
                    None,
                    ("ensemble-session",),
                    ("ensemble-session", "resume", name),
                    display,
                    None,
                ),
            )
        self.assertEqual([row.name for row in self.session.list()], ["a1", "b1", "z1"])
        self.assertEqual(
            [row.name for row in self.session.list(display="elsewhere:1.2")], ["a1", "z1"]
        )
        self.assertEqual([row.name for row in self.session.list(r"a.*")], ["a1"])

    def test_start_refuses_a_name_owned_by_another_configured_host(self):
        self.other.create("w1", SID)
        with self.assertRaisesRegex(SessionError, "session w1 exists"):
            self.tui(name="w1")
        self.host.calls.terminal.assert_not_called()
        self.assertFalse(os.path.exists(RunLog(self.workdir).path))

    def test_an_automatic_name_cannot_replace_another_hosts_session(self):
        incumbent = self.other.create("fake-0f8fad5b", SID)
        with self.assertRaisesRegex(SessionError, "^session fake-0f8fad5b exists$"):
            self.tui(sid=SID, events=self.events)
        self.host.calls.terminal.assert_not_called()
        self.assertIs(self.other.attach("fake-0f8fad5b"), incumbent)
        self.assertFalse(os.path.exists(self.workdir))
        self.assertFalse(os.path.exists(self.events))
        self.assertFalse(os.path.exists(self.root))

    def test_dead_names_on_another_host_remain_reserved(self):
        for name, resolved in (("w1", "w1"), (None, "fake-0f8fad5b")):
            incumbent = self.other.create(resolved, SID)
            incumbent.calls.alive.return_value = False
            incumbent.calls.returncode.return_value = 7
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(SessionError, f"^session {resolved} exists$"),
            ):
                self.tui(name=name, sid=SID)
            self.assertIs(self.other.attach(resolved), incumbent)
        self.host.calls.terminal.assert_not_called()
        self.assertFalse(os.path.exists(self.workdir))
        self.assertFalse(os.path.exists(self.root))

    def test_a_dead_name_on_the_selected_host_can_be_replaced(self):
        self.tui(name="w1", sid=SID)
        incumbent = self.host.attach("w1")
        incumbent.calls.alive.return_value = False
        incumbent.calls.returncode.return_value = 7
        launches_at_close = []
        incumbent.calls.close.side_effect = lambda: launches_at_close.append(
            self.host.calls.terminal.call_count
        )
        replacement = self.tui(name="w1", sid=SID)
        current = self.host.attach("w1")
        self.assertIsNot(current, incumbent)
        self.assertNotEqual(incumbent.identity, current.identity)
        incumbent.calls.close.assert_called_once_with()
        self.assertEqual(launches_at_close, [1])
        self.assertEqual(self.host.calls.terminal.call_count, 2)
        self.assertEqual(self.session.attach("w1").record(), replacement.record())

    def test_another_hosts_listing_failure_propagates_before_launch(self):
        failure = SessionError("second Host listing unavailable")
        self.other.calls.list.side_effect = failure
        for name in (None, "w1"):
            with self.subTest(name=name), self.assertRaises(SessionError) as caught:
                self.tui(name=name, sid=SID)
            self.assertIs(caught.exception, failure)
        self.host.calls.terminal.assert_not_called()
        self.assertFalse(os.path.exists(self.workdir))
        self.assertFalse(os.path.exists(self.root))

    def test_list_still_scans_other_hosts_after_the_default_changes(self):
        self.host.create("w1")
        self.other.create("w2")
        setup = self.session._config()
        self.session.configure(
            clients=setup.registry.settings.clients,
            hosts=Hosts("second", setup.hosts.tables),
            class_loader=self.session_dependencies["class_loader"],
            host_factory=lambda _kind, name, _settings: self.hosts[name],
            viewer_factory=MemoryViewer,
        )
        self.assertIs(self.session.host(), self.other)
        self.assertEqual([row.name for row in self.session.list()], ["w1", "w2"])

    def test_simultaneous_hosts_cannot_overwrite_the_first_sessions_state(self):
        entered, checked, release = (threading.Event() for _ in range(3))
        sessions, refusals, errors = {}, {}, []
        lookups = []

        def launch(*_args, **_kwargs):
            entered.set()
            if not release.wait(WAIT):
                raise AssertionError("first launch was not released")

        def listing():
            if threading.current_thread() is later and not lookups:
                lookups.append("second checked first host before its terminal existed")
                checked.set()
                return []
            return None

        def start(host):
            try:
                sessions[host] = self.session.start(
                    Spec(
                        workdir=os.path.join(self.dir, host),
                        access=Access(cwd=os.path.join(self.dir, host)),
                        client="fake",
                        mode=Mode.TUI,
                        sid=SID,
                        name="shared",
                    ),
                    host=host,
                )
            except SessionError as error:
                refusals[host] = str(error)

        first = threading.Thread(target=start, args=("tmux",), daemon=True)
        later = threading.Thread(target=start, args=("second",), daemon=True)
        self.host.calls.terminal.side_effect = launch
        self.host.calls.list.side_effect = listing
        with contextlib.ExitStack() as stack:
            for thread in (first, later):
                stack.enter_context(capture_errors(thread, errors.append))
            try:
                first.start()
                self.assertTrue(entered.wait(WAIT), "first terminal launch did not begin")
                original = tui.TuiMode.snapshot("shared")
                later.start()
                self.assertTrue(checked.wait(WAIT), "second start did not check the first Host")
            finally:
                release.set()
                first.join(WAIT)
                if later.ident is not None:
                    later.join(WAIT)
                for session in sessions.values():
                    self.addCleanup(session.stop)
        self.assertFalse(first.is_alive())
        self.assertFalse(later.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(set(sessions), {"tmux"})
        self.assertEqual(refusals, {"second": "session shared exists"})
        self.other.calls.terminal.assert_not_called()
        self.assertFalse(os.path.exists(os.path.join(self.dir, "second")))
        self.assertEqual(tui.TuiMode.snapshot("shared"), original)
        self.assertEqual(self.session.attach("shared").record(), sessions["tmux"].record())


class ChecksTest(Base):
    def test_refuses_a_spec_without_client_or_mode(self):
        for field, call in (
            ("client", lambda: self.session.start(self.spec(mode=Mode.HEADLESS))),
            ("mode", lambda: self.session.start(self.spec(client="fake"))),
        ):
            with self.subTest(field=field):
                self.refused(ConfigError, f"spec.{field}: required to start", call)

    def test_refuses_an_idle_timeout_that_is_no_positive_number(self):
        for bad in (0, -1, 0.0, -0.5, float("nan"), True, False, "1"):
            with self.subTest(bad=bad):
                self.refused(
                    ConfigError,
                    f"idle_timeout must be a positive number of seconds, got {bad!r}",
                    lambda *, bad=bad: self.start(idle_timeout=bad),
                )

    def test_refuses_an_unknown_host(self):
        self.refused(ConfigError, "unknown host nope", lambda: self.start(host="nope"))

    def test_refuses_a_sid_that_is_not_a_non_empty_printable_string_without_a_slash(self):
        for bad, shown in (
            ("", "''"),
            ("a/b", "'a/b'"),
            ("a\tb", r"'a\tb'"),
            (5, "5"),
            ("s" * 200 + "\n", "'" + "s" * 79),
        ):
            with self.subTest(bad=bad):
                self.refused(
                    ConfigError,
                    f'sid must be a non-empty printable string without "/", got {shown}',
                    lambda *, bad=bad: self.start(sid=bad),
                )

    def test_session_selection_flags_are_refused_before_launch(self):
        from ensemble.session.clients.claude import ClaudeClient
        from ensemble.session.clients.codex import CodexClient

        for client, client_cls in (("claude", ClaudeClient), ("codex", CodexClient)):
            self.configure(
                tables={
                    client: {
                        "implementation": f"ensemble.session.clients.{client}:{client_cls.__name__}"
                    }
                }
            )
            flags = (
                *client_cls.forbidden_selection,
                *(f"{word}=x" for word in client_cls.forbidden_selection if word.startswith("--")),
            )
            for flag in flags:
                with self.subTest(client=client, flag=flag):
                    self.refused(
                        ConfigError,
                        f"{flag}: picks the session; use sid= or resume=",
                        lambda *, flag=flag, client=client: self.start(
                            client=client, spec=self.spec(flags=("--verbose", flag, SID))
                        ),
                    )

    def test_selection_scanning_stops_at_the_separator_and_accepts_other_words(self):
        forbidden = ("--resume", "-r", "resume", "fork", "--last")
        for flags in (("--", *forbidden), ("--resumes", "--continued", "--teleport-x")):
            with self.subTest(flags=flags):
                selection.check_selection(flags, forbidden)

    def test_selection_words_used_as_option_values_are_not_selectors(self):
        from ensemble.session.clients.codex import CodexClient

        for flags in (
            ("--add-dir", "fork"),
            ("--add-dir=resume",),
            ("--enable", "hooks"),
            ("-c", "model=x"),
            ("-cmodel=x",),
        ):
            with self.subTest(flags=flags):
                selection.check_selection(
                    flags, CodexClient.forbidden_selection, CodexClient.valued_flags
                )
        with self.assertRaisesRegex(ConfigError, "fork: picks the session"):
            selection.check_selection(
                ("--add-dir", "safe", "fork"),
                CodexClient.forbidden_selection,
                CodexClient.valued_flags,
            )

    def test_selection_uses_declared_syntax_and_uuid_validation(self):
        syntax = (("--session-id",), ("--resume",))
        self.assertEqual(
            selection.select(SID, syntax, uuid=True, resume=False), ("--session-id", SID)
        )
        self.assertEqual(selection.select(SID, syntax, uuid=True, resume=True), ("--resume", SID))
        self.assertEqual(selection.select("native", ((), ("resume",)), resume=False), ())
        self.assertEqual(
            selection.select("native", ((), ("resume",)), resume=True), ("resume", "native")
        )
        for native in ("abc", SID.upper(), SID + "\n"):
            with (
                self.subTest(native=native),
                self.assertRaisesRegex(ConfigError, "session ids are UUIDs"),
            ):
                selection.select(native, syntax, uuid=True, resume=False)

    def test_a_registry_error_passes_through(self):
        self.refused(
            ConfigError,
            "no [clients.nope] table in the configured clients",
            lambda: self.start(client="nope"),
        )
        self.configure(model="m")
        self.refused(
            ConfigError, "unknown key 'model' in FakeClient's config", lambda: self.start()
        )

    def test_a_failed_session_line_propagates_before_the_start(self):
        os.mkdir(self.workdir)
        os.mkdir(RunLog(self.workdir).path)
        with (
            mock.patch("subprocess.Popen", side_effect=AssertionError("started")) as popen,
            self.assertRaises(OSError),
        ):
            self.start()
        popen.assert_not_called()


class SessionIdentityTest(HostedTuiBase):
    def test_sid_attached_events_yield_a_normal_exit_after_the_terminal_disappears(self):
        self.tui(name="w1", sid=SID)
        session = self.session.attach("w1", sid=SID)

        def disappeared(_):
            self.host.processes.pop("w1", None)

        with mock.patch("time.sleep", side_effect=disappeared):
            events = list(session.events())
        self.assertEqual(events[-1], Exited(-signal.SIGHUP))

    def test_attach_rejects_mismatched_or_unknown_private_sid(self):
        self.tui(name="w1", sid=SID)
        terminal = self.host.processes["w1"]
        for actual in (None, "replacement"):
            with self.subTest(actual=actual):
                terminal.calls.session_identity.return_value = (actual, None)
                with self.assertRaises(SessionError) as caught:
                    self.session.attach("w1", sid=SID)
                self.assertIn(SID, str(caught.exception))
                self.assertIn(str(actual), str(caught.exception))
                terminal.calls.close.assert_not_called()

    def test_retire_closes_the_viewers_view_after_the_session(self):
        self.host.create("plain", sid=SID)
        self.host.calls.reset_mock()
        self.session.retire("plain", sid=SID)
        self.assertNotIn("plain", self.host.processes)
        self.assertEqual(self.host.calls.mock_calls[-1], mock.call.close_view("plain"))

    def test_retire_checks_private_identity_before_closing(self):
        current = self.host.create("plain", sid="replacement")
        with self.assertRaises(SessionError) as caught:
            self.session.retire("plain", sid=SID)
        self.assertIn(SID, str(caught.exception))
        self.assertIn("replacement", str(caught.exception))
        self.assertTrue(current.alive())
        self.session.retire("plain", sid="replacement")
        self.assertNotIn("plain", self.host.processes)

    def test_attached_operations_recheck_sid_after_same_name_replacement(self):
        self.tui(name="w1", sid=SID)
        session = self.session.attach("w1", sid=SID)
        current = self.host.create("w1", sid="replacement")
        self.host.calls.reset_mock()
        actions = (
            lambda: session.send("hello"),
            lambda: session.press((KeyPress("Enter"),)),
            session.alive,
            session.status,
            session.info,
            lambda: session.reply,
            session.close,
            session.session_status,
            session.record,
            lambda: session.showable,
            session.show,
            session.hide,
            session.attach_command,
            session.screen,
        )
        for action in actions:
            with self.subTest(action=action), self.assertRaises(SessionError) as caught:
                action()
            self.assertIn(SID, str(caught.exception))
            self.assertIn("replacement", str(caught.exception))
        current.calls.send.assert_not_called()
        current.calls.press.assert_not_called()
        current.calls.close.assert_not_called()
        self.assertNotIn(mock.call.close_view("w1"), self.host.calls.mock_calls)
        self.assertTrue(current.alive())
        self.assertTrue(os.path.isdir(os.path.join(self.root, "w1")))

    def test_close_waits_for_the_name_lock_before_checking_identity(self):
        self.tui(name="w1", sid=SID)
        session = self.session.attach("w1", sid=SID)
        started = threading.Event()
        errors = []

        def close():
            started.set()
            session.close()

        thread = threading.Thread(target=close, daemon=True)
        with capture_errors(thread, errors.append):
            with inode_lock(tui.tui_lock("w1")):
                thread.start()
                self.assertTrue(started.wait(WAIT))
                replacement = self.host.create("w1", sid="replacement")
            thread.join(WAIT)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], SessionError)
        replacement.calls.close.assert_not_called()
        self.assertTrue(os.path.isdir(os.path.join(self.root, "w1")))

    def test_attach_checks_snapshot_sid_before_constructing_a_mode(self):
        self.tui(name="w1", sid=SID)
        path = Path(self.root) / "w1" / "session.json"
        state = json.loads(path.read_text())
        path.write_text(json.dumps({**state, "sid": "wrong-state"}))
        with self.assertRaises(SessionError) as caught:
            self.session.attach("w1", sid=SID)
        self.assertIn(SID, str(caught.exception))
        self.assertIn("wrong-state", str(caught.exception))
        self.host.processes["w1"].calls.close.assert_not_called()

    def test_poll_ends_a_sid_bound_handle_after_replacement(self):
        self.tui(name="w1", sid=SID)
        session = self.session.attach("w1", sid=SID)
        replacements = []

        def replace(_):
            if not replacements:
                replacements.append(self.host.create("w1", sid="replacement"))

        with mock.patch("time.sleep", side_effect=replace):
            self.assertEqual(list(session.events())[-1], Exited(-signal.SIGHUP))
        replacements[0].calls.close.assert_not_called()

    def test_stop_ends_a_sid_bound_handle_without_closing_replacement(self):
        self.tui(name="w1", sid=SID)
        session = self.session.attach("w1", sid=SID)
        replacement = self.host.create("w1", sid="replacement")
        session.stop()
        replacement.calls.close.assert_not_called()
        self.assertTrue(replacement.alive())

    def test_content_uses_current_activity(self):
        session = self.tui(name="w1", sid=SID)
        self.host.calls.screen.return_value = "current screen"
        terminal = self.host.processes["w1"]
        path = Path(self.root) / "w1" / "hooks.jsonl"
        path.write_text(
            json.dumps({"label": "stop", "payload": {"last_message": "last reply"}}) + "\n"
        )
        session = self.session.attach("w1")
        for activity, expected in (
            (StatusState.DONE, "last reply"),
            (StatusState.WORKING, "current screen"),
            (StatusState.BLOCKED, "current screen"),
        ):
            with self.subTest(activity=activity):
                terminal.calls.activity.return_value = activity
                self.assertEqual(session.content(), expected)

    def test_content_falls_back_to_screen_without_reply(self):
        session = self.tui(name="w1", sid=SID)
        self.host.processes["w1"].calls.activity.return_value = StatusState.DONE
        self.host.calls.screen.return_value = "screen fallback"
        self.assertEqual(session.content(), "screen fallback")


ENVIRONMENT = engine_types.EnvironmentSpec(image="dummy-env:test", gateway="workers", lock="l")


class EnvironmentHost(MemoryHost):
    """A Docker-kind Host: keeps the metadata of each _preflight and _validate; busy, when set,
    is what its preflight or terminal raises.
    """

    _environments = True

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.checked: list[tuple[str, dict]] = []
        self.busy: tuple[str, engine_types.SessionBusy] | None = None

    def _raise(self, where: str) -> None:
        if self.busy is not None and self.busy[0] == where:
            raise self.busy[1]

    def _preflight(self, name, launch, metadata=None):
        self.checked.append(("preflight", dict(metadata or {})))
        self._raise("preflight")

    def _validate(self, name, launch, metadata=None):
        self.checked.append(("validate", dict(metadata or {})))

    def terminal(self, name, launch, **kwargs):
        self._raise("terminal")
        return super().terminal(name, launch, **kwargs)


class EnvironmentHostTest(HostedTuiBase):
    def setUp(self):
        super().setUp()
        self.docker = EnvironmentHost("d2")
        self.hosts = {"tmux": self.host, "d2": self.docker}
        tables = {
            name: {"implementation": "tests.session.fakes.terminal:MemoryHost"}
            for name in self.hosts
        }
        self.session.configure(
            clients=self.session._config().registry.settings.clients,
            hosts=parsed(tables, "tmux"),
            class_loader=self.session_dependencies["class_loader"],
            host_factory=lambda _kind, name, _settings: self.hosts[name],
            viewer_factory=MemoryViewer,
        )
        self.busy = engine_types.SessionBusy("l", "c (owner o)")

    def env_spec(self, **fields):
        given = {"client": "fake", "mode": Mode.TUI, "name": "w1", "sid": SID}
        return self.spec(**{**given, "environment": ENVIRONMENT, **fields})

    def test_tmux_host_refuses_an_environment_before_anything_starts(self):
        for call in (
            lambda: self.session.start(self.env_spec()),
            lambda: self.session.preflight(self.env_spec()),
        ):
            with self.subTest(call):
                self.refused(ConfigError, "environment: host tmux is not a Docker-kind Host", call)
        self.assertEqual(self.host.calls.mock_calls, [])

    def test_a_docker_kind_host_of_another_name_accepts_it(self):
        self.session.preflight(self.env_spec(), host="d2")
        session = self.session.start(self.env_spec(), host="d2")
        self.addCleanup(session.stop)
        carried = json.dumps(ENVIRONMENT.persisted())
        self.assertEqual(
            [(where, found.get("environment")) for where, found in self.docker.checked],
            [("preflight", carried), ("validate", carried)],
        )
        metadata = self.docker.calls.terminal.call_args.kwargs["metadata"]
        self.assertEqual(metadata["environment"], carried)

    def test_without_an_environment_the_metadata_has_none(self):
        session = self.session.start(self.env_spec(environment=None), host="d2")
        self.addCleanup(session.stop)
        self.assertNotIn("environment", self.docker.calls.terminal.call_args.kwargs["metadata"])

    def test_in_process_refuses_it(self):
        spec = self.env_spec(mode=Mode.IN_PROCESS, name=None)
        self.refused(
            ConfigError,
            "environment: in_process mode runs on no Host",
            lambda: self.session.start(spec, host="d2"),
        )

    def test_preflight_and_start_keep_session_busy(self):
        for where, call in (
            ("preflight", lambda: self.session.preflight(self.env_spec(), host="d2")),
            ("terminal", lambda: self.session.start(self.env_spec(), host="d2")),
        ):
            self.docker.busy = (where, self.busy)
            with self.subTest(where), self.assertRaises(engine_types.SessionBusy) as raised:
                call()
            self.assertIs(raised.exception, self.busy)


class OwnedTest(HostedTuiBase):
    """HostedSession.own: the owner handle a session harness drives its one worker through."""

    def own(self, name="w1"):
        self.channel = Channel.create()
        spec = dataclasses.replace(
            self.spec(), client="fake", mode="tui", prompt="hello", name=name, sid=SID
        )
        owned = self.session.own(spec, owner=self.channel.directory)
        self.addCleanup(lambda: self.host.processes.pop(name, None))
        return owned

    def hook(self, label, payload, name="w1"):
        with open(os.path.join(self.root, name, "hooks.jsonl"), "a") as f:
            f.write(json.dumps({"ts": "", "label": label, "payload": payload}) + "\n")

    def test_own_starts_the_worker_its_owner_dir_named(self):
        owned = self.own()
        state = tui.TuiMode.snapshot("w1")
        self.assertEqual(state["owner"], self.channel.directory)
        self.assertEqual(
            (owned.name, owned.sid, owned.generation), ("w1", SID, state["generation"])
        )
        self.assertIn("w1", self.host.processes)

    def test_pump_yields_events_never_exited_and_says_when_the_worker_ended(self):
        owned = self.own()
        self.hook("stop", {"last_message": "r1"})
        found, ended = owned.pump(0.5)
        self.assertEqual(
            ([e for e in found if not isinstance(e, ClientBound)], ended),
            ([TurnEnd("r1", 0)], False),
        )
        terminal = self.host.processes["w1"]
        terminal.calls.state.return_value = Liveness.LIVE
        terminal.calls.returncode.return_value = 3
        found, ended = owned.pump(0.1)
        self.assertTrue(ended)
        self.assertNotIn(Exited(3), found)
        self.assertEqual(owned.pump(0), ([], True))

    def test_close_returns_what_it_read_before_ending_the_terminal(self):
        owned = self.own()
        terminal = self.host.processes["w1"]
        self.hook("stop", {"last_message": "last"})
        self.host.calls.close_view.reset_mock()
        final = owned.close()
        self.assertIn(TurnEnd("last", 0), final)
        terminal.calls.close.assert_called_once_with()
        self.host.calls.close_view.assert_called_once_with("w1")
        self.assertFalse(os.path.lexists(os.path.join(self.root, "w1")))
        self.assertEqual(owned.confirm_end(1), -signal.SIGHUP)

    def test_stop_returns_its_final_read_and_leaves_the_viewer(self):
        owned = self.own()
        self.hook("stop", {"last_message": "last"})
        self.host.calls.close_view.reset_mock()
        self.assertIn(TurnEnd("last", 0), owned.stop())
        self.host.calls.close_view.assert_not_called()
        self.assertEqual(owned.confirm_end(1), -signal.SIGHUP)

    def test_confirm_end_is_none_while_the_terminal_lives(self):
        owned = self.own()
        self.assertIsNone(owned.confirm_end(0.1))

    def test_an_in_process_worker_is_owned_nameless(self):
        self.configure(implementation="ensemble.session.clients.fake:InProcessFakeClient")
        channel = Channel.create()
        spec = self.spec(client="fake", mode="in_process", prompt="hello", sid=SID)
        owned = self.session.own(spec, owner=channel.directory)
        self.assertIsNone(owned.name)
        self.assertTrue(owned.generation)
        found, ended = [], False
        deadline = time.monotonic() + 10
        while not ended and time.monotonic() < deadline:
            events, ended = owned.pump(0.1)
            found += events
        self.assertTrue(ended)
        self.assertNotIn(Exited(0), found)
        self.assertEqual(owned.confirm_end(1), 0)
        view = owned.view(channel)
        self.assertIsInstance(view, OwnedSession)
        self.assertEqual(
            (view.sid, view.generation, view.mode), (SID, owned.generation, Mode.IN_PROCESS)
        )

    def test_confirm_end_unanswered_is_none(self):
        owned = self.own()
        self.host.processes["w1"].calls.read.side_effect = engine_types.Unanswered(
            "worker w1: gone"
        )
        self.assertIsNone(owned.confirm_end(0.1))

    def test_confirm_end_ignores_the_display(self):
        owned = self.own()
        handle = self.host.processes["w1"]
        handle.calls.state.return_value = Liveness.ABSENT
        handle.calls.returncode.return_value = 0
        self.host.calls.list.return_value = ["w1"]  # a display of its name still listed
        self.assertEqual(owned.confirm_end(1), 0)

    def test_close_keeps_the_viewer_when_the_record_stays(self):
        owned = self.own()
        state = tui.TuiMode.snapshot("w1")
        with open(os.path.join(self.root, "w1", "session.json"), "w") as f:
            json.dump({**state, "generation": "newer"}, f)
        self.host.calls.close_view.reset_mock()
        owned.close()
        self.host.calls.close_view.assert_not_called()
        self.assertEqual(tui.TuiMode.snapshot("w1")["generation"], "newer")

    def test_record_notes_client_sessions_and_stderr(self):
        owned = self.own()
        owned.record(ClientBound("n1"))
        owned.record(Stderr("oops"))
        kinds = [entry["kind"] for entry in self.written()]
        self.assertIn("client_session", kinds)
        self.assertIn("stderr", kinds)

    def test_redisplay_asks_its_managed_handle(self):
        owned = self.own()
        self.assertFalse(owned.redisplay())  # a plain terminal: nothing to open
        handle = mock.create_autospec(ManagedProcess, instance=True)
        handle.redisplay.return_value = True
        owned._session._running._terminal = handle
        self.assertTrue(owned.redisplay())
        handle.redisplay.assert_called_once_with()

    def test_view_is_bound_to_this_worker(self):
        owned = self.own()
        view = owned.view(self.channel, idle_timeout=3)
        self.assertIsInstance(view, OwnedSession)
        self.assertEqual(
            (view.name, view.client, view.sid, view.generation, view.mode),
            ("w1", "fake", SID, owned.generation, Mode.TUI),
        )
        before = len(self.written())
        self.assertTrue(view.alive())
        view.info()
        self.assertEqual(len(self.written()), before, "the view's attach writes no RunLog line")

    def test_the_views_attach_refuses_another_generation(self):
        owned = self.own()
        view = owned.view(self.channel)
        self.host.processes.pop("w1")
        self.own()
        with self.assertRaisesRegex(SessionError, "w1"):
            view.screen()


class _ScriptedPiped(Piped):
    def __init__(self, out: str, code: int | None):
        self.stdout, self.stderr, self.code = io.StringIO(out), io.StringIO(), code

    def wait(self, timeout):
        return self.code

    def stop(self, *, ended):
        self.code = -signal.SIGTERM if self.code is None else self.code

    def close(self):
        return None


class OwnedHeadlessTest(HostedTuiBase):
    """HostedSession.own of a headless run: nameless, its own generation, ended by its exit."""

    def own(self, out="", code=None):
        """Its run on a pipe printing `out`; code: its exit's, None till stopped."""
        piped = _ScriptedPiped(out, code)
        self.host.pipe = lambda *_args, **_kw: piped
        self.channel = Channel.create()
        spec = dataclasses.replace(self.spec(), client="fake", mode="headless", sid=SID)
        owned = self.session.own(spec, owner=self.channel.directory)
        self.addCleanup(owned.stop)
        return owned

    def pumped(self, owned) -> list:
        found, deadline = [], time.monotonic() + WAIT
        while time.monotonic() < deadline:
            batch, ended = owned.pump(0.1)
            found.extend(batch)
            if ended:
                return found
        self.fail("the headless worker never ended")

    def test_own_runs_it_nameless_under_a_generation_of_its_own(self):
        owned = self.own('{"type": "result", "text": "done"}\n', 0)
        self.assertEqual((owned.name, owned.sid), (None, SID))
        self.assertRegex(owned.generation, "^[0-9a-f]{32}$")
        self.assertIn(TurnEnd("done", 0), self.pumped(owned))
        self.assertEqual(owned.confirm_end(WAIT), 0)

    def test_stop_returns_its_interrupted_turn_and_its_exit_is_confirmed(self):
        owned = self.own()
        final = owned.stop()
        self.assertEqual([e for e in final if isinstance(e, TurnFailed)][-1].error, "interrupted")
        self.assertIsNotNone(owned.confirm_end(WAIT))

    def test_its_view_is_a_headless_view_with_no_terminal(self):
        owned = self.own()
        view = owned.view(self.channel)
        self.assertEqual(
            (view.mode, view.name, view.sid, view.generation),
            (Mode.HEADLESS, None, SID, owned.generation),
        )
        self.assertEqual(view.info().sid, SID)
        self.assertIsInstance(view.session_status(), engine_types.SessionStatus)
        self.assertFalse(view.alive())
        self.assertEqual((view.status().name, view.status().alive), (SID, False))
        self.assertFalse(view.showable)
        self.assertIsNone(view.record())
        with self.assertRaisesRegex(SessionError, "send: tui mode only"):
            view.send("x")
        with self.assertRaisesRegex(SessionError, "press: tui mode only"):
            view.press(("Enter",))
        for call in (view.screen, view.content, view.show, view.hide, view.attach_command):
            with self.subTest(call=call.__name__), self.assertRaises(NotShowable):
                call()


class HarnessRequestTest(HostedTuiBase):
    """harness_request and from_harness_request: a start sent to a session harness and back."""

    def test_a_spec_round_trips_and_configures_the_harness_class(self):
        spec = self.spec(
            access=Access(cwd=self.workdir, dirs=(self.dir,), commands=("git",), env={"A": "1"}),
            client="fake",
            mode="tui",
            name="w1",
            sid=SID,
            prompt="hello",
            env={"TOKEN": "t"},
            flags=("--x",),
            handlers=(engine_types.HookCallback("pkg.mod", {"k": [1]}, (self.dir,)),),
        )
        with mock.patch.dict(os.environ, {"PYTHONPATH": "lib:/abs/src"}):
            request = json.loads(
                json.dumps(
                    self.session.harness_request(spec, display="d", host="tmux", roots=(self.dir,))
                )
            )
        fresh = type("Fresh", (HostedSession,), {"__module__": __name__})
        start = fresh.from_harness_request(request)
        self.assertEqual(start, (spec, "d", "tmux", (self.dir,), "lib:/abs/src"))
        self.assertEqual((start.spec, start.pythonpath), (spec, "lib:/abs/src"))
        self.assertEqual(fresh._config().hosts, self.session._config().hosts)

    def test_a_caller_without_pythonpath_sends_none(self):
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        with mock.patch.dict(os.environ, env, clear=True):
            request = self.session.harness_request(
                self.spec(client="fake", mode="tui"), display=None, host=None, roots=()
            )
        fresh = type("Fresh", (HostedSession,), {"__module__": __name__})
        self.assertIsNone(fresh.from_harness_request(request).pythonpath)

    def test_a_malformed_request_is_a_config_error(self):
        good = self.session.harness_request(
            self.spec(client="fake", mode="tui"), display=None, host=None, roots=()
        )
        fresh = type("Fresh", (HostedSession,), {"__module__": __name__})
        for bad in (
            {},
            {**good, "spec": {**good["spec"], "nope": 1}},
            {**good, "roots": ["relative"]},
            {**good, "host": 5},
            {**good, "pythonpath": ["/a"]},
            {**good, "pythonpath": 5},
            {**good, "pythonpath": "/a\n/b"},
        ):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                fresh.from_harness_request(bad)


class OwnedStartTest(Case):
    """An owned start (a Claude tui start on tmux, every Setup callable the default) spawns a
    session harness, here a stub Popen whose fake harness writes harness.json.
    """

    def setUp(self):
        self.session_cls = HostedSession.configured(
            clients={"claude": {"implementation": "ensemble.session.clients.claude:ClaudeClient"}},
            hosts=Host.parse_hosts({"tmux": HOSTS["tmux"], "default": "tmux"}),
        )
        self.workdir = os.path.join(self.home, "work")
        os.mkdir(self.workdir)
        self.popen = self.enterContext(mock.patch("subprocess.Popen"))
        self.popen.return_value.poll.return_value = None

    def spec(self, **fields):
        return Spec(
            workdir=self.workdir,
            access=Access(cwd=self.workdir),
            client="claude",
            mode="tui",
            prompt="hi",
            sid=SID,
            name="w1",
            **fields,
        )

    def harness_writes(self, status, *, log=None, poll=None):
        def spawn(argv, **kwargs):
            channel = Channel(argv[-1])
            if log is not None:
                kwargs["stderr"].write(log)
                kwargs["stderr"].flush()
            if status is not None:
                channel.write_status(status)
            self.popen.return_value.poll.return_value = poll
            self.spawned = (argv, kwargs, channel)
            return self.popen.return_value

        self.popen.side_effect = spawn

    def test_it_returns_the_view_once_the_harness_runs(self):
        self.harness_writes({"state": "running", "name": "w1", "sid": SID, "generation": "g7"})
        session = self.session_cls.start(self.spec(), idle_timeout=5)
        argv, kwargs, channel = self.spawned
        self.assertIsInstance(session, OwnedSession)
        self.assertEqual(
            (session.name, session.sid, session.client, session.generation),
            ("w1", SID, "claude", "g7"),
        )
        self.assertEqual(argv[1:], ["-P", "-s", "-m", hosted_session.HARNESS, channel.directory])
        self.assertEqual(kwargs.get("pass_fds", ()), ())  # a tui worker outlives its caller
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(kwargs["stdin"], kwargs["stdout"])
        request = os.path.join(channel.directory, "request.json")
        self.assertEqual(stat.S_IMODE(os.stat(request).st_mode), 0o600)
        with open(request, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["spec"]["name"], "w1")
        self.popen.return_value.kill.assert_not_called()

    def test_a_start_error_in_the_harness_is_raised_here_and_its_dir_goes(self):
        for error, kind, message in (
            ({"type": "ConfigError", "message": "bad"}, ConfigError, "bad"),
            ({"type": "SessionError", "message": "w1 exists"}, SessionError, "w1 exists"),
            (
                {"type": "SessionBusy", "message": "busy", "lock": "L", "holder": "c (owner o)"},
                engine_types.SessionBusy,
                "busy: lock L is held by c \\(owner o\\)",
            ),
        ):
            with self.subTest(kind=kind.__name__):
                self.harness_writes({"state": "failed", "error": error}, poll=1)
                with self.assertRaisesRegex(kind, message) as caught:
                    self.session_cls.start(self.spec())
                self.assertIs(type(caught.exception), kind)
                self.assertFalse(os.path.exists(self.spawned[2].directory))

    def test_a_dead_harness_raises_its_last_log_line_and_a_live_one_is_killed(self):
        self.harness_writes(None, log=b"starting\nTraceback: boom\n", poll=1)
        with self.assertRaisesRegex(SessionError, "did not start: Traceback: boom"):
            self.session_cls.start(self.spec())
        self.popen.return_value.kill.assert_not_called()
        self.harness_writes({"state": "failed"}, poll=None)
        with self.assertRaises(SessionError):
            self.session_cls.start(self.spec())
        self.popen.return_value.kill.assert_called_once_with()
        self.assertFalse(os.path.exists(self.spawned[2].directory))

    def owned_record(self, generation: str = "g1") -> Channel:
        """w1's session.json naming a held harness running generation `generation`."""
        channel = Channel.create()
        fd = os.open(channel.lock, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.addCleanup(os.close, fd)
        channel.write_status(
            {"state": "running", "name": "w1", "sid": SID, "generation": generation}
        )
        folder = os.path.join(self.ensemble, "tui", "w1")
        os.makedirs(folder, 0o700)
        state = {
            "client": "claude",
            "workdir": self.workdir,
            "sid": SID,
            "cwd": self.workdir,
            "project": False,
            "transcript": "",
            "resume": "",
            "events": None,
            "words": {},
            "host": "tmux",
            "generation": generation,
            "owner": channel.directory,
        }
        with open(os.path.join(folder, "session.json"), "w") as f:
            json.dump(state, f)
        tui.TuiMode.snapshot("w1")  # valid
        return channel

    def test_running_owner_refused_before_spawn(self):
        self.owned_record()
        for call in (self.session_cls.preflight, self.session_cls.start):
            with (
                self.subTest(call=call.__name__),
                self.assertRaisesRegex(SessionError, "^tui session w1 is running"),
            ):
                call(self.spec(resume=True))
        self.popen.assert_not_called()

    def transcript(self) -> str:
        """Where claude keeps w1's conversation for this spec (none there yet)."""
        config = {"CLAUDE_CONFIG_DIR": os.path.join(self.home, ".claude")}
        self.enterContext(mock.patch.dict(os.environ, config))
        client = self.session_cls._config().registry.client("claude")
        assert isinstance(client, ClaudeClient)
        return client.common.transcript(self.workdir, SID)

    def test_owned_resume_without_transcript_refused(self):
        self.assertFalse(os.path.lexists(self.transcript()))
        for call in (self.session_cls.preflight, self.session_cls.start):
            with (
                self.subTest(call=call.__name__),
                self.assertRaisesRegex(
                    ConfigError, f"^session w1: conversation {SID} has no transcript to resume$"
                ),
            ):
                call(self.spec(resume=True))
        self.popen.assert_not_called()

    def test_owned_resume_on_a_container_host_leaves_the_check_to_it(self):
        """A container Host keeps the native history (Docker: its own home) and refuses itself."""
        self.assertFalse(os.path.lexists(self.transcript()))
        client = self.session_cls._config().registry.client("claude")
        host = mock.Mock()
        host._client_context.return_value = mock.Mock(container=object())
        hosted_session._resume_check(self.spec(resume=True), client, "w1", host)
        host._client_context.return_value = mock.Mock(container=None)
        with self.assertRaises(ConfigError):
            hosted_session._resume_check(self.spec(resume=True), client, "w1", host)

    def test_owned_resume_unknown_goes_on(self):
        self.harness_writes({"state": "running", "name": "w1", "sid": SID, "generation": "g7"})
        path = self.transcript()
        os.makedirs(path)  # a directory there: unknown
        self.assertIsInstance(self.session_cls.start(self.spec(resume=True)), OwnedSession)
        os.rmdir(path)
        Path(path).touch()  # a transcript: resumable
        self.assertIsInstance(self.session_cls.start(self.spec(resume=True)), OwnedSession)

    def test_a_worker_that_exited_at_once_still_gives_its_view(self):
        self.harness_writes(
            {"state": "exited", "name": "w1", "sid": SID, "generation": "g7"}, poll=0
        )
        session = self.session_cls.start(self.spec())
        self.assertIsInstance(session, OwnedSession)
        self.assertTrue(os.path.isdir(self.spawned[2].directory))

    def test_a_harness_that_never_runs_is_killed_at_the_deadline(self):
        self.harness_writes({"state": "starting"})
        clock = itertools.chain([0.0], itertools.repeat(hosted_session.START_WAIT + 1.0))
        with (
            mock.patch("time.monotonic", side_effect=lambda: next(clock)),
            self.assertRaisesRegex(SessionError, "did not start"),
        ):
            self.session_cls.start(self.spec())
        self.popen.return_value.kill.assert_called_once_with()
        self.assertFalse(os.path.exists(self.spawned[2].directory))

    def test_an_interrupted_start_kills_its_harness_and_its_dir_goes(self):
        self.harness_writes({"state": "starting"})
        with (
            mock.patch("time.sleep", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.session_cls.start(self.spec())
        self.popen.return_value.kill.assert_called_once_with()
        self.assertFalse(os.path.exists(self.spawned[2].directory))

    def test_a_stop_right_after_popen_kills_its_harness(self):
        self.harness_writes({"state": "running", "name": "w1", "sid": SID, "generation": "g7"})
        monotonic = time.monotonic

        def clock():
            if self.popen.called:
                raise KeyboardInterrupt
            return monotonic()

        with (
            mock.patch("time.monotonic", side_effect=clock),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.session_cls.start(self.spec())
        self.popen.return_value.kill.assert_called_once_with()
        self.assertFalse(os.path.exists(self.spawned[2].directory))

    def test_a_stop_while_its_view_is_built_kills_its_harness(self):
        self.harness_writes({"state": "running", "name": "w1", "sid": SID, "generation": "g7"})
        with (
            mock.patch.object(self.session_cls, "_view", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.session_cls.start(self.spec())
        self.popen.return_value.kill.assert_called_once_with()
        self.assertFalse(os.path.exists(self.spawned[2].directory))

    def stop_on_sigterm(self) -> type[Exception]:
        """SIGTERM raises the returned exception here, as a drive's stop handler does."""

        class Stop(Exception):
            pass

        def stop(_signum, _frame):
            raise Stop

        self.addCleanup(signal.signal, signal.SIGTERM, signal.signal(signal.SIGTERM, stop))
        self.addCleanup(
            signal.pthread_sigmask, signal.SIG_SETMASK, signal.pthread_sigmask(signal.SIG_BLOCK, ())
        )
        return Stop

    def test_a_stop_signal_during_popen_lands_after_it_and_kills_its_harness(self):
        stop = self.stop_on_sigterm()
        self.harness_writes({"state": "running", "name": "w1", "sid": SID, "generation": "g7"})
        spawn = self.popen.side_effect

        def signalled(argv, **kwargs):
            signal.pthread_kill(threading.get_ident(), signal.SIGTERM)
            return spawn(argv, **kwargs)

        self.popen.side_effect = signalled
        with self.assertRaises(stop):
            self.session_cls.start(self.spec())
        self.popen.return_value.kill.assert_called_once_with()
        self.assertFalse(os.path.exists(self.spawned[2].directory))

    def test_a_stop_signal_during_a_failing_popen_leaves_no_request(self):
        stop = self.stop_on_sigterm()
        directories = []

        def failing(argv, **_kwargs):
            directories.append(argv[-1])
            signal.pthread_kill(threading.get_ident(), signal.SIGTERM)
            raise OSError(errno.EAGAIN, "Resource temporarily unavailable")

        self.popen.side_effect = failing
        with self.assertRaises(stop):
            self.session_cls.start(self.spec())
        self.assertFalse(os.path.exists(directories[0]))

    def test_handler_state_json_cannot_hold_leaves_no_request(self):
        spec = self.spec(handlers=(engine_types.HookCallback("m", {"s": {1}}),))
        with self.assertRaisesRegex(ConfigError, "not JSON"):
            self.session_cls.start(spec)
        self.popen.assert_not_called()
        root = os.path.join(self.ensemble, "harness")
        self.assertEqual([d for d in os.listdir(root) if not d.startswith(".")], [])

    def test_a_harness_running_another_session_is_refused(self):
        self.harness_writes({"state": "running", "name": "w2", "sid": SID, "generation": "g"})
        with self.assertRaisesRegex(SessionError, "started another session"):
            self.session_cls.start(self.spec())
        self.assertFalse(os.path.exists(self.spawned[2].directory))

    def test_an_in_process_start_runs_in_a_harness_and_gives_its_view(self):
        self.harness_writes({"state": "running", "name": None, "sid": SID, "generation": "g7"})
        session_cls = HostedSession.configured(
            clients={"fake": {"implementation": "tests.session.fake_client:InProcessFakeClient"}},
            hosts=Host.parse_hosts({"tmux": HOSTS["tmux"], "default": "tmux"}),
        )
        spec = Spec(
            workdir=self.workdir,
            access=Access(cwd=self.workdir),
            client="fake",
            mode="in_process",
            prompt="hi",
            sid=SID,
        )
        session = session_cls.start(spec)
        argv, _, channel = self.spawned
        self.assertIsInstance(session, OwnedSession)
        self.assertEqual(
            (session.sid, session.generation, session.name, session.mode),
            (SID, "g7", None, Mode.IN_PROCESS),
        )
        self.assertEqual(argv[1:], ["-P", "-s", "-m", hosted_session.HARNESS, channel.directory])
        self.harness_writes({"state": "running", "name": None, "sid": "other", "generation": "g"})
        with self.assertRaisesRegex(SessionError, "started another session"):
            session_cls.start(spec)

    def test_preflight_finds_handlers_without_importing_them(self):
        root = os.path.join(self.home, "handlers")
        os.mkdir(root)
        with open(os.path.join(root, "never_imported_handler.py"), "w") as f:
            f.write("raise SystemExit('imported')\n")
        session_cls = HostedSession.configured(
            clients={"fake": {"implementation": "tests.session.fake_client:InProcessFakeClient"}},
            hosts=Host.parse_hosts({"tmux": HOSTS["tmux"], "default": "tmux"}),
        )
        good = engine_types.HookCallback("never_imported_handler", roots=(root,))
        spec = Spec(
            workdir=self.workdir,
            access=Access(cwd=self.workdir),
            client="fake",
            mode="in_process",
            prompt="hi",
            sid=SID,
            handlers=(good,),
        )
        session_cls.preflight(spec)
        missing = dataclasses.replace(spec, handlers=(engine_types.HookCallback("no_such_mod"),))
        with self.assertRaisesRegex(ConfigError, "no_such_mod: cannot find"):
            session_cls.preflight(missing)
        self.popen.assert_not_called()

    def test_an_environment_is_refused_before_any_harness(self):
        with self.assertRaises(ConfigError):
            self.session_cls.harness_request(
                self.spec(environment=mock.Mock(spec=engine_types.EnvironmentSpec)),
                display=None,
                host=None,
                roots=(),
            )
        self.popen.assert_not_called()


class OwnedHeadlessStartTest(Case):
    """A built-in client's headless start on tmux spawns a session harness too, nameless."""

    setUp = OwnedStartTest.setUp
    harness_writes = OwnedStartTest.harness_writes

    def spec(self, **fields):
        return dataclasses.replace(OwnedStartTest.spec(self, **fields), mode="headless", name=None)

    def test_it_returns_a_headless_view_once_the_harness_runs(self):
        self.harness_writes({"state": "running", "name": None, "sid": SID, "generation": "g7"})
        session = self.session_cls.start(self.spec())
        self.assertIsInstance(session, OwnedSession)
        self.assertEqual(
            (session.mode, session.name, session.sid, session.generation),
            (Mode.HEADLESS, None, SID, "g7"),
        )
        with open(os.path.join(self.spawned[2].directory, "request.json"), encoding="utf-8") as f:
            self.assertIsNone(json.load(f)["spec"]["name"])

    def test_a_failed_spawn_terminates_its_harness_before_any_kill(self):
        self.harness_writes({"state": "starting"})
        clock = itertools.chain([0.0], itertools.repeat(hosted_session.START_WAIT + 1.0))
        with (
            mock.patch("time.monotonic", side_effect=lambda: next(clock)),
            self.assertRaisesRegex(SessionError, "did not start"),
        ):
            self.session_cls.start(self.spec())
        harness = self.popen.return_value
        harness.terminate.assert_called_once_with()
        harness.wait.assert_any_call(hosted_session.CLOSE_WAIT)
        self.assertFalse(os.path.exists(self.spawned[2].directory))

    def test_an_interrupted_start_terminates_its_harness_and_its_dir_goes(self):
        self.harness_writes({"state": "starting"})
        with (
            mock.patch("time.sleep", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.session_cls.start(self.spec())
        self.popen.return_value.terminate.assert_called_once_with()
        self.assertFalse(os.path.exists(self.spawned[2].directory))

    def test_a_caller_pipe_it_cannot_make_is_a_session_error_leaving_no_dir(self):
        error = OSError(errno.EMFILE, "Too many open files")
        with (
            mock.patch("os.pipe", side_effect=error),
            self.assertRaisesRegex(SessionError, "Too many open files"),
        ):
            self.session_cls.start(self.spec())
        self.popen.assert_not_called()
        root = os.path.join(self.ensemble, "harness")
        self.assertEqual([d for d in os.listdir(root) if not d.startswith(".")], [])

    def test_its_harness_reads_a_pipe_that_ends_with_its_view(self):
        held = []

        def spawn(argv, **kwargs):
            (far,) = kwargs["pass_fds"]
            self.assertEqual(argv[-3:-1], ["--caller", str(far)])
            held.append(os.dup(far))  # the harness's copy
            Channel(argv[-1]).write_status(
                {"state": "running", "name": None, "sid": SID, "generation": "g7"}
            )
            return self.popen.return_value

        self.popen.side_effect = spawn
        session = self.session_cls.start(self.spec())
        (far,) = held
        self.addCleanup(os.close, far)
        self.assertEqual(select.select([far], [], [], 0)[0], [])  # its caller holds the other end
        del session
        gc.collect()
        self.assertEqual(select.select([far], [], [], 0)[0], [far])
        self.assertEqual(os.read(far, 1), b"")  # EOF: the harness closes its worker


class OwnedGateTest(Case):
    def test_built_in_clients_tui_and_headless_are_owned_on_tmux(self):
        cls = HostedSession.configured(
            clients={
                "claude": {"implementation": "ensemble.session.clients.claude:ClaudeClient"},
                "codex": {"implementation": "ensemble.session.clients.codex:CodexClient"},
                "opencode": {"implementation": "ensemble.session.clients.opencode:OpenCodeClient"},
            },
            hosts=Host.parse_hosts({"tmux": HOSTS["tmux"], "default": "tmux"}),
        )
        setup, on = cls._config(), cls.host()
        for name in ("claude", "codex", "opencode"):
            client = setup.registry.client(name)
            for mode in (Mode.TUI, Mode.HEADLESS):
                with self.subTest(client=name, mode=mode):
                    self.assertTrue(hosted_session._owned(setup, PLAIN, mode, client, on))
            self.assertFalse(hosted_session._owned(setup, PLAIN, Mode.IN_PROCESS, client, on))

    def test_a_start_with_an_environment_is_never_owned(self):
        cls = HostedSession.configured(
            clients={
                "claude": {"implementation": "ensemble.session.clients.claude:ClaudeClient"},
                "codex": {"implementation": "ensemble.session.clients.codex:CodexClient"},
                "opencode": {"implementation": "ensemble.session.clients.opencode:OpenCodeClient"},
            },
            hosts=Host.parse_hosts({"tmux": HOSTS["tmux"], "default": "tmux"}),
        )
        setup, docker = cls._config(), DockerHost("docker", {"docker": "/usr/local/bin/docker"})
        for on in (cls.host(), docker):
            for name in ("claude", "codex", "opencode"):
                for mode in (Mode.TUI, Mode.HEADLESS):
                    with self.subTest(host=on.name, client=name, mode=mode):
                        client = setup.registry.client(name)
                        self.assertFalse(hosted_session._owned(setup, ENVIRONED, mode, client, on))


class LegacyHandlersTest(HostedTuiBase):
    def test_handlers_on_a_start_no_harness_owns_are_refused(self):
        handlers = (engine_types.HookCallback("pkg.handler"),)
        with self.assertRaisesRegex(
            ConfigError, "fake tui on host tmux has no session harness yet"
        ):
            self.tui(name="w1", handlers=handlers)
        self.nothing_hosted()
        with self.assertRaisesRegex(ConfigError, "fake headless on host tmux has no session"):
            self.start(handlers=handlers)

    def test_preflight_refuses_handlers_on_a_start_no_harness_owns(self):
        spec = self.spec(client="fake", mode="tui", name="w1")
        self.session.preflight(spec)
        handlers = (engine_types.HookCallback("pkg.handler"),)
        with self.assertRaisesRegex(
            ConfigError, "^handlers: fake tui on host tmux has no session harness yet$"
        ):
            self.session.preflight(dataclasses.replace(spec, handlers=handlers))
        with self.assertRaisesRegex(ConfigError, "fake headless on host tmux"):
            self.session.preflight(dataclasses.replace(spec, mode="headless", handlers=handlers))


class BoundOwnerTest(HostedTuiBase):
    """attach and retire of a tui session whose session.json names a session harness."""

    def setUp(self):
        super().setUp()
        self.tui(name="w1", sid=SID)
        self.state = tui.TuiMode.snapshot("w1")
        self.channel = Channel.create()
        self.state["owner"] = self.channel.directory
        self.held = None
        with open(os.path.join(self.root, "w1", "session.json"), "w") as f:
            json.dump(self.state, f)

    def owner(self, **status) -> int | None:
        """The lock fd it holds."""
        if not self.channel.alive():
            self.held = os.open(self.channel.lock, os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(self.held, fcntl.LOCK_EX)
            self.addCleanup(self.let_go)
        self.channel.write_status(
            {
                "state": "running",
                "name": "w1",
                "sid": SID,
                "generation": self.state["generation"],
                **status,
            }
        )
        return self.held

    def let_go(self):
        """The harness exits: its lock released."""
        if self.held is not None:
            os.close(self.held)
            self.held = None

    def serve(self, let_go=None, **answer_with):
        """Answers one request; after an ok one it lets go of its lock (`let_go`, the fd owner()
        returned), as a harness does once it answered an end.
        """
        self.served = []
        answer_with = answer_with or {"ok": True}

        def answer():
            deadline = time.monotonic() + WAIT
            while time.monotonic() < deadline:
                for ident, op in self.channel.requests():
                    self.served.append(op)
                    self.channel.reply(ident, **answer_with)
                    if answer_with.get("ok"):
                        self.let_go()
                    return
                time.sleep(0.01)

        thread = threading.Thread(target=answer)
        thread.start()
        self.addCleanup(thread.join)

    def test_attach_of_a_bound_owner_is_its_control_view(self):
        self.owner()
        self.channel.append(
            {
                "seq": 1,
                "id": f"{self.state['generation']}:1",
                "turn": None,
                "type": "TurnEnd",
                "fields": {"reply": "r1", "pending": 0},
            }
        )
        view = self.session.attach("w1", sid=SID)
        self.assertIsInstance(view, OwnedSession)
        self.assertEqual((view.generation, view.reply), (self.state["generation"], "r1"))

    def test_a_control_views_failed_attach_is_made_again(self):
        """WDRC-304: its first attach, held from attach, is not handed out again."""
        self.owner()
        stale, fresh = mock.Mock(name="stale"), mock.Mock(name="fresh")
        stale.send.side_effect = SessionError("tui session w1 is not running")
        made = mock.Mock(side_effect=[stale, fresh])
        with mock.patch.object(self.session, "_attached", made):
            view = self.session.attach("w1", sid=SID)
            view.send("hi")
        fresh.send.assert_called_once_with("hi")
        self.assertEqual(made.call_count, 2)
        self.assertEqual(
            made.call_args, mock.call("w1", sid=SID, host=None, generation=self.state["generation"])
        )

    def test_an_owner_bound_elsewhere_or_gone_is_ignored(self):
        for status in (
            {"name": "w2"},
            {"sid": "other"},
            {"generation": "other"},
            {"state": "failed"},
        ):
            with self.subTest(status=status):
                self.owner(**status)
                with self.assertLogs("session", "WARNING"):
                    found = self.session.attach("w1")
                self.assertNotIsInstance(found, OwnedSession)
        self.assertFalse(os.path.exists(os.path.join(self.channel.directory, "requests")))

    def test_an_exited_owner_is_the_legacy_path_for_attach(self):
        self.owner(state="exited")
        self.assertNotIsInstance(self.session.attach("w1"), OwnedSession)

    def test_attach_of_a_running_owner_whose_terminal_is_gone_raises_as_legacy(self):
        self.owner()
        self.host.processes.pop("w1")
        with self.assertRaisesRegex(SessionError, "w1"):
            self.session.attach("w1")

    def test_retire_of_a_running_owner_whose_terminal_is_gone_asks_it(self):
        held = self.owner()
        self.host.processes.pop("w1")
        with self.assertRaisesRegex(SessionError, "expected sid 'other'"):
            self.session.retire("w1", sid="other")
        self.serve(let_go=held)
        self.session.retire("w1", sid=SID)
        self.assertEqual(self.served, [Op.CLOSE])

    def test_a_failed_spawn_closes_the_worker_its_harness_launched(self):
        terminal = self.host.processes["w1"]
        harness = mock.Mock()
        harness.poll.return_value = None
        self.session._unspawn(self.channel, harness, "w1", None, Mode.TUI)
        harness.kill.assert_called_once_with()
        terminal.calls.close.assert_called_once_with()
        self.assertFalse(os.path.exists(self.channel.directory))
        self.assertFalse(os.path.lexists(os.path.join(self.root, "w1")))

    def test_an_unheld_owner_is_the_legacy_path(self):
        self.assertNotIsInstance(self.session.attach("w1"), OwnedSession)

    def test_a_starting_owner_is_waited_for(self):
        self.owner(state="starting")
        threading.Timer(
            0.2,
            self.channel.write_status,
            (
                {
                    "state": "running",
                    "name": "w1",
                    "sid": SID,
                    "generation": self.state["generation"],
                },
            ),
        ).start()
        self.assertIsInstance(self.session.attach("w1"), OwnedSession)

    def test_attach_with_another_sid_raises_before_the_owner(self):
        self.owner()
        with self.assertRaisesRegex(SessionError, "expected sid 'other'"):
            self.session.attach("w1", sid="other")

    def test_retire_asks_the_owner_and_closes_nothing_itself(self):
        self.serve(let_go=self.owner())
        terminal = self.host.processes["w1"]
        self.session.retire("w1", sid=SID)
        self.assertEqual(self.served, [Op.CLOSE])
        terminal.calls.close.assert_not_called()

    def test_retire_asks_the_owner_even_when_its_terminal_is_gone(self):
        self.owner()
        self.serve()
        self.host.processes.pop("w1")
        self.session.retire("w1", sid=SID)
        self.assertEqual(self.served, [Op.CLOSE])

    def test_an_owner_that_cannot_confirm_the_end_is_unanswered_and_nothing_is_closed(self):
        self.owner()
        self.serve(ok=False, error="w1: worker exit not confirmed", unanswered=True)
        terminal = self.host.processes.pop("w1")
        with self.assertRaisesRegex(Unanswered, "not confirmed") as caught:
            self.session.retire("w1", sid=SID)
        self.assertNotIsInstance(caught.exception, engine_types.OwnerRefused)
        terminal.calls.close.assert_not_called()
        self.assertTrue(os.path.exists(os.path.join(self.root, "w1", "session.json")))

    def test_an_owner_that_refuses_the_close_raises_owner_refused(self):
        self.owner()
        self.serve(ok=False, error="cannot close w1")
        terminal = self.host.processes["w1"]
        with self.assertRaisesRegex(engine_types.OwnerRefused, "cannot close w1"):
            self.session.retire("w1", sid=SID)
        terminal.calls.close.assert_not_called()
        self.assertTrue(os.path.exists(os.path.join(self.root, "w1", "session.json")))

    def test_retire_of_another_sid_never_waits_on_a_starting_owner(self):
        self.owner(state="starting")
        started = time.monotonic()
        with self.assertRaises(SessionError) as caught:
            self.session.retire("w1", sid="other")
        self.assertLess(time.monotonic() - started, 5)
        self.assertNotIsInstance(caught.exception, Unanswered)
        self.assertIn("other", str(caught.exception))
        self.assertTrue(self.host.processes["w1"].alive())

    def test_retire_fallback_takes_the_record_rule(self):
        terminal = self.host.processes["w1"]
        self.host.calls.worker_liveness.return_value = Liveness.UNKNOWN
        self.host.calls.close_view.reset_mock()
        with self.assertRaisesRegex(engine_types.SessionBusy, "worker generation"):
            self.session.retire("w1", sid=SID)
        terminal.calls.close.assert_not_called()
        self.host.calls.close_view.assert_not_called()
        self.assertEqual(tui.TuiMode.snapshot("w1"), self.state)
        self.host.calls.worker_liveness.return_value = Liveness.ABSENT
        self.session.retire("w1", sid=SID)
        terminal.calls.close.assert_called_once_with()
        self.host.calls.close_view.assert_called_once_with("w1")
        self.assertFalse(os.path.lexists(os.path.join(self.root, "w1")))

    def test_retire_of_another_generation_asks_no_owner(self):
        self.owner()
        terminal = self.host.processes["w1"]
        self.session._retire("w1", sid=SID, generation="older")
        self.assertFalse(os.path.exists(os.path.join(self.channel.directory, "requests")))
        terminal.calls.close.assert_not_called()
        self.assertEqual(tui.TuiMode.snapshot("w1"), self.state)

    def test_attached_close_of_an_owned_record_takes_the_record_rule(self):
        session = self.session.attach("w1")
        self.assertNotIsInstance(session, OwnedSession)
        self.host.calls.worker_liveness.return_value = Liveness.UNKNOWN
        with self.assertRaises(engine_types.SessionBusy):
            session.close()
        self.assertEqual(tui.TuiMode.snapshot("w1"), self.state)
        with open(os.path.join(self.root, "w1", "session.json"), "w") as f:
            json.dump({**self.state, "generation": "newer"}, f)
        self.host.calls.close_view.reset_mock()
        session.close()  # another generation's record now: left alone, no wait
        self.host.calls.close_view.assert_not_called()
        self.assertEqual(tui.TuiMode.snapshot("w1")["generation"], "newer")

    def test_retire_with_another_sid_raises_before_any_request(self):
        self.owner()
        with self.assertRaisesRegex(SessionError, "expected sid 'other'"):
            self.session.retire("w1", sid="other")
        self.assertFalse(os.path.exists(os.path.join(self.channel.directory, "requests")))


class DockerOwnedTest(Case):
    """On the Docker Host a Claude tui start and every headless one are harness-owned, a tui
    worker remote there; Codex and OpenCode tui starts keep the legacy path until their hooks are
    verified through the guard.
    """

    def setUp(self):
        self.cls = HostedSession.configured(
            clients={
                "claude": {"implementation": "ensemble.session.clients.claude:ClaudeClient"},
                "codex": {"implementation": "ensemble.session.clients.codex:CodexClient"},
                "opencode": {"implementation": "ensemble.session.clients.opencode:OpenCodeClient"},
            },
            hosts=Host.parse_hosts(
                {
                    "docker": {
                        "implementation": "ensemble.session.hosts.docker.host:DockerHost",
                        "docker": "/usr/local/bin/docker",
                    },
                    "default": "docker",
                }
            ),
        )
        self.setup = self.cls._config()

    def test_handlers_with_an_environment_are_refused_before_any_harness(self):
        workdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, workdir)
        spec = Spec(
            workdir=workdir,
            access=Access(cwd=workdir),
            client="claude",
            mode="headless",
            prompt="hi",
            environment=ENVIRONMENT,
            handlers=(engine_types.HookCallback("pkg.handler"),),
        )
        with (
            mock.patch.object(hosted_session.subprocess, "Popen") as popen,
            self.assertRaisesRegex(
                ConfigError, "claude headless on host docker has no session harness yet"
            ),
        ):
            self.cls.start(spec)
        popen.assert_not_called()

    def test_on_docker_claude_tui_and_headless_are_owned_codex_and_opencode_tui_are_not(self):
        docker = DockerHost("docker", {"docker": "/usr/local/bin/docker"})
        for name, tui_owned in (("claude", True), ("codex", False), ("opencode", False)):
            client = self.setup.registry.client(name)
            with self.subTest(client=name):
                owned = hosted_session._owned(self.setup, PLAIN, Mode.TUI, client, docker)
                self.assertIs(owned, tui_owned)
                self.assertTrue(
                    hosted_session._owned(self.setup, PLAIN, Mode.HEADLESS, client, docker)
                )

    def test_a_remote_tui_host_has_remote_workers(self):
        self.assertTrue(DockerHost._remote_tui)
        self.assertIs(DockerWorker.remote, DockerHost._remote_tui)
        self.assertFalse(TmuxHost._remote_tui)

    def test_owned_is_remote_as_its_mode_says(self):
        for remote in (True, False):
            with self.subTest(remote=remote):
                session = SimpleNamespace(_running=SimpleNamespace(remote=remote))
                self.assertIs(hosted_session.Owned(session, "docker").remote, remote)

    def test_owned_is_up_as_its_mode_says(self):
        for up in (True, False):
            with self.subTest(up=up):
                session = SimpleNamespace(_running=SimpleNamespace(client_up=up))
                self.assertIs(hosted_session.Owned(session, "docker").up, up)
