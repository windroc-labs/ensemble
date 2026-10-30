"""Tui mode behavior through nonspawning Host and InteractiveProcess boundaries."""

import errno
import fcntl
import itertools
import json
import os
import re
import shlex
import shutil
import signal
import stat
import sys
import tempfile
import threading
import time
from dataclasses import replace
from unittest import mock

from ensemble.session.clients.claude import ClaudeClient
from ensemble.session.clients.claude.tui import ClaudeTui
from ensemble.session.clients.codex.tui import CodexTui
from ensemble.session.clients.opencode import OpenCodeClient
from ensemble.session.engine.records import RunLog
from ensemble.session.engine.types import (
    Access,
    Activity,
    Blocked,
    ClientBound,
    ClientContext,
    ClientHome,
    ConfigError,
    Container,
    HookKind,
    Key,
    KeyPress,
    Launch,
    LimitHit,
    Liveness,
    Mode,
    Paste,
    Permission,
    Prepared,
    Reply,
    SessionBusy,
    SessionError,
    SessionInfo,
    Spec,
    TurnEnd,
    TurnFailed,
    TurnStarted,
    Unanswered,
    WorkerExit,
    WorkerHook,
)
from ensemble.session.engine.worker import WorkerState
from ensemble.session.hosts import base as host_base
from ensemble.session.hosts.base import GUARD_WAIT, InteractiveProcess
from ensemble.session.hosts.managed import ManagedProcess
from ensemble.session.modes import tui, tui_hook
from ensemble.session.modes.base import POINTER
from ensemble.utils import ACCOUNT_ENV as ACCOUNT
from ensemble.utils import STALE_AGE, StatusState, inode_lock
from tests.session import fake_client
from tests.session.case import PLUGIN_BIN, SID, Case
from tests.session.fakes.terminal import MemoryHost, ScreenHost
from tests.session.tmux_host_support import Base as TmuxBase
from tests.utils.sandbox import scratch

NAME = "w1"

TS = "2026-10-11T12:00:00.000+00:00"

WORDS = {"stop": "done", "ask": "blocked", "tool": "working"}

BAD_NAMES = ("../x", "a b", "", "a/b", "w1\n", "none")


def record(label: str, **payload) -> dict:
    return {"ts": TS, "label": label, "payload": payload}


class Clock:
    """time.monotonic and time.sleep's stand-in: sleep moves `now`, then runs each action due by
    then; every sleep is logged in `log`.
    """

    def __init__(self, log: list):
        self.now, self.log, self.actions = 0.0, log, []

    def sleep(self, seconds: float) -> None:
        self.log.append(("sleep", seconds))
        self.now += seconds
        while self.actions and self.actions[0][0] <= self.now:
            self.actions.pop(0)[1]()


class ScanTui(fake_client.FakeTui):
    """The fake Tui launching /bin/run with its hook (other Launch fields: `launched`), with two
    dialogs and a usage-limit message `LIMIT <epoch>`; activity is the payload's tool, the blocked
    reason its why; each screen limit() sees is kept.
    """

    dialogs = ("Trust  this\nfolder?", "Sign in")

    def __init__(self, client):
        super().__init__(client)
        self.screens, self.launched = [], {}

    def launch(self, spec, hook):
        return Launch(("/bin/run", *hook), **self.launched)

    def limit(self, screen):
        self.screens.append(screen)
        found = re.search(r"LIMIT ([0-9]+)", screen)
        return LimitHit(float(found[1])) if found else None

    def activity(self, payload):
        return payload.get("tool", "")

    def blocked_reason(self, payload):
        return payload.get("why", "")


class TuiClient(fake_client.FakeClient):
    """The fake Client whose tui() is always its one `run`, a ScanTui unless replaced."""

    def __init__(self, config):
        super().__init__(config)
        self.run = ScanTui(self)

    def tui(self):
        return self.run


class IdentityClient(fake_client.FakeClient):
    identity_env = ("INNER_ID",)


class Restored(ScanTui):
    """A ScanTui logging its restore and hook_events calls in its owner's `calls`."""

    def restore(self, spec, state):
        self.owner.calls.append(("restore", spec, dict(state)))

    def hook_events(self, label, payload):
        self.owner.calls.append(("hook_events", label))
        return super().hook_events(label, payload)


class Seen(ScanTui):
    """A ScanTui keeping the Spec it launched and putting its prompt first in argv."""

    def launch(self, spec, hook):
        self.spec = spec
        return Launch(("/bin/run", spec.prompt, *hook))


class FreshClient(fake_client.FakeClient):
    """The fake Client whose tui() is a new Restored each call, kept in `made`."""

    def __init__(self, config):
        super().__init__(config)
        self.calls, self.made = [], []

    def tui(self):
        self.made.append(Restored(self))
        return self.made[-1]


class Base(Case):
    def setUp(self):
        self.root = os.path.join(self.ensemble, "tui")
        self.dir = os.path.join(self.root, NAME)
        self.hooks = os.path.join(self.dir, "hooks.jsonl")
        self.tmp = scratch(self)
        self.events = os.path.join(self.home, "events")
        self.host = ScreenHost()
        self.log = []
        self.clock = Clock(self.log)
        for patch in (
            mock.patch("time.monotonic", lambda: self.clock.now),
            mock.patch("time.sleep", self.clock.sleep),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.client = TuiClient({"tui": True})
        self.run, self.recorded = self.client.run, []
        self.live = Liveness.LIVE  # a view's worker, as its liveness source says

    def liveness(self) -> Liveness:
        return self.live

    def owner(self, name: str = "a" * 32) -> str:
        """A session harness's dir `name` under HARNESS_ROOT, made if missing."""
        path = os.path.join(self.ensemble, "harness", name)
        os.makedirs(path, 0o700, exist_ok=True)
        return path

    def start(self, run=None, record=None, *, display=None, **fields) -> tui.TuiMode:
        """A TuiMode its owner (the session harness of owner()) started with self.client (its tui
        `run` if given) on Spec(workdir self.tmp, name NAME, client fake, sid SID, events
        self.events, **fields), its Prepared in self.prepared; its session line given to record,
        else kept in self.recorded.
        """
        if run is not None:
            self.client.run = run
        spec = Spec(
            **{
                "workdir": self.tmp,
                "access": Access(cwd=self.tmp),
                "name": NAME,
                "client": "fake",
                "sid": SID,
                "events": self.events,
                **fields,
            }
        )
        mode = tui.TuiMode.start(
            spec,
            self.client,
            registry=self.session_cls._config().registry,
            record=record or self.recorded.append,
            host=self.host,
            display=display,
            owner=self.owner(),
        )
        self.prepared = mode.prepared
        return mode

    def started(self):
        self.host.processes.clear()
        mode = self.start()
        self.host.calls.reset_mock()
        mode._terminal.calls.reset_mock()
        return mode

    def append(self, *lines) -> None:
        """Each line to hooks.jsonl: bytes as given, else a JSON line stamped as the hook does."""
        with open(self.hooks, "ab") as f:
            for line in lines:
                if isinstance(line, dict) and (sent := host_base.sent(self.dir)) is not None:
                    line = {"sent": sent, **line}
                f.write(line if isinstance(line, bytes) else (json.dumps(line) + "\n").encode())

    def state(self, **changes) -> dict:
        process = self.host.processes.get(NAME)
        generation = (
            None if process is None else process.session_identity()[1]
        ) or "fixture-generation"
        return {
            "client": "fake",
            "sid": SID,
            "workdir": self.tmp,
            "cwd": self.tmp,
            "project": False,
            "transcript": "",
            "resume": "",
            "events": self.events,
            "words": WORDS,
            "generation": generation,
            "host": self.host.name,
            "display": None,
            **changes,
        }

    def write_state(self, value) -> None:
        os.makedirs(self.dir, 0o700, exist_ok=True)
        with open(os.path.join(self.dir, "session.json"), "wb") as f:
            f.write(value if isinstance(value, bytes) else json.dumps(value).encode())

    def assertNoSessionDir(self):
        self.assertFalse(os.path.lexists(self.dir))

    def locked(self, name: str = NAME) -> bool:
        try:
            fd = os.open(os.path.join(self.root, f"{name}.lock"), os.O_RDONLY)
        except FileNotFoundError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            os.close(fd)
        return False


class Observing(ScanTui):
    """A ScanTui whose observe() hands out its `records` once."""

    def __init__(self, client):
        super().__init__(client)
        self.records = []

    def observe(self):
        found, self.records = self.records, []
        return found


class ClaudeScreens(ScanTui):
    """A ScanTui reading its screens as ClaudeTui does."""

    dialogs = ClaudeTui.dialogs

    def __init__(self, client):
        super().__init__(client)
        self.claude = ClaudeClient({}).tui()

    def limit(self, screen):
        return self.claude.limit(screen)

    def shows(self, dialog, screen):
        return self.claude.shows(dialog, screen)


class Keys(ScanTui):
    def submit(self, text):
        return Key("Escape"), Paste(text), Key("Enter"), Key("Enter"), Paste("b"), Key("Tab")


class AttachTest(Base):
    def setUp(self):
        super().setUp()
        self.process = self.host.create(NAME, SID)

    def test_opencode_attach_filters_permissions_using_the_hosts_mode(self):
        client = OpenCodeClient({"modes": {"auto": [], "yolo": ["--auto"]}})
        state = self.state(
            client="opencode",
            client_hook={
                "module": "ensemble.session.clients.opencode.tui",
                "state": {"cwd": self.tmp, "store": client.common.store},
                "roots": [],
            },
        )
        self.write_state(state)
        native = "ses_0123456789abABCDEFGHIJKLMN"
        asks = (
            record("permission.asked", session_id=native, permission="custom_tool"),
            record("question.asked", session_id=native, questions=["Database"]),
        )
        self.append(*asks)
        for isolated in (False, True):
            with (
                self.subTest(isolated=isolated),
                mock.patch.object(self.host, "_isolated", new=isolated),
            ):
                mode = tui.TuiMode.attach(
                    NAME, state, client, host=self.host, liveness=self.liveness
                )
                expected = [] if isolated else [Blocked("custom_tool")]
                expected.append(Blocked("question: Database"))
                self.assertEqual(mode.replayed, (ClientBound(native), *expected))
                self.append(*asks)
                self.assertEqual(mode.poll(0), (expected, False))
                os.unlink(self.hooks)
                self.append(*asks)

    def attach(self, *lines, client=None) -> tui.TuiMode:
        """TuiMode.attach of self.client (else `client`) on a session dir with session.json and
        hooks.jsonl's lines, the host's calls cleared first.
        """
        self.write_state(self.state())
        self.append(*lines)
        self.host.calls.reset_mock()
        return tui.TuiMode.attach(
            NAME,
            tui.TuiMode.snapshot(NAME),
            client or self.client,
            host=self.host,
            liveness=self.liveness,
        )

    def test_its_replay_bound_and_the_next_record_from_the_files_end(self):
        partial = (json.dumps(record("tool", tool="next")) + "\n").encode()
        mode = self.attach(
            record("tool", session_id="n1"),
            record("stop", session_id="n1", last_message="r1"),
            record("stop", session_id="n2", last_message="r2"),
            record("ask"),
            b"bad\n",
            partial[:7],
        )
        self.assertEqual(
            (mode.name, mode.prepared.info.sid, mode.prepared.spec.events),
            (NAME, SID, self.events),
        )
        self.assertEqual(
            mode.replayed,
            (
                ClientBound("n1"),
                Activity(""),
                TurnEnd("r1", 0),
                ClientBound("n2"),
                TurnEnd("r2", 0),
                Blocked(""),
            ),
        )
        self.assertEqual(self.host.calls.mock_calls, [])  # it holds no handle of the worker
        self.append(partial[7:], record("stop", session_id="n2", last_message="r3"))
        self.assertEqual(mode.poll(0), ([Activity("next"), TurnEnd("r3", 0)], False))

    def test_client_records_come_before_the_hook_events_attached_and_live(self):
        run = self.client.run = Observing(self.client)
        run.records = [Reply("before")]
        mode = self.attach(record("stop", last_message="r1"))
        self.assertEqual(mode.replayed, (Reply("before"), TurnEnd("r1", 0)))
        run.records = [Reply("late")]
        self.append(record("tool", tool="t"))
        self.assertEqual(mode.poll(0), ([Reply("late"), Activity("t")], False))
        run.records = [Reply("alone")]
        self.assertEqual(mode.poll(0), ([Reply("alone")], False))

    def test_its_prepared_is_session_jsons_spec_and_identity(self):
        mode = self.attach()
        self.assertEqual(
            mode.prepared,
            Prepared(
                Spec(
                    workdir=self.tmp,
                    access=Access(cwd=self.tmp),
                    client="fake",
                    mode=Mode.TUI,
                    sid=SID,
                    name=NAME,
                    events=self.events,
                ),
                SessionInfo(sid=SID, cwd=self.tmp, project=False, transcript="", resume=""),
            ),
        )

    def test_an_identity_is_refused_alike_in_session_json_and_run_jsonl(self):
        """SessionInfo.from_fields decodes both; the hook reads none of these keys."""
        valid = SessionInfo(
            sid=SID, cwd=self.tmp, project=False, transcript="", resume=""
        ).persisted()
        for field, value in (
            ("cwd", ""),
            ("cwd", "rel"),
            ("transcript", "rel"),
            ("project", "no"),
            ("resume", None),
        ):
            with self.subTest(field=field, value=value):
                RunLog(self.tmp).note("session", **{**valid, field: value})
                self.assertIsNone(RunLog(self.tmp).session(SID))
                self.write_state(self.state(**{field: value}))
                path = os.path.join(self.dir, "session.json")
                for call in (
                    lambda: tui.TuiMode.snapshot(NAME),
                    lambda *, field=field, value=value: tui.TuiMode.attach(
                        NAME,
                        self.state(**{field: value}),
                        self.client,
                        host=self.host,
                        liveness=self.liveness,
                    ),
                ):
                    with self.assertRaises(SessionError) as caught:
                        call()
                    self.assertEqual(
                        str(caught.exception), f"no tui session w1: {path}: bad {field}"
                    )
        self.assertEqual(self.host.calls.mock_calls, [])

    def test_no_records_yet(self):
        mode = self.attach()
        self.assertEqual(mode.replayed, ())
        self.append(record("stop", session_id="n1"))
        self.assertEqual(mode.poll(0), ([ClientBound("n1"), TurnEnd("", 0)], False))

    def test_its_records_replay_as_the_events_they_give_live(self):
        lines = (
            record("tool", session_id="n1", tool="t"),
            record("ask", why="perm"),
            record("other", session_id="n2"),
            record("stop", session_id="n2", last_message="r", tasks=[1]),
            b"bad\n",
            record("stop", last_message=""),
        )
        live = self.started()
        self.append(*lines)
        polled, _ = live.poll(0)
        fresh = FreshClient({"tui": True})
        attached = tui.TuiMode.attach(
            NAME, tui.TuiMode.snapshot(NAME), fresh, host=self.host, liveness=self.liveness
        )
        self.assertEqual(attached.replayed, tuple(polled))
        self.assertEqual(len(polled), 6)

    def test_a_fresh_tui_is_restored_once_with_its_client_state_before_its_records(self):
        restored = {"module": "fake_client_hook", "state": {"store": "/s"}, "roots": []}
        for extra, kept in (({"client_hook": restored}, {"store": "/s"}), ({}, {})):
            with self.subTest(extra=extra):
                client = FreshClient({"tui": True})
                self.write_state(self.state(**extra))
                self.append(record("tool"), record("stop"))
                mode = tui.TuiMode.attach(
                    NAME, tui.TuiMode.snapshot(NAME), client, host=self.host, liveness=self.liveness
                )
                self.assertEqual(client.made, [mode.run])
                self.assertEqual(
                    client.calls,
                    [
                        ("restore", mode.prepared.spec, kept),
                        ("hook_events", "tool"),
                        ("hook_events", "stop"),
                    ],
                )
                os.unlink(self.hooks)

    def test_a_tui_it_cannot_restore_or_no_tui_mode_is_no_tui_session(self):
        class Unrestorable(ScanTui):
            def restore(self, spec, state):
                raise ConfigError("no recovery state")

        self.client.run = Unrestorable(self.client)
        with self.assertRaises(SessionError) as caught:
            self.attach(record("tool"))
        self.assertEqual(str(caught.exception), "no tui session w1: no recovery state")
        self.assertEqual(self.host.calls.mock_calls, [])
        self.client.run = None
        with self.assertRaises(SessionError) as caught:
            self.attach()
        self.assertEqual(str(caught.exception), "no tui session w1: client 'fake' has no tui mode")
        self.assertEqual(self.host.calls.mock_calls, [])

    def test_marked_blocked_is_its_workers_activity(self):
        mode = self.attach()
        activity = os.path.join(self.dir, "activity")
        for line, blocked in (
            (f"{mode.generation} blocked\n", True),
            (f"{mode.generation} working\n", False),
            ("another blocked\n", False),
            (None, False),
        ):
            with self.subTest(line=line):
                if line is None:
                    os.unlink(activity)
                else:
                    with open(activity, "w") as f:
                        f.write(line)
                self.assertIs(mode.marked_blocked(), blocked)

    def test_another_sid_is_no_tui_session(self):
        self.write_state(self.state())
        with self.assertRaisesRegex(SessionError, f"expected sid 'other', found '{SID}'"):
            tui.TuiMode.attach(
                NAME,
                tui.TuiMode.snapshot(NAME),
                self.client,
                host=self.host,
                liveness=self.liveness,
                sid="other",
            )

    def test_an_attached_session_is_live_and_never_marks_or_reads_a_screen(self):
        """Its owner alone marks and scans; a view only reads its records, and has no terminal:
        its input and screen are its harness's.
        """
        mode = self.attach()
        self.host.calls.screen.return_value = "Sign in"
        self.assertEqual(mode.poll(0), ([], False))
        self.host.calls.screen.assert_not_called()
        for call in (lambda: mode.send("hi"), lambda: mode.press(()), mode.screen):
            with self.assertRaisesRegex(SessionError, "session w1: its harness is gone"):
                call()
        self.assertFalse(os.path.exists(os.path.join(self.dir, "activity")))
        self.assertFalse(os.path.exists(self.events) and os.path.getsize(self.events))
        mode.stop()  # no owner named: no worker to end, only its dir
        self.process.calls.close.assert_not_called()
        self.assertNoSessionDir()

    def test_an_attached_session_ends_once_its_liveness_says_absent(self):
        mode = self.attach()
        self.live = Liveness.UNKNOWN
        self.assertEqual((mode.state(), mode.poll(0)), (Liveness.UNKNOWN, ([], False)))
        self.live = Liveness.ABSENT
        self.assertEqual(mode.poll(0), ([], True))
        self.assertEqual((self.run.screens, mode.returncode(0)), ([], -signal.SIGHUP))

    def test_an_attached_session_ends_once_session_json_is_another_generations(self):
        mode = self.attach()
        self.write_state(self.state(generation="another"))
        self.append(record("tool"))
        self.assertEqual(mode.poll(0), ([], True))
        self.assertEqual(mode.returncode(0), -signal.SIGHUP)


class StartTest(Base):
    def test_failed_client_commit_removes_its_tui_state_without_launching(self):
        class Conflict(ScanTui):
            def commit(self):
                raise ConfigError("commit conflict")

        with self.assertRaisesRegex(ConfigError, "commit conflict"):
            self.start(run=Conflict(self.client))
        self.assertNoSessionDir()
        self.assertEqual(self.recorded, [])
        self.host.calls.worker.assert_not_called()

    def test_without_ensemble_session_on_path_nothing_starts(self):
        with (
            mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}),
            self.assertRaisesRegex(
                ConfigError, f"tui session {NAME}: ensemble-session is not on PATH"
            ),
        ):
            self.start()
        self.nothing_hosted()

    def nothing_hosted(self):
        self.host.calls.worker.assert_not_called()
        self.assertFalse(os.path.lexists(self.root))
        self.assertEqual(self.recorded, [])

    def test_display_and_private_identity_reach_the_hosts_worker(self):
        mode = self.start(events=None, display="opaque-location")
        call = self.host.calls.worker.call_args
        self.assertEqual(
            (call.kwargs["display"], call.kwargs["terminal"]), ("opaque-location", True)
        )
        self.assertEqual(call.kwargs["metadata"]["sid"], SID)
        self.assertEqual(call.kwargs["metadata"]["client"], "fake")
        self.assertEqual(mode._terminal.display(), "opaque-location")

    def test_the_selected_host_receives_the_launch_and_identity_receipt(self):
        mode = self.start()
        call = self.host.calls.worker.call_args
        self.assertEqual(
            call.args,
            (
                NAME,
                Launch(
                    ("/bin/run", *tui.TuiStore.attach(NAME, host=self.host).hook_argv()),
                    cwd=self.tmp,
                    env=self.prepared.env,
                ),
            ),
        )
        self.assertEqual(call.kwargs["events"], self.events)
        self.assertEqual(call.kwargs["metadata"]["sid"], SID)
        self.assertEqual(call.kwargs["metadata"]["client"], "fake")
        self.assertEqual(
            (mode.name, mode.prepared.info.sid, mode._store.dir), (NAME, SID, self.dir)
        )
        self.assertEqual(stat.S_IMODE(os.stat(self.events).st_mode), 0o600)
        mode.stop()
        self.start(events=None)
        self.assertIsNone(self.host.calls.worker.call_args.kwargs["events"])

    def test_one_session_line_between_session_json_and_terminal_launch_under_the_names_lock(self):
        lines = []

        def record(info):
            lines.append(
                (
                    info,
                    tui.TuiMode.snapshot(NAME)["sid"],
                    self.host.calls.worker.called,
                    self.locked(),
                )
            )

        self.start(record=record)
        self.assertEqual(lines, [(self.prepared.info, SID, False, True)])
        self.host.calls.worker.assert_called_once()
        self.assertEqual(
            self.prepared.info,
            SessionInfo(sid=SID, cwd=self.tmp, project=False, transcript="", resume=""),
        )

    def test_the_names_lock_is_held_from_session_dir_to_terminal_launch(self):
        held = []

        def launch(*_args, **_kwargs):
            held.append(self.locked())
            if len(held) > 1:
                raise SessionError("host launch failed")

        self.host.calls.worker.side_effect = launch
        self.start()
        with self.assertRaisesRegex(SessionError, "host launch failed"):
            self.start()
        self.assertEqual((held, self.locked()), ([True, True], False))

    def test_the_spec_as_started_names_the_session(self):
        self.start()
        self.assertEqual(self.prepared.spec.name, NAME)
        mode = self.start(name=None, events=None)
        self.assertEqual((mode.name, self.prepared.spec.name), ("fake-0f8fad5b", "fake-0f8fad5b"))
        self.assertEqual(self.host.calls.worker.call_args.args[0], "fake-0f8fad5b")

    def test_host_client_env_reaches_launch_and_names_the_home_in_metadata(self):
        class Homed(ScanTui):
            def _home(self):
                return ClientHome(
                    "fake_dir", "fake", "FAKE_HOME", "fake-token", (("settings.json", "{}"),)
                )

            def launch(self, spec, hook):
                self.spec = spec
                return super().launch(spec, hook)

        run, calls = Homed(self.client), []
        run.launched = {"transcript": "/home/fake/projects/p/s.jsonl"}

        def client_env(name, client, home, *, resume):
            calls.append((name, client, home, resume))
            return {"FAKE_HOME": "/home/fake"}

        for resume, flag in ((False, "0"), (True, "1")):
            with self.subTest(resume=resume):
                calls.clear()
                self.host.processes.clear()
                with mock.patch.object(self.host, "_client_env", client_env):
                    self.start(run=run, env={"OTHER": "1"}, resume=resume)
                self.assertEqual(
                    calls,
                    [
                        (
                            NAME,
                            "fake",
                            run._home(),
                            resume,
                        )
                    ],
                )
                self.assertEqual(dict(run.spec.env), {"OTHER": "1", "FAKE_HOME": "/home/fake"})
                args, kwargs = self.host.calls.worker.call_args
                metadata = kwargs["metadata"]
                self.assertEqual(
                    (metadata["home_field"], metadata["home_dir"], metadata["resume"]),
                    ("fake_dir", "fake", flag),
                )
                self.assertEqual(metadata["credential"], "fake-token")
                self.assertEqual(json.loads(metadata["home_files"]), {"settings.json": "{}"})
                self.assertNotIn("approval", metadata)
                self.assertEqual(args[1].transcript, "/home/fake/projects/p/s.jsonl")

    def test_a_workers_saved_account_names_the_home_on_each_start_never_the_launch(self):
        class Homed(ScanTui):
            def _home(self):
                return ClientHome("claude_dir", "claude", "FAKE_HOME", "claude-setup-token")

            def launch(self, spec, hook):
                self.spec = spec
                return super().launch(spec, hook)

        run = Homed(self.client)
        saved = WorkerState(
            NAME, SID, "fake", self.tmp, "tmux", env={ACCOUNT: "work"}, events=self.events
        )
        for resume, (env, account) in itertools.product(
            (False, True), (({ACCOUNT: "work"}, "work"), ({}, None))
        ):
            with (
                self.subTest(resume=resume, env=env),
                mock.patch.object(self.client, "_accounts", return_value={"default", "work"}),
                mock.patch.object(self.host, "_client_env", return_value={"FAKE_HOME": "/h"}),
                mock.patch.dict(os.environ, {ACCOUNT: "outer"}),
            ):
                self.host.processes.clear()
                spec = replace(saved, env=env).spec(prompt="", resume=resume)
                self.start(
                    run=run, **{k: getattr(spec, k) for k in ("workdir", "access", "env", "resume")}
                )
                launch = self.host.calls.worker.call_args.args[1]
                metadata = self.host.calls.worker.call_args.kwargs["metadata"]
                self.assertEqual(metadata.get("home_account"), account)
                self.assertEqual(dict(run.spec.env), {"FAKE_HOME": "/h"})
                self.assertNotIn(ACCOUNT, launch.env)
                self.assertNotIn(ACCOUNT, self.prepared.spec.env)
        self.assertEqual(saved.env[ACCOUNT], "work")

    def test_host_context_reaches_adapter_and_projection_reaches_terminal(self):
        context = ClientContext(
            "host.docker.internal",
            Permission.YOLO,
            Container("/image/bin", "/image/python", "/image/modules", "/run/state"),
        )
        projection = {"/run/state/store/record.json": '{"native_id":"kept"}'}
        seen = []

        class Projected(ScanTui):
            def _home(self):
                return ClientHome(
                    "fake_dir", "fake", "FAKE_HOME", "fake-token", persistent=("sessions",)
                )

            def launch(self, spec, hook):
                seen.append(self._context)
                self._runtime_files = projection
                return super().launch(spec, hook)

        with (
            mock.patch.object(self.host, "_client_context", return_value=context),
            mock.patch.object(self.host, "_client_env", return_value={"FAKE_HOME": "/volatile"}),
        ):
            self.start(run=Projected(self.client))
        self.assertEqual(len(seen), 1)
        self.assertIs(seen[0], context)
        metadata = self.host.calls.worker.call_args.kwargs["metadata"]
        self.assertEqual(json.loads(metadata["runtime_files"]), projection)
        self.assertEqual(json.loads(metadata["home_persistent"]), ["sessions"])

    def test_the_run_gets_yolo_on_an_isolated_host_else_auto(self):
        seen = []

        class Seen(ScanTui):
            def launch(self, spec, hook):
                seen.append(self._context.permission)
                return super().launch(spec, hook)

        self.start(run=Seen(self.client))
        self.host.processes.clear()
        with mock.patch.object(self.host, "_isolated", new=True):
            self.start(run=Seen(self.client))
        self.assertEqual(seen, [Permission.AUTO, Permission.YOLO])

    def test_a_host_resume_argv_replaces_the_clients_resume_command(self):
        run = ScanTui(self.client)
        run.launched = {"resume": "cd /w && ensemble-session client-resume fake /w s"}
        lines = []
        self.start(run=run, record=lines.append)
        self.assertEqual(lines[-1].resume, run.launched["resume"])
        argv = ("ensemble-session", "resume", NAME)
        self.host.processes.clear()
        with mock.patch.object(self.host, "_resume_argv", return_value=argv):
            self.start(run=run, record=lines.append)
        wanted = f"cd {shlex.quote(self.tmp)} && ensemble-session resume {NAME}"
        self.assertEqual((lines[-1].resume, tui.TuiMode.snapshot(NAME)["resume"]), (wanted, wanted))

    def test_a_host_that_inherits_no_env_launches_with_only_its_launch_env(self):
        self.host._inherit_env = False
        with mock.patch.dict(os.environ, {"HOST_ONLY": "1"}):
            self.start()
        launch = self.host.calls.worker.call_args.args[1]
        self.assertNotIn("HOST_ONLY", launch.env)
        self.assertEqual(launch.env["PWD"], launch.cwd)

    def test_a_host_without_client_env_leaves_the_spec_and_metadata_alone(self):
        self.assertEqual(self.host._client_env(NAME, "fake", None, resume=True), {})
        self.start(env={"OTHER": "1"})
        self.assertEqual(dict(self.prepared.spec.env), {"OTHER": "1"})
        metadata = self.host.calls.worker.call_args.kwargs["metadata"]
        self.assertEqual(set(metadata), {"sid", "client", "generation", "owner"})

    def test_cwd_and_grants_over_the_hook_paths_launch(self):
        for hook_path, field in itertools.product((self.events, self.root), ("cwd", "dirs")):
            for path in (hook_path, os.path.dirname(hook_path), os.path.join(hook_path, "child")):
                access = Access(cwd=path) if field == "cwd" else Access(cwd=self.tmp, dirs=(path,))
                with self.subTest(path=path, field=field):
                    self.host.processes.clear()
                    self.start(access=access)
                    self.assertEqual(self.prepared.spec.access, access)
        self.assertEqual(self.host.calls.worker.call_count, 12)

    def test_its_child_env_drops_client_identities_then_takes_the_launch_env(self):
        self.session_dependencies = {"class_loader": lambda _path, _base: IdentityClient}
        self.configure_session(
            {"inner": {"implementation": "ensemble.session.clients.inner:InnerClient"}}
        )
        self.run.launched = {"env": {"LAUNCH_SET": "1"}, "unset": ("LAUNCH_DROP",)}
        with mock.patch.dict(
            os.environ,
            {
                "HOME": os.environ["HOME"],
                "PATH": PLUGIN_BIN,
                "INNER_ID": "i",
                "LAUNCH_DROP": "x",
                "KEEP": "k",
            },
            clear=True,
        ):
            self.start()
        self.assertEqual(
            self.host.calls.worker.call_args.args[1].env,
            {
                "HOME": os.environ["HOME"],
                "PATH": PLUGIN_BIN,
                "KEEP": "k",
                "LAUNCH_SET": "1",
                "PWD": self.tmp,
                "ENSEMBLE_AGENT_SESSION": "1",
                "ENSEMBLE_CLIENT": "fake",
            },
        )

    def test_a_bad_name_given_or_made_from_the_sid_is_refused_before_host_launch_or_a_path(self):
        for name in BAD_NAMES:
            with self.subTest(name=name), self.assertRaises(ConfigError) as caught:
                self.start(name=name)
            self.assertEqual(
                str(caught.exception),
                f"name must match [A-Za-z0-9_-]+ but not none, got {name!r:.80}",
            )
        for fields, message in (
            (
                {"name": None, "sid": "a b"},
                "name must match [A-Za-z0-9_-]+ but not none, got 'fake-a b'",
            ),
            ({"name": 5}, "name must be None or a string"),
        ):
            with self.subTest(fields=fields), self.assertRaises(ConfigError) as caught:
                self.start(**fields)
            self.assertEqual(str(caught.exception), message)
        self.assertEqual(self.host.calls.mock_calls, [])
        self.nothing_hosted()

    def test_a_tui_store_refuses_a_bad_name_before_the_host_or_a_path(self):
        for name in (*BAD_NAMES, None):
            with self.subTest(name=name), self.assertRaises(ConfigError) as caught:
                tui.TuiStore.attach(name, host=self.host).close()
            self.assertEqual(
                str(caught.exception),
                f"name must match [A-Za-z0-9_-]+ but not none, got {name!r:.80}",
            )
        self.assertEqual(self.host.calls.mock_calls, [])
        self.nothing_hosted()

    def test_a_bad_events_file_is_refused(self):
        self.host.calls.validate.side_effect = ConfigError("events destination refused")
        with self.assertRaisesRegex(ConfigError, "events destination refused"):
            self.start()
        self.host.calls.validate.assert_called_once()
        self.nothing_hosted()

    def test_an_unavailable_host_is_a_driver_error_with_nothing_started(self):
        failure = SessionError("host unavailable")
        self.host.calls.validate.side_effect = failure
        with self.assertRaisesRegex(
            SessionError, rf"\Acannot start tui session {NAME}: host unavailable\Z"
        ) as caught:
            self.start(events=None)
        self.assertIs(caught.exception.__cause__, failure)
        self.host.calls.validate.assert_called_once()
        self.nothing_hosted()

    def test_an_unsupported_tui_mode_is_refused_before_anything(self):
        self.client.run = None
        with self.assertRaisesRegex(ConfigError, "client 'fake' does not support tui mode"):
            self.start()
        self.assertEqual(self.host.calls.mock_calls, [])
        self.nothing_hosted()

    def test_session_selection_flags_are_refused_before_launch(self):
        self.client.forbidden_selection = ("--resume",)
        with self.assertRaises(ConfigError) as caught:
            self.start(flags=("--resume", SID))
        self.assertEqual(str(caught.exception), "--resume: picks the session; use sid= or resume=")
        self.nothing_hosted()

    def test_a_failed_host_start_is_a_driver_error_after_the_session_line_removing_only_its_dir(
        self,
    ):
        os.makedirs(os.path.join(self.root, "w2"))
        self.host.calls.worker.side_effect = SessionError("tmux: boom")
        with self.assertRaises(SessionError) as caught:
            self.start()
        self.assertEqual(str(caught.exception), "cannot start tui session w1: tmux: boom")
        self.assertEqual(len(self.recorded), 1)
        self.assertNoSessionDir()
        self.assertEqual(sorted(os.listdir(self.root)), ["w1.lock", "w2"])

    def test_a_failed_start_keeps_a_dir_it_did_not_make(self):
        def swap(_info):
            os.rename(self.dir, os.path.join(self.tmp, "ours"))  # kept: no inode reuse
            os.mkdir(self.dir)
            open(os.path.join(self.dir, "theirs"), "w").close()

        self.host.calls.worker.side_effect = SessionError("tmux: boom")
        with self.assertRaises(SessionError):
            self.start(record=swap)
        self.assertEqual(os.listdir(self.dir), ["theirs"])

    def test_a_failed_record_or_a_start_failing_without_a_driver_error_leaves_its_dir(self):
        def fail(_info):
            raise OSError("disk full")

        for record, failure in ((fail, None), (None, ValueError("boom"))):
            with self.subTest(failure=failure):
                self.host.calls.reset_mock()
                self.host.calls.worker.side_effect = failure
                with self.assertRaises(OSError if failure is None else ValueError):
                    self.start(record=record)
                self.assertEqual(os.listdir(self.dir), ["session.json"])
                self.assertEqual(
                    len(self.host.calls.worker.mock_calls), 0 if failure is None else 1
                )
                self.assertFalse(self.locked())

    def test_terminal_acquisition_failure_does_not_wait_for_its_held_lock(self):
        caught = []
        self.host.calls.worker.side_effect = SessionError("terminal could not be acquired")

        def start():
            try:
                self.start()
            except SessionError as error:
                caught.append(str(error))

        thread = threading.Thread(target=start, daemon=True)
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(caught, ["cannot start tui session w1: terminal could not be acquired"])
        self.assertEqual(len(self.recorded), 1)
        self.assertEqual(self.host.processes, {})
        self.assertNoSessionDir()

    def test_the_names_lock_is_the_file_its_path_names_when_held(self):
        store, path = (
            tui.TuiStore.attach(NAME, host=self.host),
            os.path.join(self.root, f"{NAME}.lock"),
        )
        real_flock, calls = fcntl.flock, []

        def flock(fd, operation):
            if not calls:
                os.unlink(path)
                open(path, "w").close()
            calls.append(fd)
            return real_flock(fd, operation)

        with mock.patch("fcntl.flock", side_effect=flock), store._locked():
            self.assertTrue(self.locked())
        self.assertFalse(self.locked())

    def test_the_names_lock_reports_its_own_failure_and_passes_its_bodys(self):
        store, path = (
            tui.TuiStore.attach(NAME, host=self.host),
            os.path.join(self.root, f"{NAME}.lock"),
        )
        with self.assertRaises(ValueError), store._locked():
            raise ValueError("body")
        os.unlink(path)
        os.mkdir(path)
        with self.assertRaisesRegex(SessionError, f"^{re.escape(path)}: "), store._locked():
            self.fail("locked a directory")

    def test_an_absent_terminal_keeps_the_exit_code_it_confirmed(self):
        mode = self.start(events=None)
        mode._terminal.calls.state.return_value = Liveness.ABSENT
        mode._terminal.calls.returncode.return_value = 7
        self.assertEqual((mode.poll(0), mode.returncode(0)), (([], True), 7))

    def test_start_returns_its_session_live_at_once_with_its_prepared(self):
        mode = self.start(events=None)
        self.assertIsInstance(mode, tui.TuiMode)
        self.assertEqual((mode.prepared.info, mode.prepared.spec.name), (self.recorded[0], NAME))
        self.assertEqual((mode.state(), mode.poll(0)), (Liveness.LIVE, ([], False)))
        process = mode._terminal
        mode.stop()
        process.calls.close.assert_called_once_with()
        self.assertEqual((mode.poll(0), mode.returncode(0)), (([], True), -signal.SIGHUP))


DAY = 24 * 3600


class SweepTest(Base):
    """Other names' leftovers swept by a start of w1."""

    def leftover(self, name, *, days=31, host=None, folder=True):
        """tui/<name>/ (folder) with session.json naming `host` (else this host) and hooks.jsonl, and
        tui/<name>.lock, all `days` old.
        """
        paths = [os.path.join(self.root, f"{name}.lock")]
        if folder:
            os.makedirs(os.path.join(self.root, name))
            paths += [os.path.join(self.root, name, f) for f in ("session.json", "hooks.jsonl")]
        os.makedirs(self.root, exist_ok=True)
        for path in paths:
            with open(path, "w", encoding="utf-8") as file:
                file.write(json.dumps({"host": host or self.host.name}))
        self.age(days, *paths, *([os.path.join(self.root, name)] if folder else []))

    def age(self, days, *paths):
        moment = time.time() - days * DAY
        for path in paths:
            os.utime(path, (moment, moment), follow_symlinks=False)

    def names(self):
        return sorted(os.listdir(self.root))

    def test_an_idle_name_without_a_session_goes_with_its_lock(self):
        self.leftover("a")
        self.leftover("b", folder=False)
        self.leftover("c")
        with open(os.path.join(self.root, "c", "session.json"), "w", encoding="utf-8") as file:
            file.write("{")
        self.age(31, os.path.join(self.root, "c", "session.json"))
        self.start()
        self.assertEqual(self.names(), [NAME, f"{NAME}.lock"])

    def test_a_name_recent_held_listed_or_of_another_host_stays(self):
        self.leftover("young", days=29)
        self.leftover("held")
        self.leftover("listed")
        self.leftover("other", host="elsewhere")
        self.host.create("listed")
        with inode_lock(os.path.join(self.root, "held.lock")):
            self.start()
        kept = ["held", "listed", "other", "young"]
        self.assertEqual(
            self.names(), sorted([*kept, *(f"{n}.lock" for n in kept), NAME, f"{NAME}.lock"])
        )

    def test_nothing_idle_lists_no_sessions_for_the_sweep(self):
        self.leftover("young", days=29)
        self.start()
        self.host.calls.list.assert_not_called()

    def test_a_symlinked_name_and_its_target_stay(self):
        self.leftover("a")
        target = os.path.join(self.tmp, "target")
        os.rename(os.path.join(self.root, "a"), target)
        os.symlink(target, os.path.join(self.root, "a"))
        self.age(31, os.path.join(self.root, "a"))
        self.start()
        self.assertEqual(self.names(), ["a", "a.lock", NAME, f"{NAME}.lock"])
        self.assertTrue(os.path.exists(os.path.join(target, "hooks.jsonl")))

    def test_a_failure_on_one_name_is_logged_and_the_rest_swept(self):
        self.leftover("a")
        self.leftover("b")
        real_unlink = os.unlink

        def unlink(path, *args, **kwargs):
            if str(path).endswith("a.lock"):
                raise PermissionError("denied")
            return real_unlink(path, *args, **kwargs)

        with mock.patch("os.unlink", side_effect=unlink), self.assertLogs("session") as logs:
            self.start()
        self.assertEqual(self.names(), ["a.lock", NAME, f"{NAME}.lock"])
        self.assertEqual(logs.output, ["WARNING:session:tui sweep a: denied"])

    def test_a_name_that_cannot_be_aged_is_logged_and_the_rest_swept(self):
        self.leftover("a")
        self.leftover("b")
        real_lstat = os.lstat

        def lstat(path, *args, **kwargs):
            if str(path) == os.path.join(self.root, "a", "hooks.jsonl"):
                raise PermissionError("denied")
            return real_lstat(path, *args, **kwargs)

        with mock.patch("os.lstat", side_effect=lstat), self.assertLogs("session") as logs:
            self.start()
        self.assertEqual(self.names(), ["a", "a.lock", NAME, f"{NAME}.lock"])
        self.assertEqual(logs.output, ["WARNING:session:tui sweep a: denied"])

    def test_the_starting_name_is_never_swept(self):
        def launched(*_args, **_kwargs):
            self.age(31, self.dir, *(os.path.join(self.dir, f) for f in os.listdir(self.dir)))
            self.age(31, os.path.join(self.root, f"{NAME}.lock"))

        self.host.calls.worker.side_effect = launched
        self.host.calls.list.return_value = []
        self.start()
        self.assertEqual(self.names(), [NAME, f"{NAME}.lock"])
        self.assertEqual(os.listdir(self.dir), ["session.json"])

    def test_a_failed_listing_is_logged_once_and_the_start_stands(self):
        self.leftover("a")
        self.leftover("b")
        for failure in (SessionError("tmux failed"), PermissionError("denied")):
            with self.subTest(failure=failure):
                self.host.calls.list.side_effect = failure
                with self.assertLogs("session") as logs:
                    mode = self.start()
                self.host.processes.clear()
                self.assertIsInstance(mode, tui.TuiMode)
                self.assertEqual(self.names(), ["a", "a.lock", "b", "b.lock", NAME, f"{NAME}.lock"])
                self.assertEqual(logs.output, [f"WARNING:session:tui sweep: {failure}"])


class SnapshotTest(Base):
    def test_a_valid_state(self):
        self.start()
        self.assertEqual(tui.TuiMode.snapshot(NAME), self.state(owner=self.owner()))
        self.write_state(
            self.state(events=None, transcript="/t", project=True, resume="r", words={}, later=1)
        )
        self.assertEqual(tui.TuiMode.snapshot(NAME)["later"], 1)

    def test_a_client_is_lowercase_word_characters_led_by_a_letter(self):
        for client, ok in (
            ("a", True),
            ("a_1", True),
            ("z9_", True),
            ("", False),
            ("A", False),
            ("1a", False),
            ("_a", False),
            ("a-b", False),
            ("a\n", False),
            (1, False),
        ):
            with self.subTest(client=client):
                self.write_state(self.state(client=client))
                if ok:
                    self.assertEqual(tui.TuiMode.snapshot(NAME)["client"], client)
                else:
                    with self.assertRaises(SessionError) as caught:
                        tui.TuiMode.snapshot(NAME)
                    path = os.path.join(self.dir, "session.json")
                    self.assertEqual(
                        str(caught.exception), f"no tui session w1: {path}: bad client"
                    )

    def test_missing_or_invalid_is_a_driver_error(self):
        with self.assertRaisesRegex(SessionError, r"\Ano tui session w1: "):
            tui.TuiMode.snapshot(NAME)
        bad = {
            "words": {"stop": "dead"},
            "events": "rel",
            "sid": "a/b",
            "client": "Fake",
            "workdir": "rel",
            "generation": "",
        }
        for field, value in bad.items():
            for state in (
                self.state(**{field: value}),
                {k: v for k, v in self.state().items() if k != field},
            ):
                with self.subTest(field=field, missing=field not in state):
                    self.write_state(state)
                    with self.assertRaisesRegex(
                        SessionError, rf"\Ano tui session w1: .*bad {field}"
                    ):
                        tui.TuiMode.snapshot(NAME)
        self.write_state({k: v for k, v in self.state().items() if k not in ("cwd", "project")})
        with self.assertRaises(SessionError) as caught:
            tui.TuiMode.snapshot(NAME)
        path = os.path.join(self.dir, "session.json")
        self.assertEqual(str(caught.exception), f"no tui session w1: {path}: bad cwd")
        for data in (b"", b"[]", b"not json"):
            with self.subTest(data):
                self.write_state(data)
                with self.assertRaisesRegex(SessionError, r"\Ano tui session w1: "):
                    tui.TuiMode.snapshot(NAME)

    def test_a_session_json_that_is_no_regular_file_is_refused_without_blocking(self):
        path = os.path.join(self.dir, "session.json")
        os.makedirs(self.dir)
        for make in (
            lambda: os.mkfifo(path),
            lambda: os.mkdir(path),
        ):
            make()
            with self.subTest(stat.filemode(os.lstat(path).st_mode)):
                out = {}
                thread = threading.Thread(
                    target=lambda *, out=out: out.setdefault("error", self.refused()), daemon=True
                )
                thread.start()
                thread.join(10)
                self.assertFalse(thread.is_alive(), "blocked")
                self.assertTrue(out["error"])
            (os.rmdir if stat.S_ISDIR(os.lstat(path).st_mode) else os.unlink)(path)

    def refused(self) -> bool:
        try:
            tui.TuiMode.snapshot(NAME)
        except SessionError:
            return True
        return False


class BoundTest(Base):
    """An owned mode's handle alone ends it."""

    def test_stop_ends_it_at_once_its_code_the_one_seen_else_sighup(self):
        mode = self.started()
        mode.stop()
        os.mkdir(self.dir)
        self.append(record("tool"))
        self.host.calls.reset_mock()
        self.assertEqual((mode.poll(5), mode.returncode(5)), (([], True), -signal.SIGHUP))
        self.assertEqual((self.host.calls.mock_calls, self.log), ([], []))
        mode = self.started()
        mode._terminal.calls.returncode.return_value = 3
        self.assertEqual(mode.poll(0), ([], True))
        mode.stop()
        self.assertEqual(mode.returncode(0), 3)

    def test_stop_closes_its_handle_under_the_names_lock(self):
        mode = self.started()
        held = []
        mode._terminal.calls.close.side_effect = lambda: held.append(self.locked())
        mode.stop()
        self.assertEqual((held, self.locked()), ([True], False))
        self.assertNoSessionDir()

    def test_an_absent_handle_is_not_scanned_and_stop_still_closes_it(self):
        mode = self.started()
        self.host.processes.clear()
        self.assertEqual(mode.poll(0), ([], True))
        mode.stop()
        self.host.calls.screen.assert_not_called()
        mode._terminal.calls.close.assert_called_once_with()
        self.assertNoSessionDir()

    def test_an_unknown_terminal_is_not_scanned_or_ended(self):
        mode = self.started()
        mode._terminal.calls.state.return_value = Liveness.UNKNOWN
        self.host.calls.screen.return_value = "Sign in"
        self.assertEqual(mode.poll(0), ([], False))
        self.assertEqual(
            (self.run.screens, mode.returncode(0), mode.state()), ([], None, Liveness.UNKNOWN)
        )


class SwapTest(Base):
    """Operations on a captured InteractiveProcess never reach its replacement."""

    def test_send_never_types_into_the_replacement_terminal(self):
        mode = self.started()
        original = mode._terminal

        def replace():
            self.host.create(NAME, SID)
            return Liveness.LIVE

        original.calls.state.side_effect = replace
        with self.assertRaisesRegex(SessionError, "terminal w1 is not running"):
            mode.send("hi")
        self.host.processes[NAME].calls.send.assert_not_called()
        original.calls.send.assert_not_called()

    def test_screen_never_reads_the_replacement_terminal(self):
        mode = self.started()

        def replace():
            self.host.create(NAME, SID)
            return Liveness.LIVE

        mode._terminal.calls.state.side_effect = replace
        with self.assertRaises(SessionError):
            mode.screen()
        self.host.calls.screen.assert_not_called()

    def test_stop_never_kills_the_new_session(self):
        mode = self.started()
        original = mode._terminal

        def swap():
            self.host.create(NAME, SID)

        original.calls.close.side_effect = swap
        mode.stop()
        self.assertIsNot(self.host.processes[NAME], original)
        self.host.processes[NAME].calls.close.assert_not_called()
        self.assertNoSessionDir()


class SendScreenTest(Base):
    def test_a_nonshowable_selected_host_runs_without_screen_scanning(self):
        self.host = MemoryHost("selected")
        mode = self.started()
        self.assertIs(mode.host, self.host)
        self.assertEqual(mode.poll(0), ([], False))
        self.append(record("stop", last_message="reply"))
        self.assertEqual(mode.poll(0), ([TurnEnd("reply", 0)], False))
        mode.send("hello")
        self.assertEqual(
            mode._terminal.calls.send.call_args_list,
            [mock.call(Paste("hello")), mock.call(Key("Enter"))],
        )
        with self.assertRaisesRegex(SessionError, r"\Ahost selected cannot show\Z"):
            mode.screen()
        self.assertNotIn("screen", [call[0] for call in self.host.calls.mock_calls])

    def test_send_preserves_the_client_actions_at_the_terminal_boundary(self):
        mode = self.started()
        mode.send("hi\nthere")
        self.assertEqual(
            mode._terminal.calls.send.call_args_list,
            [mock.call(Paste("hi\nthere")), mock.call(Key("Enter"))],
        )
        mode._terminal.calls.send.reset_mock()
        mode.run = Keys(self.run.client)
        mode.send("a")
        self.assertEqual(
            mode._terminal.calls.send.call_args_list,
            [
                mock.call(Key("Escape")),
                mock.call(Paste("a")),
                mock.call(Key("Enter")),
                mock.call(Key("Enter")),
                mock.call(Paste("b")),
                mock.call(Key("Tab")),
            ],
        )
        self.assertEqual(self.log, [])

    def test_send_or_screen_to_a_session_not_running_is_refused(self):
        mode = self.started()
        mode._terminal.calls.state.return_value = Liveness.ABSENT
        for call in (lambda: mode.send("hi"), mode.screen):
            with self.assertRaisesRegex(SessionError, r"\Atui session w1 is not running\Z"):
                call()
        mode._terminal.calls.send.assert_not_called()
        self.host.calls.screen.assert_not_called()

    def test_terminal_and_display_errors_propagate(self):
        mode = self.started()
        for method, call in (
            (mode._terminal.calls.send, lambda: mode.send("hi")),
            (self.host.calls.screen, mode.screen),
        ):
            with self.subTest(method=method):
                method.side_effect = SessionError("host operation failed")
                with self.assertRaisesRegex(SessionError, r"\Ahost operation failed\Z"):
                    call()
                method.side_effect = None

    def test_screen_uses_the_selected_showable_host(self):
        mode = self.started()
        self.host.calls.screen.return_value = "a\nb"
        self.assertEqual((mode.screen(), mode.screen(5)), ("a\nb", "a\nb"))
        self.assertEqual(
            self.host.calls.screen.call_args_list,
            [mock.call(mode._terminal, None), mock.call(mode._terminal, 5)],
        )


class CloseStopTest(Base):
    def test_close_kills_the_session_and_removes_its_dir(self):
        mode = self.started()
        self.append(record("tool"))
        mode.close()
        mode._terminal.calls.close.assert_called_once_with()
        self.assertEqual(self.host.processes, {})
        self.assertNoSessionDir()
        self.assertTrue(os.path.isdir(self.root))

    def test_close_kills_and_removes_under_the_names_lock(self):
        mode = self.started()
        held, remove = [], tempfile.TemporaryDirectory.cleanup
        mode._terminal.calls.close.side_effect = lambda: held.append(self.locked())
        with mock.patch.object(
            tempfile.TemporaryDirectory,
            "cleanup",
            lambda retired: (held.append(self.locked()), remove(retired)),
        ):
            tui.TuiStore.attach(NAME, host=self.host).close()
        self.assertEqual((held, self.locked()), ([True, True], False))
        self.assertNoSessionDir()

    def test_close_without_an_owned_record_removes_only_its_dir(self):
        """No owner named: no worker to end; a host session of its name is not its to close."""
        for state in (None, self.state()):
            with self.subTest(state=state):
                terminal = mock.create_autospec(InteractiveProcess, instance=True)
                self.host.processes[NAME] = terminal
                if state is not None:
                    self.write_state(state)
                tui.TuiStore.attach(NAME, host=self.host).close()
                terminal.close.assert_not_called()
                self.assertNoSessionDir()

    def test_close_of_an_absent_session_removes_its_dir(self):
        mode = self.started()
        self.host.processes.clear()
        tui.TuiStore.attach(NAME, host=self.host).close()
        mode._terminal.calls.close.assert_not_called()
        self.assertNoSessionDir()
        tui.TuiStore.attach(NAME, host=self.host).close()

    def test_a_kill_that_fails_on_a_live_or_unknown_session_is_a_driver_error(self):
        mode = self.started()
        mode._terminal.calls.close.side_effect = SessionError("host close failed")
        for state in (Liveness.LIVE, Liveness.UNKNOWN):
            with self.subTest(state=state):
                mode._terminal.calls.state.return_value = state
                with self.assertRaisesRegex(
                    SessionError, r"\Acannot close tui session w1: host close failed\Z"
                ):
                    tui.TuiStore.attach(NAME, host=self.host).close()
                self.assertTrue(os.path.isdir(self.dir))

    def test_close_never_follows_a_symlink(self):
        os.mkdir(self.root, 0o700)
        target = os.path.join(self.tmp, "target")
        os.mkdir(target)
        open(os.path.join(target, "keep"), "w").close()
        for make in (lambda: os.symlink(target, self.dir), lambda: open(self.dir, "w").close()):
            make()
            tui.TuiStore.attach(NAME, host=self.host).close()
            self.assertTrue(os.path.lexists(self.dir))
            os.unlink(self.dir)
        self.assertEqual(os.listdir(target), ["keep"])

    def test_stop_closes_the_session(self):
        mode = self.start()
        mode.stop()
        mode._terminal.calls.close.assert_called_once_with()
        self.assertNoSessionDir()

    def test_a_stop_whose_display_lookup_fails_still_closes_its_handle(self):
        mode = self.started()
        self.host.calls.list.side_effect = SessionError("tmux sessions unknown: failed")
        mode.stop()
        mode._terminal.calls.close.assert_called_once_with()
        self.assertNoSessionDir()
        self.assertFalse(os.path.lexists(os.path.join(self.root, f"{NAME}.lock")))
        self.assertEqual((mode.poll(0), mode.returncode(0)), (([], True), -signal.SIGHUP))

    def test_close_and_stop_remove_the_names_lock(self):
        lock = os.path.join(self.root, f"{NAME}.lock")
        self.started()
        tui.TuiStore.attach(NAME, host=self.host).close()
        self.assertFalse(os.path.lexists(lock))
        self.started().stop()
        self.assertFalse(os.path.lexists(lock))
        mode = self.started()
        self.host.processes.clear()
        mode.stop()
        self.assertFalse(os.path.lexists(lock))

    def test_a_failed_close_keeps_the_names_lock(self):
        mode = self.started()
        mode._terminal.calls.close.side_effect = SessionError("host close failed")
        with self.assertRaises(SessionError):
            tui.TuiStore.attach(NAME, host=self.host).close()
        self.assertTrue(os.path.exists(os.path.join(self.root, f"{NAME}.lock")))


class PollTest(Base):
    def test_accepted_turn_without_terminal_fails_before_exit(self):
        mode = self.started()
        self.run.hooks = {**self.run.hooks, "start": HookKind.TURN_STARTED}
        self.append(record("start"))
        self.assertEqual(mode.poll(0), ([TurnStarted()], False))
        mode._terminal.calls.returncode.return_value = -9
        self.assertEqual(mode.poll(0), ([TurnFailed("process_exit", "worker exited -9")], True))
        self.assertEqual(mode.poll(0), ([], True))

    def test_records_become_the_runs_events_in_order_bound_once(self):
        mode = self.started()
        self.append(
            record("tool", session_id="n1", tool="Bash"),
            record("stop", session_id="n1", last_message="hi", tasks=[1, 2]),
        )
        self.assertEqual(
            mode.poll(0.5), ([ClientBound("n1"), Activity("Bash"), TurnEnd("hi", 2)], False)
        )
        self.append(
            record("ask", session_id="n2", why="perm"),
            record("other", session_id="n2"),
            record("tool"),
        )
        self.assertEqual(
            mode.poll(0.5), ([ClientBound("n2"), Blocked("perm"), Activity("")], False)
        )
        self.assertFalse(hasattr(mode, "bound"))
        self.assertEqual(self.host.calls.mock_calls, [])
        self.assertEqual(self.log, [])

    def test_a_partial_last_line_waits_for_its_end(self):
        mode = self.started()
        line = (json.dumps(record("stop", last_message="r")) + "\n").encode()
        self.append(record("tool"), line[:10])
        self.assertEqual(mode.poll(0), ([Activity("")], False))
        self.assertEqual(mode.poll(0), ([], False))
        self.append(line[10:])
        self.assertEqual(mode.poll(0), ([TurnEnd("r", 0)], False))

    def test_bad_lines_are_skipped(self):
        mode = self.started()
        self.append(
            b"not json\n",
            b"[1]\n",
            b'{"label": 1, "payload": {}}\n',
            b'{"label": "stop", "payload": []}\n',
            b'{"label": "stop"}\n',
            b"\xff\xfe\n",
            b"\n",
            b"[" * 100000 + b"\n",
            record("tool", tool="t"),
        )
        self.assertEqual(mode.poll(0), ([Activity("t")], False))

    def test_a_hooks_file_that_is_no_regular_file_gives_no_records(self):
        mode = self.started()
        os.mkfifo(self.hooks)
        self.assertEqual(mode.poll(0), ([], False))

    def test_empty_poll_checks_the_terminal_and_waits_in_slices(self):
        mode = self.started()
        self.assertEqual(mode.poll(0.5), ([], False))
        self.assertEqual(self.host.calls.mock_calls, [mock.call.screen(mode._terminal, None)])
        mode._terminal.calls.returncode.assert_called_once_with()
        self.assertEqual(
            {call[0] for call in mode._terminal.calls.mock_calls}, {"read", "state", "returncode"}
        )
        self.assertTrue(all(0 < seconds <= 0.1 for _, seconds in self.log), self.log)
        self.assertAlmostEqual(self.clock.now, 0.5)

    def test_a_quiet_wait_checks_the_terminal_once_per_poll_however_long(self):
        for timeout in (0.5, 5):
            with self.subTest(timeout=timeout):
                mode = self.started()
                self.assertEqual(mode.poll(timeout), ([], False))
                self.assertEqual(
                    self.host.calls.mock_calls, [mock.call.screen(mode._terminal, None)]
                )
                self.assertEqual(
                    sorted(call[0] for call in mode._terminal.calls.mock_calls),
                    ["read", "returncode", "state"],
                )
        mode = self.started()
        late = self.clock.now + 2.05
        self.clock.actions.append((late, lambda: self.append(record("tool", tool="late"))))
        self.assertEqual(mode.poll(5), ([Activity("late")], False))
        self.assertEqual(mode._terminal.calls.state.call_count, 1)

    def test_a_record_while_waiting_returns_at_once(self):
        mode = self.started()
        self.clock.actions.append((0.25, lambda: self.append(record("tool", tool="late"))))
        self.assertEqual(mode.poll(5), ([Activity("late")], False))
        self.assertAlmostEqual(self.clock.now, 0.3)

    def test_dead_or_absent_drains_the_records_then_ends(self):
        for code in (3, -15, 0, -signal.SIGHUP):
            with self.subTest(code=code):
                mode = self.started()
                process = mode._terminal

                def ended(*, code=code):
                    self.append(record("stop", last_message="last"))
                    if code == -signal.SIGHUP:
                        self.host.processes.clear()
                    return code

                process.calls.returncode.side_effect = ended
                self.assertEqual(mode.poll(0.5), ([TurnEnd("last", 0)], True))
                self.host.calls.screen.assert_not_called()
                self.assertEqual(mode.returncode(1), code)

    def test_unknown_keeps_waiting_with_no_scan(self):
        mode = self.started()
        mode._terminal.calls.state.return_value = Liveness.UNKNOWN
        self.assertEqual(mode.poll(0.2), ([], False))
        self.host.calls.screen.assert_not_called()

    def test_exact_events_per_poll_over_a_session(self):
        mode = self.started()
        self.host.calls.screen.side_effect = ["", "Sign in", "Sign in", "x", "LIMIT 9", "x"]
        script = [
            [record("tool", session_id="n1")],
            [],
            [],
            [record("ask", session_id="n1")],
            [],
            [],
            [record("stop", session_id="n1", last_message="done")],
        ]
        got = []
        for lines in script:
            self.append(*lines)
            got.append(mode.poll(0.1))
        self.assertEqual(
            got,
            [
                ([ClientBound("n1"), Activity("")], False),
                ([], False),
                ([Blocked("Sign in")], False),
                ([Blocked("")], False),
                ([], False),
                ([Activity("")], False),
                ([TurnEnd("done", 0)], False),
            ],
        )
        self.assertEqual(mode.poll(0.1), ([LimitHit(9.0)], False))
        self.assertEqual(mode.poll(0.1), ([], False))


class SendLockTest(Base):
    """Sends from two threads (its session harness's handlers, say) share its one terminal."""

    def setUp(self):
        super().setUp()
        self.mode = self.other = self.started()
        self.calls = self.mode._terminal.calls
        self.order = []
        self.calls.send.side_effect = lambda action: self.order.append(action)
        self.calls.press.side_effect = lambda keys: self.order.append(keys)

    def racing(self, first, second, arrived) -> None:
        """second runs on a thread started from first's Paste, which then waits up to 0.5 s for
        arrived (set once second reaches the terminal: Paste("two") sets it).
        """
        threads = []

        def send(action):
            self.order.append(action)
            if action == Paste("two"):
                arrived.set()
            if action == Paste("one") and not threads:
                threads.append(threading.Thread(target=second))
                threads[0].start()
                arrived.wait(0.5)

        self.calls.send.side_effect = send
        first()
        threads[0].join(5)

    def test_two_concurrent_senders_never_interleave(self):
        arrived = threading.Event()
        self.racing(lambda: self.mode.send("one"), lambda: self.other.send("two"), arrived)
        self.assertEqual(self.order, [Paste("one"), Key("Enter"), Paste("two"), Key("Enter")])

    def test_a_press_waits_for_a_send_in_progress(self):
        arrived = threading.Event()

        def press(keys):
            self.order.append(keys)
            arrived.set()

        self.calls.press.side_effect = press
        escape = KeyPress.parse("Escape")
        self.racing(lambda: self.mode.send("one"), lambda: self.other.press([escape]), arrived)
        self.assertEqual(self.order, [Paste("one"), Key("Enter"), (escape,)])

    def test_no_lock_dir_refuses_and_sends_nothing(self):
        shutil.rmtree(self.dir)
        for call in (lambda: self.mode.send("one"), lambda: self.mode.press([KeyPress.parse("a")])):
            with self.assertRaisesRegex(SessionError, f"tui session {NAME} is not running"):
                call()
        self.assertEqual(self.order, [])

    def test_a_failure_on_the_paste_is_a_session_error(self):
        self.calls.send.side_effect = SessionError("gone")
        with self.assertRaises(SessionError) as raised:
            self.mode.send("one")
        self.assertNotIsInstance(raised.exception, Unanswered)
        store = tui.TuiStore.attach(NAME, host=self.host)
        self.assertEqual(store.prompts(self.mode.generation), (0, 0))

    def test_a_failure_after_the_paste_is_unanswered(self):
        self.calls.send.side_effect = [None, SessionError("gone")]
        with self.assertRaisesRegex(Unanswered, f"send to tui session {NAME}: partly sent: gone"):
            self.mode.send("one")
        store = tui.TuiStore.attach(NAME, host=self.host)
        self.assertEqual(store.prompts(self.mode.generation), (1, 0))

    def test_each_send_counts_a_prompt_of_its_generation(self):
        store = tui.TuiStore.attach(NAME, host=self.host)
        self.assertEqual(store.prompts(self.mode.generation), (0, 0))
        self.other.send("one")
        self.mode.send("two")
        self.assertEqual(store.prompts(self.mode.generation), (2, 2))
        self.assertEqual(store.prompts("another"), (0, 0))
        self.mode.press([KeyPress.parse("a")])
        self.assertEqual(self.order[-1], (KeyPress.parse("a"),))


class Opening(ScanTui):
    opening = ("ready", "/color")


class StartupTui(ScanTui):
    _ready = "ready"
    _interrupt = Key("Escape")
    _starts_turn = CodexTui._starts_turn
    hooks = {
        **ScanTui.hooks,
        "start": HookKind.TURN_STARTED,
        "interrupted": HookKind.TURN_END,
    }


class StartupSendTest(Base):
    def test_cancellation_before_paste_sends_no_input_and_allows_the_next_send(self):
        mode = self.started_with(prompt="")
        self.ready(turn=False)
        pending = iter((True, False))
        self.assertFalse(mode._try_send("canceled", lambda: next(pending)))
        self.assertEqual(self.sent(), [])
        self.assertEqual(mode._store.prompts(mode.generation), (0, 0))
        self.assertTrue(mode.send("next"))
        self.assertEqual(self.sent(), [Paste("next"), Key("Enter")])

    def test_each_wait_reports_its_gate_and_cancellation_stays_distinct(self):
        mode = self.started_with()
        self.assertEqual(mode._try_send("next", lambda: True), "client startup")
        self.ready(turn=False)
        self.assertEqual(mode._try_send("next", lambda: True), "turn acceptance")
        self.append(record("start", turn_id="first"))
        mode.poll(0)
        self.assertEqual(mode._try_send("next", lambda: True), "turn interruption")
        self.assertEqual(mode._try_send("next", lambda: True), "turn interruption")
        self.assertFalse(mode._try_send("canceled", lambda: False))
        self.assertEqual(self.sent(), [Key("Escape")])

    def test_a_codex_send_does_not_block_polling_while_the_send_lock_is_held(self):
        mode = self.started_with()
        with mode._store.sending():
            self.assertEqual(mode._try_send("next", lambda: True), "send lock")
        self.assertEqual(self.sent(), [])

    def test_cancellation_after_paste_cannot_submit_enter(self):
        mode = self.started_with(prompt="")
        self.ready(turn=False)
        pending = [True]

        def pasted(action):
            if isinstance(action, Paste):
                pending[0] = False

        self.terminal.calls.send.side_effect = pasted
        with self.assertRaisesRegex(Unanswered, "partly sent.*canceled"):
            mode._try_send("canceled", lambda: pending[0])
        self.assertEqual(self.sent(), [Paste("canceled")])
        self.assertEqual(mode._store.prompts(mode.generation), (1, 0))

    def test_expiry_after_paste_is_partial_and_cannot_submit_enter(self):
        mode = self.started_with(prompt="")
        self.ready(turn=False)
        deadline = self.clock.now + 0.5

        def pasted(action):
            if isinstance(action, Paste):
                self.clock.now = deadline

        def pending():
            if self.clock.now >= deadline:
                raise SessionError("blocked by request deadline")
            return True

        self.terminal.calls.send.side_effect = pasted
        with self.assertRaisesRegex(Unanswered, "partly sent.*request deadline"):
            mode._try_send("expired", pending)
        self.assertEqual(self.sent(), [Paste("expired")])
        self.assertEqual(mode._store.prompts(mode.generation), (1, 0))

    def test_a_rejected_plan_command_cannot_hold_the_next_send(self):
        mode = self.started_with(prompt="")
        self.ready(turn=False)
        self.assertTrue(mode.send("/plan implement this"))
        mode.poll(0)
        self.assertTrue(mode.send("implement without plan"))
        self.assertEqual(self.sent()[-2:], [Paste("implement without plan"), Key("Enter")])

    def test_cancellation_during_the_press_lock_wait_prevents_key_input(self):
        mode = self.started_with()
        pending = [True]
        flock = fcntl.flock

        def after_wait(fd, how):
            if how & fcntl.LOCK_EX:
                pending[0] = False
            return flock(fd, how)

        with mock.patch("fcntl.flock", side_effect=after_wait):
            mode._try_press([KeyPress.parse("Enter")], lambda: pending[0])
        self.terminal.calls.press.assert_not_called()

    def test_a_superseding_turn_is_interrupted_again(self):
        mode = self.started_with()
        self.ready()
        self.assertFalse(mode.send("next"))
        self.append(record("start", turn_id="manual"), record("interrupted", turn_id="first"))
        self.assertEqual(mode.poll(0), ([TurnStarted()], False))
        self.assertFalse(mode.send("next"))
        self.assertEqual(self.sent(), [Key("Escape"), Key("Escape")])

    def test_cancellation_during_the_send_lock_wait_prevents_all_input(self):
        mode = self.started_with()
        self.ready()
        pending = [True]
        flock = fcntl.flock

        def after_wait(fd, how):
            if how & fcntl.LOCK_EX:
                pending[0] = False
            return flock(fd, how)

        with mock.patch("fcntl.flock", side_effect=after_wait):
            self.assertFalse(mode._try_send("canceled", lambda: pending[0]))
        self.assertEqual(self.sent(), [])
        self.assertEqual(mode._store.prompts(mode.generation), (0, 0))

    def test_local_commands_and_empty_sends_do_not_block_the_next_message(self):
        mode = self.started_with(prompt="")
        self.ready(turn=False)
        for text in ("", " \n ", "/status"):
            self.assertTrue(mode.send(text))
        self.assertTrue(mode.send("next"))
        self.assertEqual(self.sent()[-2:], [Paste("next"), Key("Enter")])

    def started_with(self, *, prompt="opening"):
        run = StartupTui(self.client)
        self.mode = self.start(run, prompt=prompt)
        self.terminal = self.mode._terminal
        self.terminal.calls.send.reset_mock()
        return self.mode

    def sent(self):
        return [call.args[0] for call in self.terminal.calls.send.call_args_list]

    def ready(self, *, turn=True):
        self.append(record("ready"))
        if turn:
            self.append(record("start", turn_id="first"))
        self.mode.poll(0)
        self.mode.poll(tui.OPEN_AFTER + 0.1)

    def test_startup_send_interrupts_the_first_turn_then_starts_the_next(self):
        mode = self.started_with()
        self.assertFalse(mode.send("follow-up"))
        self.assertEqual(self.sent(), [])
        self.assertEqual(mode._store.prompts(mode.generation), (0, 0))
        self.append(record("start", turn_id="first"))
        self.assertEqual(mode.poll(0), ([TurnStarted()], False))
        self.assertFalse(mode.send("follow-up"))
        self.append(record("ready"))
        mode.poll(0)
        self.assertFalse(mode.send("follow-up"))
        self.assertEqual(self.sent(), [])
        mode.poll(tui.OPEN_AFTER + 0.1)
        self.assertFalse(mode.send("follow-up"))
        self.assertFalse(mode.send("follow-up"))
        self.assertEqual(self.sent(), [Key("Escape")])
        self.assertEqual(mode._store.prompts(mode.generation), (0, 0))
        self.append(record("interrupted", turn_id="first"))
        self.assertEqual(mode.poll(0), ([TurnEnd()], False))

        def receive(action):
            if action == Key("Enter"):
                self.append(record("start", turn_id="next"))

        self.terminal.calls.send.side_effect = receive
        self.assertTrue(mode.send("follow-up"))
        self.assertEqual(self.sent(), [Key("Escape"), Paste("follow-up"), Key("Enter")])
        self.assertEqual(mode._store.prompts(mode.generation), (1, 1))
        self.assertEqual(mode.poll(0), ([TurnStarted()], False))
        self.append(record("stop", turn_id="first", last_message="late"))
        self.assertEqual(mode.poll(0), ([], False))
        self.assertFalse(mode.send("another"))
        self.assertEqual(self.sent()[-1], Key("Escape"))

    def test_readiness_does_not_mistake_an_unobserved_launch_turn_for_idle(self):
        mode = self.started_with()
        self.ready(turn=False)
        self.assertFalse(mode.send("next"))
        self.assertEqual(self.sent(), [])
        self.append(record("start", turn_id="first"))
        mode.poll(0)
        self.assertFalse(mode.send("next"))
        self.assertEqual(self.sent(), [Key("Escape")])

    def test_idle_send_submits_without_escape_and_the_next_send_waits_for_acceptance(self):
        mode = self.started_with(prompt="")
        self.ready(turn=False)
        self.assertTrue(mode.send("first"))
        self.assertFalse(mode.send("second"))
        self.assertEqual(self.sent(), [Paste("first"), Key("Enter")])
        self.append(record("start", turn_id="first"))
        mode.poll(0)
        self.assertFalse(mode.send("second"))
        self.assertEqual(self.sent()[-1], Key("Escape"))

    def test_a_natural_end_winning_the_interrupt_race_allows_submission(self):
        mode = self.started_with()
        self.ready()
        self.assertFalse(mode.send("next"))
        self.append(record("stop", turn_id="first", last_message="finished"))
        self.assertEqual(mode.poll(0), ([TurnEnd("finished")], False))
        self.assertTrue(mode.send("next"))
        self.assertEqual(self.sent(), [Key("Escape"), Paste("next"), Key("Enter")])

    def test_an_interrupt_failure_is_refused_without_a_prompt_marker(self):
        mode = self.started_with()
        self.ready()
        self.terminal.calls.send.side_effect = SessionError("gone")
        with self.assertRaisesRegex(SessionError, "gone"):
            mode.send("next")
        self.assertEqual(mode._store.prompts(mode.generation), (0, 0))

    def test_a_dead_client_is_refused_even_before_readiness(self):
        mode = self.started_with()
        self.terminal.calls.state.return_value = Liveness.ABSENT
        with self.assertRaisesRegex(SessionError, "not running"):
            mode.send("next")
        self.assertEqual(self.sent(), [])


class PromptOpening(Opening):
    _opening_prompt = True


class MidTurnOpening(Opening):
    _opening_mid_turn = True


class OpeningTest(Base):
    def opened(self) -> tui.TuiMode:
        self.client.run = Opening(self.client)
        mode = self.started()
        self.sent_at = []
        mode._terminal.calls.send.side_effect = lambda _action: self.sent_at.append(self.clock.now)
        return mode

    def sends(self, mode) -> list:
        return [c.args[0] for c in mode._terminal.calls.send.call_args_list]

    def test_sends_once_after_delay(self):
        mode = self.opened()
        self.assertEqual(mode.opening_pending, "/color")
        mode.poll(2)
        self.assertEqual(self.sends(mode), [])
        self.append(record("ready"))
        self.assertEqual(mode.poll(0), ([], False))
        due = self.clock.now + tui.OPEN_AFTER
        mode.poll(0.5)
        self.assertEqual(self.sends(mode), [])
        self.assertEqual(mode.opening_pending, "/color")
        mode.poll(0.6)
        self.assertEqual(self.sends(mode), [Paste("/color"), Key("Enter")])
        self.assertGreaterEqual(self.sent_at[0], due)
        self.assertIsNone(mode.opening_pending)
        self.append(record("ready"))
        mode.poll(2)
        self.assertEqual(len(self.sends(mode)), 2)

    def test_later_trigger_restarts_wait(self):
        mode = self.opened()
        self.append(record("ready"))
        mode.poll(0.8)
        self.append(record("ready"))
        mode.poll(0.5)
        self.assertEqual(self.sends(mode), [])
        mode.poll(0.6)
        self.assertEqual(self.sends(mode), [Paste("/color"), Key("Enter")])

    def test_held_while_dialog_shows(self):
        mode = self.opened()
        self.host.calls.screen.return_value = "Sign in"
        self.append(record("ready"))
        mode.poll(0)
        for _ in range(4):
            mode.poll(0.5)
        self.assertEqual(self.sends(mode), [])
        self.assertEqual(mode.opening_pending, "/color")
        self.host.calls.screen.return_value = ""
        mode.poll(0.6)
        self.assertEqual(self.sends(mode), [Paste("/color"), Key("Enter")])

    def test_a_dialog_shown_while_records_arrive_still_holds(self):
        mode = self.opened()
        self.append(record("ready"))
        mode.poll(0)
        self.host.calls.screen.return_value = "Trust this folder?"
        self.append(record("tool"))
        mode.poll(0)
        mode.poll(1.5)
        self.assertEqual(self.sends(mode), [])

    def test_a_dialog_on_the_fresh_read_holds_the_opening(self):
        mode = self.opened()
        fresh = [False]

        def state():
            fresh[0] = True
            return Liveness.LIVE

        def screen(_terminal, _lines):  # a cached capture until state() renews it
            shown, fresh[0] = ("Trust this folder?" if fresh[0] else ""), False
            return shown

        mode._terminal.calls.state.side_effect = state
        self.host.calls.screen.side_effect = screen
        self.append(record("ready"))
        mode.poll(0)
        mode.poll(1.2)
        self.assertEqual(self.sends(mode), [])
        self.assertEqual(mode.opening_pending, "/color")
        self.host.calls.screen.side_effect = None
        self.host.calls.screen.return_value = ""
        mode.poll(0.6)
        self.assertEqual(self.sends(mode), [Paste("/color"), Key("Enter")])

    def test_a_process_not_live_at_the_confirm_or_a_failed_screen_read_holds_the_opening(self):
        for target, attribute, value in (
            (lambda mode: mode._terminal.calls.state, "return_value", Liveness.UNKNOWN),
            (lambda _mode: self.host.calls.screen, "side_effect", SessionError("tmux: gone")),
        ):
            with self.subTest(attribute):
                mode = self.opened()
                setattr(target(mode), attribute, value)
                self.append(record("ready"))
                mode.poll(0)
                mode.poll(1.2)
                self.assertEqual(self.sends(mode), [])
                self.assertEqual(mode.opening_pending, "/color")
                os.unlink(self.hooks)

    def test_held_while_marked_blocked(self):
        mode = self.opened()
        self.append(record("ready"), record("ask", why="perm"))
        mode.poll(0)
        mode.poll(2)
        self.assertEqual(self.sends(mode), [])
        self.append(record("tool"))
        mode.poll(0)
        mode.poll(0.2)
        self.assertEqual(self.sends(mode), [Paste("/color"), Key("Enter")])

    def test_attached_never_sends(self):
        mode = self.opened()
        attached = tui.TuiMode.attach(
            NAME, tui.TuiMode.snapshot(NAME), self.client, host=self.host, liveness=self.liveness
        )
        self.assertIsNone(attached.opening_pending)
        self.append(record("ready"))
        attached.poll(0)
        attached.poll(2)
        self.assertEqual(self.sends(mode), [])

    def test_send_error_clears_pending(self):
        mode = self.opened()
        mode._terminal.calls.send.side_effect = SessionError("gone")
        self.append(record("ready"))
        mode.poll(0)
        mode.poll(1.2)
        self.assertIsNone(mode.opening_pending)
        mode._terminal.calls.send.side_effect = None
        self.append(record("ready"))
        mode.poll(2)
        self.assertEqual(len(self.sends(mode)), 1)

    def test_a_host_whose_terminals_cannot_show_still_sends_the_opening(self):
        self.host = MemoryHost("selected")
        mode = self.opened()
        self.append(record("ready"))
        mode.poll(0)
        mode.poll(1.2)
        self.assertEqual(self.sends(mode), [Paste("/color"), Key("Enter")])
        self.assertIsNone(mode.opening_pending)

    def test_no_opening_is_never_pending(self):
        mode = self.started()
        self.assertIsNone(mode.opening_pending)
        self.append(record("ready"))
        mode.poll(2)
        mode._terminal.calls.send.assert_not_called()

    def test_an_ended_mode_is_not_pending(self):
        mode = self.opened()
        mode.stop()
        self.assertIsNone(mode.opening_pending)

    def launched(self, run=None, **fields) -> tui.TuiMode:
        """opened(), its Tui (default Opening) also reporting `start` as TurnStarted."""
        self.client.run = run or Opening(self.client)
        self.client.run.hooks = {**self.client.run.hooks, "start": HookKind.TURN_STARTED}
        self.host.processes.clear()
        mode = self.mode = self.start(**fields)
        mode._terminal.calls.reset_mock()
        return mode

    def other(self) -> tui.TuiMode:
        """Another sender: its owner serving a send request on a handler's thread."""
        return self.mode

    def test_another_senders_prompt_defers_the_opening_past_its_turn(self):
        mode = self.launched()
        self.other().send("hi")
        self.append(record("ready"))
        mode.poll(0)
        mode.poll(1.2)  # its turn not read yet: the opening would land in it
        self.assertEqual(self.sends(mode), [Paste("hi"), Key("Enter")])
        self.assertEqual(mode.opening_pending, "/color")
        self.append(record("start"))
        mode.poll(0)
        mode.poll(1.2)
        self.assertEqual(len(self.sends(mode)), 2)
        self.append(record("stop"))
        mode.poll(0)
        mode.poll(0.6)
        self.assertEqual(self.sends(mode)[2:], [Paste("/color"), Key("Enter")])
        self.assertIsNone(mode.opening_pending)

    def test_a_turn_defers_the_opening_until_it_ends(self):
        mode = self.launched()
        self.append(record("ready"), record("start"))
        mode.poll(0)
        mode.poll(1.2)
        self.assertEqual(self.sends(mode), [])
        self.assertEqual(mode.opening_pending, "/color")
        self.append(record("stop"))
        mode.poll(0)
        mode.poll(0.6)
        self.assertEqual(self.sends(mode), [Paste("/color"), Key("Enter")])

    def test_a_prompted_start_ends_with_its_color_set(self):
        mode = self.launched(prompt="go")
        self.append(record("ready"))
        mode.poll(0)
        mode.poll(1.2)  # its launch turn runs, its TurnStarted not read yet
        self.assertEqual(self.sends(mode), [])
        self.append(record("start"))
        mode.poll(0)
        mode.poll(1.2)
        self.assertEqual(self.sends(mode), [])
        self.append(record("stop"))
        mode.poll(0)
        mode.poll(0.6)
        self.assertEqual(self.sends(mode), [Paste("/color"), Key("Enter")])
        self.assertIsNone(mode.opening_pending)

    def test_a_send_joining_a_running_turn_does_not_hold_the_opening(self):
        mode = self.launched(prompt="go")
        self.append(record("ready"), record("start"))
        mode.poll(0)
        self.other().send("also")  # joins the running turn: no TurnStarted of its own
        self.append(record("stop"))
        mode.poll(0)
        mode.poll(1.2)
        self.assertEqual(self.sends(mode)[2:], [Paste("/color"), Key("Enter")])
        self.assertIsNone(mode.opening_pending)

    def test_a_send_after_an_unread_turn_end_waits_for_its_own_turn(self):
        mode = self.launched(prompt="go")
        self.append(record("ready"), record("start"))
        mode.poll(0)
        self.append(record("stop"))
        self.other().send("next")  # after the turn end was written, before it is read
        mode.poll(0)
        mode.poll(1.2)
        self.assertEqual(self.sends(mode), [Paste("next"), Key("Enter")])
        self.assertEqual(mode.opening_pending, "/color")
        self.append(record("start"))
        mode.poll(0)
        mode.poll(1.2)
        self.assertEqual(len(self.sends(mode)), 2)
        self.append(record("stop"))
        mode.poll(0)
        mode.poll(0.6)
        self.assertEqual(self.sends(mode)[2:], [Paste("/color"), Key("Enter")])

    def test_a_partly_sent_send_holds_the_opening(self):
        mode = self.launched()
        mode._terminal.calls.send.side_effect = [None, SessionError("gone")]
        with self.assertRaises(Unanswered):
            self.other().send("half")
        mode._terminal.calls.send.side_effect = None
        self.append(record("ready"), record("start"), record("stop"))
        mode.poll(0)
        mode.poll(1.2)
        self.assertEqual(self.sends(mode), [Paste("half"), Key("Enter")])  # the Enter failed
        self.assertEqual(mode.opening_pending, "/color")
        self.other().send("rest")  # its Enter submits the half too
        self.append(record("start"), record("stop"))
        mode.poll(0)
        mode.poll(0.6)
        self.assertEqual(self.sends(mode)[4:], [Paste("/color"), Key("Enter")])

    def test_no_turn_yet_keeps_the_opening(self):
        mode = self.launched()
        self.append(record("ready"))
        mode.poll(0)
        mode.poll(1.2)
        self.assertEqual(self.sends(mode), [Paste("/color"), Key("Enter")])

    def test_an_older_generations_marker_keeps_the_opening(self):
        mode = self.launched()
        tui.TuiStore.attach(NAME, host=self.host).mark_prompted("an older generation")
        self.append(record("ready"))
        mode.poll(0)
        mode.poll(1.2)
        self.assertEqual(self.sends(mode), [Paste("/color"), Key("Enter")])

    def test_a_prompt_opening_waits_for_no_launch_turn(self):
        mode = self.launched(PromptOpening(self.client), prompt="go")
        self.append(record("ready"))
        mode.poll(0)
        mode.poll(1.2)
        self.assertEqual(self.sends(mode), [Paste("/color"), Key("Enter")])
        self.assertFalse(Opening._opening_prompt)

    def test_a_mid_turn_opening_goes_in_once_the_launch_turn_started(self):
        mode = self.launched(MidTurnOpening(self.client), prompt="go")
        self.append(record("ready"))
        mode.poll(0)
        mode.poll(1.2)  # its launch prompt not yet taken: keys now could join it
        self.assertEqual(self.sends(mode), [])
        self.append(record("start"))
        mode.poll(0)
        mode.poll(0.6)
        self.assertEqual(self.sends(mode), [Paste("/color"), Key("Enter")])
        self.assertIsNone(mode.opening_pending)
        self.assertFalse(Opening._opening_mid_turn)

    def test_a_mid_turn_opening_waits_out_a_turn_once_blocked(self):
        mode = self.launched(MidTurnOpening(self.client), prompt="go")
        self.append(record("ready"), record("start"), record("ask", why="perm"))
        mode.poll(0)
        mode.poll(1.2)
        self.append(record("tool"))  # another tool's: its dialog may still show
        mode.poll(0)
        mode.poll(1.2)
        self.assertEqual(self.sends(mode), [])
        self.append(record("stop"))
        mode.poll(0)
        mode.poll(0.6)
        self.assertEqual(self.sends(mode), [Paste("/color"), Key("Enter")])

    def test_a_mid_turn_opening_waits_for_a_send_its_turn_has_not_taken(self):
        mode = self.launched(MidTurnOpening(self.client))
        self.other().send("hi")
        self.append(record("ready"))
        mode.poll(0)
        mode.poll(1.2)
        self.assertEqual(self.sends(mode), [Paste("hi"), Key("Enter")])
        self.append(record("start"))
        mode.poll(0)
        mode.poll(0.6)
        self.assertEqual(self.sends(mode)[2:], [Paste("/color"), Key("Enter")])

    def test_the_opening_holds_the_send_lock(self):
        mode = self.launched()
        other, arrived, threads, order = self.other(), threading.Event(), [], []

        def send(action):
            order.append(action)
            if action == Paste("two"):
                arrived.set()
            elif action == Paste("/color"):
                threads.append(threading.Thread(target=other.send, args=("two",)))
                threads[0].start()
                arrived.wait(0.5)

        mode._terminal.calls.send.side_effect = send
        self.append(record("ready"))
        mode.poll(0)
        mode.poll(1.2)
        threads[0].join(5)
        self.assertEqual(order, [Paste("/color"), Key("Enter"), Paste("two"), Key("Enter")])

    def test_a_held_send_lock_defers_the_opening(self):
        mode = self.launched()
        self.append(record("ready"))
        mode.poll(0)
        with tui.TuiStore.attach(NAME, host=self.host).sending():
            mode.poll(1.2)
        self.assertEqual(self.sends(mode), [])
        self.assertEqual(mode.opening_pending, "/color")
        mode.poll(0.6)
        self.assertEqual(self.sends(mode), [Paste("/color"), Key("Enter")])


class ScanTest(Base):
    def test_a_hook_arriving_during_capture_precedes_the_limit_seen_on_screen(self):
        mode = self.started()

        def capture(_terminal, _lines):
            self.append(record("tool", tool="submitted"))
            self.host.calls.screen.side_effect = None
            self.host.calls.screen.return_value = "LIMIT 100"
            return "LIMIT 100"

        self.host.calls.screen.side_effect = capture
        self.assertEqual(mode.poll(0), ([Activity("submitted")], False))
        self.assertEqual(mode.poll(0), ([LimitHit(100.0)], False))
        self.assertEqual(mode.poll(0), ([], False))

    def scans(self, mode, *screens) -> list:
        """mode.poll's events for each screen in turn."""
        found = []
        for screen in screens:
            self.host.calls.screen.return_value = screen
            found.append(mode.poll(0)[0])
        return found

    def test_a_dialog_is_blocked_once_and_its_end_activity(self):
        mode = self.started()
        self.assertEqual(
            self.scans(mode, "x", "a Trust this \n  folder? b", "Trust this folder?", "y", "y"),
            [[], [Blocked("Trust  this\nfolder?")], [], [Activity("")], []],
        )
        self.assertEqual(self.run.screens[1], "a Trust this folder? b")

    def test_dialogs_and_limits_are_one_event_per_appearance(self):
        for screens, expected in (
            (
                ("Sign in; Trust this folder?", "Sign in", "Trust this folder?", ""),
                [
                    [Blocked("Trust  this\nfolder?"), Blocked("Sign in")],
                    [Activity("")],
                    [Activity(""), Blocked("Trust  this\nfolder?")],
                    [Activity("")],
                ],
            ),
            (
                ("LIMIT 100", "LIMIT  100", "LIMIT 200", "", "LIMIT 200", "LIMIT 300"),
                [[LimitHit(100.0)], [], [], [], [LimitHit(200.0)], []],
            ),
        ):
            with self.subTest(screens=screens):
                mode = self.started()
                self.assertEqual(self.scans(mode, *screens), expected)

    def test_an_absent_name_is_not_scanned(self):
        mode = self.started()
        self.host.processes.clear()
        self.host.calls.screen.return_value = "Sign in"
        self.assertEqual(mode.poll(0), ([], True))
        self.assertEqual((self.run.screens, mode.returncode(0)), ([], -signal.SIGHUP))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "activity")))

    def test_quoted_claude_limit_or_dialog_text_is_no_event_and_the_native_dialog_is(self):
        for screens, expected in (
            (
                (
                    "> Documentation example: You've hit your weekly limit",
                    "  12 + You've hit your session limit · resets 3pm (UTC)",
                    "  ⎿  You've hit your session limit · resets 3pm (America/Los_Angeles)",
                ),
                [[], [], []],
            ),
            (
                (
                    '  55 TRUST = "Quick safety check: … 1. Yes, I trust this folder 2. No, exit"',
                    "> see Quick safety check docs",
                    "│ Quick safety check: Is this a project you trust?\n"
                    "│ ❯ 1. Yes, I trust this folder",
                ),
                [[], [], [Blocked("Quick safety check")]],
            ),
        ):
            with self.subTest(screens=screens):
                self.host.processes.clear()
                mode = self.start(run=ClaudeScreens(self.client))
                self.host.calls.reset_mock()
                self.assertEqual(self.scans(mode, *screens), expected)

    def test_a_failed_read_is_no_event(self):
        mode = self.started()
        self.host.calls.screen.side_effect = SessionError("tmux: gone")
        self.assertEqual(mode.poll(0), ([], False))


class ReturncodeStateTest(Base):
    def test_returncode_maps_the_status(self):
        for state, code in (
            (Liveness.LIVE, 0),
            (Liveness.LIVE, 2),
            (Liveness.LIVE, -9),
            (Liveness.ABSENT, -signal.SIGHUP),
            (Liveness.LIVE, None),
            (Liveness.UNKNOWN, None),
        ):
            with self.subTest(state=state, code=code):
                mode = self.started()
                mode._terminal.calls.state.return_value = state
                mode._terminal.calls.returncode.return_value = code
                self.assertEqual(mode.returncode(0), code)

    def test_returncode_waits_its_timeout_in_slices_while_running(self):
        mode = self.started()
        self.clock.actions.append(
            (0.25, lambda: setattr(mode._terminal.calls.returncode, "return_value", 4))
        )
        self.assertEqual(mode.returncode(5), 4)
        self.assertAlmostEqual(self.clock.now, 0.3)
        mode = self.started()
        self.clock.now = 0
        self.assertIsNone(mode.returncode(0.35))
        self.assertAlmostEqual(self.clock.now, 0.35)

    def test_state_maps_the_status(self):
        for state in Liveness:
            with self.subTest(state=state):
                mode = self.started()
                mode._terminal.calls.state.return_value = state
                self.assertEqual(mode.state(), state)

    def test_exited_asks_its_hosts_client_exited_for_its_run(self):
        mode = self.started()
        self.assertFalse(mode.exited())
        for exited in (False, True):
            with mock.patch.object(self.host, "_client_exited", return_value=exited) as asked:
                self.assertIs(mode.exited(), exited)
            asked.assert_called_once_with(NAME, mode.generation)


class PathsTest(Base):
    def test_its_hook_argv_runs_the_plugins_ensemble_session_hook_on_its_dir(self):
        self.start()
        self.assertEqual(
            self.host.calls.worker.call_args.args[1].argv,
            ("/bin/run", os.path.join(PLUGIN_BIN, "ensemble-session"), "hook", self.dir),
        )

    def test_its_hook_argv_names_the_command_its_current_path_finds(self):
        other = os.path.join(self.tmp, "other plugin", "bin")
        os.makedirs(other)
        command = os.path.join(other, "ensemble-session")
        with open(command, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\n")
        os.chmod(command, 0o755)
        with mock.patch.dict(os.environ, {"PATH": other + os.pathsep + os.environ["PATH"]}):
            self.start()
        self.assertEqual(
            self.host.calls.worker.call_args.args[1].argv,
            ("/bin/run", command, "hook", self.dir),
        )


class PrepareTest(Base):
    """start's session dir and session.json, before its session line."""

    def test_writes_session_json_0600_in_a_0700_session_dir(self):
        self.run.launched = {
            "cwd": "/cwd",
            "project": True,
            "transcript": "/t/x.jsonl",
            "resume": "fake --resume x",
        }
        self.start(workdir="/work")
        with open(os.path.join(self.dir, "session.json")) as f:
            self.assertEqual(
                json.load(f),
                self.state(
                    owner=self.owner(),
                    workdir="/work",
                    cwd="/cwd",
                    project=True,
                    transcript="/t/x.jsonl",
                    resume="fake --resume x",
                ),
            )
        self.assertEqual(os.listdir(self.dir), ["session.json"])
        for path, mode_bits in (
            (self.root, 0o700),
            (self.dir, 0o700),
            (os.path.join(self.dir, "session.json"), 0o600),
        ):
            self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), mode_bits, path)

    def test_its_identity_is_the_session_infos_persisted_fields_and_reads_back(self):
        self.run.launched = {"cwd": "/cwd", "project": True, "transcript": "/t", "resume": "r"}
        self.start()
        state, info = tui.TuiMode.snapshot(NAME), self.prepared.info
        persisted = info.persisted()
        self.assertEqual({key: state[key] for key in persisted}, persisted)
        self.assertEqual(set(persisted) & set(tui_hook.SESSION_STATE), {"sid"})
        self.assertEqual(
            SessionInfo.from_fields(state),
            SessionInfo(sid=SID, cwd="/cwd", project=True, transcript="/t", resume="r"),
        )

    def test_only_labels_of_a_known_kind_get_a_word_and_events_may_be_none(self):
        class Labels(ScanTui):
            hooks = {
                "end": HookKind.TURN_END,
                "fail": HookKind.TURN_FAILED,
                "perm": HookKind.BLOCKED,
                "note": HookKind.BLOCKED,
                "use": HookKind.ACTIVITY,
                "begin": HookKind.TURN_STARTED,
                "odd": "other",
            }

        self.start(run=Labels(self.client), events=None)
        self.assertEqual(
            tui.TuiMode.snapshot(NAME),
            self.state(
                owner=self.owner(),
                events=None,
                words={
                    "end": "done",
                    "fail": "done",
                    "perm": "blocked",
                    "note": "blocked",
                    "use": "working",
                    "begin": "working",
                },
            ),
        )

    def test_a_dead_or_absent_session_is_prepared(self):
        for code in (0, 2, -15, None):
            with self.subTest(code=code):
                self.host.processes.clear()
                previous = self.host.create(NAME, SID) if code is not None else None
                if previous is not None:
                    previous.calls.alive.return_value = False
                    previous.calls.returncode.return_value = code
                self.start()
                self.assertTrue(os.path.isfile(os.path.join(self.dir, "session.json")))

    def test_a_bad_session_state_is_refused_before_host_launch_or_a_path(self):
        class BadLabel(ScanTui):
            hooks = {"a b": HookKind.TURN_END}

        for run, launched, fields, bad in (
            (ScanTui, {"transcript": "rel.jsonl"}, {}, "transcript 'rel.jsonl'\\Z"),
            (ScanTui, {"cwd": "rel"}, {}, "cwd 'rel'\\Z"),
            (BadLabel, {}, {}, "words "),
            (ScanTui, {}, {"client": None}, "client None"),
        ):
            with (
                self.subTest(bad),
                self.assertRaisesRegex(SessionError, f"\\Atui session w1: bad {bad}"),
            ):
                self.client.run = run(self.client)
                self.client.run.launched = launched
                self.start(**fields)
        self.assertEqual(
            [c for c in self.host.calls.mock_calls if c[0] != "validate"],
            [],
        )
        self.assertFalse(os.path.lexists(self.root))
        self.assertEqual(self.recorded, [])

    def test_tui_dir_and_home_dir_are_made_when_missing(self):
        os.rmdir(self.ensemble)
        self.start(events=None)
        self.assertEqual(stat.S_IMODE(os.lstat(self.root).st_mode), 0o700)

    def test_a_tui_dir_that_is_no_directory_is_refused(self):
        open(self.root, "w").close()
        with self.assertRaisesRegex(SessionError, re.escape(self.root)):
            self.start(events=None)
        self.assertTrue(os.path.isfile(self.root))
        self.host.calls.worker.assert_not_called()

    def test_an_old_session_dir_is_replaced(self):
        os.makedirs(os.path.join(self.dir, "sub"))
        for name in ("hooks.jsonl", "session.json", "other"):
            with open(os.path.join(self.dir, name), "w") as f:
                f.write("old")
        self.start()
        self.assertEqual(os.listdir(self.dir), ["session.json"])
        self.assertEqual(tui.TuiMode.snapshot(NAME), self.state(owner=self.owner()))

    def test_an_old_entry_that_is_no_real_directory_is_refused(self):
        os.mkdir(self.root, 0o700)
        target = os.path.join(self.tmp, "target")
        os.mkdir(target)
        open(os.path.join(target, "keep"), "w").close()
        for case, make in (
            ("symlink", lambda: os.symlink(target, self.dir)),
            ("file", lambda: open(self.dir, "w").close()),
            ("fifo", lambda: os.mkfifo(self.dir)),
        ):
            with self.subTest(case):
                make()
                with self.assertRaisesRegex(
                    SessionError, rf"\A{re.escape(self.dir)}: not a directory\Z"
                ):
                    self.start()
                self.assertTrue(os.path.lexists(self.dir))
                os.unlink(self.dir)
        self.assertEqual(os.listdir(target), ["keep"])

    def test_a_session_dir_another_start_makes_first_is_starting_and_kept(self):
        real = os.mkdir

        def racing(path, mode=0o777, **kwargs):
            if os.fspath(path) == self.dir:  # another start's mkdir lands first
                real(path, 0o700)
                open(os.path.join(path, "theirs"), "w").close()
            return real(path, mode, **kwargs)

        with mock.patch("os.mkdir", racing), self.assertRaises(SessionError) as caught:
            self.start()
        self.assertEqual(str(caught.exception), "tui session w1 is starting")
        self.assertEqual(os.listdir(self.dir), ["theirs"])

    def test_a_failed_write_is_a_driver_error_and_removes_the_dir(self):
        with (
            mock.patch(
                "tempfile.mkstemp", side_effect=OSError(errno.ENOSPC, "No space left on device")
            ),
            self.assertRaisesRegex(SessionError, "No space left on device"),
        ):
            self.start()
        self.assertNoSessionDir()
        self.assertTrue(os.path.isdir(self.root))
        self.assertEqual(self.recorded, [])


class LockTest(Base):
    def test_a_0600_lock_file_per_name_in_tui_dir_held_till_the_block_ends(self):
        os.rmdir(self.ensemble)
        with tui.TuiStore.attach(NAME, host=self.host)._locked():
            self.assertTrue(self.locked())
            with tui.TuiStore.attach("w2", host=self.host)._locked():
                self.assertTrue(self.locked("w2"))
            self.assertFalse(self.locked("w2"))
        self.assertFalse(self.locked())
        self.assertEqual(stat.S_IMODE(os.lstat(os.path.join(self.root, "w1.lock")).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.lstat(self.root).st_mode), 0o700)
        self.assertEqual(self.host.calls.mock_calls, [])

    def test_a_second_holder_waits_for_the_first(self):
        entered = threading.Event()

        def second():
            with tui.TuiStore.attach(NAME, host=self.host)._locked():
                entered.set()

        with tui.TuiStore.attach(NAME, host=self.host)._locked():
            thread = threading.Thread(target=second)
            thread.start()
            self.assertFalse(entered.wait(0.2))
        thread.join(5)
        self.assertTrue(entered.is_set())

    def test_a_lock_path_that_is_no_regular_file_is_refused_at_once(self):
        os.mkdir(self.root, 0o700)
        path = os.path.join(self.root, "w1.lock")
        for case, make in (
            ("fifo", lambda: os.mkfifo(path)),
            ("directory", lambda: os.mkdir(path)),
        ):
            with self.subTest(case):
                make()
                with (
                    self.assertRaisesRegex(SessionError, rf"\A{re.escape(path)}: "),
                    tui.TuiStore.attach(NAME, host=self.host)._locked(),
                ):
                    pass
                (os.rmdir if case == "directory" else os.unlink)(path)


class StoreTest(Base):
    def test_records_are_whole_lines_from_a_cursor_and_the_cursor_past_them(self):
        store = tui.TuiStore.attach(NAME, host=self.host)
        self.assertEqual(store.records(None), ([], (None, 0)))
        os.makedirs(self.dir)
        first = (json.dumps(record("tool", tool="t")) + "\n").encode()
        self.append(first, b"bad\n", record("stop"), b'{"label": "ask"')
        found, (ino, end) = store.records(None)
        self.assertEqual((found, ino), ([("tool", {"tool": "t"}), ("stop", {})], self.ino()))
        self.assertEqual(store.records((ino, len(first))), ([("stop", {})], (ino, end)))
        self.assertEqual(store.records((ino, end)), ([], (ino, end)))

    def ino(self, path=None):
        return os.stat(path or self.hooks).st_ino

    def test_a_cursor_follows_a_rotation_reading_each_record_once(self):
        store = tui.TuiStore.attach(NAME, host=self.host)
        os.makedirs(self.dir)
        self.append(record("a"))
        _, cursor = store.records(None)
        self.append(record("b"))
        os.replace(self.hooks, f"{self.hooks}.1")
        self.append(record("c"))
        found, cursor = store.records(cursor)
        self.assertEqual(([label for label, _ in found], cursor[0]), (["b", "c"], self.ino()))
        self.assertEqual(store.records(cursor), ([], cursor))

    def test_a_directory_in_either_hook_log_place_gives_no_records_and_leaks_no_fd(self):
        store = tui.TuiStore.attach(NAME, host=self.host)
        for place in (self.hooks, f"{self.hooks}.1"):
            with self.subTest(place=place):
                os.makedirs(place)
                fds = len(os.listdir("/dev/fd"))
                self.assertEqual(store.records(None), ([], (None, 0)))
                self.assertEqual(len(os.listdir("/dev/fd")), fds)
                os.rmdir(place)

    def test_attach_replays_the_rotated_file_then_the_current_one_once(self):
        store = tui.TuiStore.attach(NAME, host=self.host)
        os.makedirs(self.dir)
        self.append(record("a"))
        os.replace(self.hooks, f"{self.hooks}.1")
        self.append(record("b"))
        found, _ = store.records(None)
        self.assertEqual([label for label, _ in found], ["a", "b"])
        os.unlink(f"{self.hooks}.1")
        os.link(self.hooks, f"{self.hooks}.1")
        found, _ = store.records(None)
        self.assertEqual([label for label, _ in found], ["b"])


class TmuxErrorTest(TmuxBase):
    def test_real_host_launch_keeps_the_modes_exact_error_prefix(self):
        os.makedirs(owner := os.path.join(self.ensemble, "harness", "a" * 32), 0o700)
        workdir = os.path.join(self.dir, "work")
        os.mkdir(workdir)

        class Launchable(ScanTui):
            def launch(self, spec, hook):
                return Launch((sys.executable,))

        client = TuiClient({"tui": True})
        client.run = Launchable(client)
        with (
            mock.patch("subprocess.Popen", side_effect=OSError("boom")),  # its guard's spawn
            self.assertRaises(SessionError) as caught,
        ):
            tui.TuiMode.start(
                Spec(
                    workdir=workdir, access=Access(cwd=workdir), name=NAME, client="fake", sid=SID
                ),
                client,
                registry=self.session_cls._config().registry,
                record=lambda _info: None,
                host=self.host,
                owner=owner,
            )
        self.assertEqual(
            str(caught.exception),
            "cannot start tui session w1: worker w1: cannot start guard: boom",
        )


class TmuxAccountTest(TmuxBase):
    def test_a_valid_selection_changes_no_tmux_launch(self):
        workdir = os.path.join(self.dir, "work")
        os.mkdir(workdir)
        client = ClaudeClient({"accounts": {"work": {"dir": "/accounts/work"}}})
        bin_dir = os.path.join(self.dir, "bin")
        os.mkdir(bin_dir)
        command = os.path.join(bin_dir, "claude")
        with open(command, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\n")
        os.chmod(command, 0o755)
        prepared = []
        with mock.patch.dict(
            os.environ,
            {"CLAUDE_CONFIG_DIR": "/mine", "PATH": bin_dir + os.pathsep + "/usr/bin:/bin"},
        ):
            for env in ({}, {ACCOUNT: "work"}, {ACCOUNT: "default"}):
                with self.subTest(env=env):
                    prepared.append(
                        tui.TuiMode.preflight(
                            Spec(
                                workdir=workdir,
                                access=Access(cwd=workdir),
                                name=NAME,
                                client="claude",
                                sid=SID,
                                env=env,
                            ),
                            client,
                            registry=self.session_cls._config().registry,
                            host=self.host,
                        )
                    )
        argv, env = prepared[0].argv, dict(prepared[0].env)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/mine")
        self.assertNotIn(ACCOUNT, env)
        for other in prepared[1:]:
            self.assertEqual((other.argv, dict(other.env)), (argv, env))
        with self.assertRaises(ConfigError) as caught:
            tui.TuiMode.preflight(
                Spec(
                    workdir=workdir,
                    access=Access(cwd=workdir),
                    name=NAME,
                    client="claude",
                    sid=SID,
                    env={ACCOUNT: "nope"},
                ),
                client,
                registry=self.session_cls._config().registry,
                host=self.host,
            )
        self.assertEqual(
            str(caught.exception),
            "account 'nope': not a registered account of client claude"
            " ([clients.claude.accounts.nope])",
        )


BIG = "雪" * 40_000


class PromptFileTest(Base):
    def setUp(self):
        super().setUp()
        self.path = os.path.join(self.dir, "prompt", "prompt.md")
        self.seen = Seen(self.client)

    def pointer(self, path):
        return POINTER.format(size=len(BIG.encode()), path=path)

    def test_an_over_budget_prompt_becomes_a_pointer_with_its_dir_readable(self):
        self.start(run=self.seen, prompt=BIG, access=Access(cwd=self.tmp, reads=("/r",)))
        self.assertEqual(
            (self.seen.spec.prompt, self.prepared.argv[1]), (self.pointer(self.path),) * 2
        )
        self.assertEqual(self.seen.spec.access.reads, ("/r", os.path.dirname(self.path)))
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), BIG)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(self.path)).st_mode), 0o700)
        self.assertNotIn("prompt_file", self.host.calls.worker.call_args.kwargs["metadata"])

    def test_a_resume_s_over_budget_prompt_is_a_pointer_too(self):
        self.start(run=self.seen, prompt=BIG, resume=True)
        self.assertEqual(self.seen.spec.prompt, self.pointer(self.path))
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), BIG)

    def test_a_container_context_puts_the_prompt_in_metadata_not_on_disk(self):
        context = ClientContext(
            "host.docker.internal",
            Permission.YOLO,
            Container("/image/bin", "/image/python", "/image/modules", "/run/state"),
        )
        with mock.patch.object(self.host, "_client_context", return_value=context):
            self.start(run=self.seen, prompt=BIG)
        metadata = self.host.calls.worker.call_args.kwargs["metadata"]
        path = "/run/state/prompt/prompt.md"
        self.assertEqual((metadata["prompt_file"], metadata["prompt"]), (path, BIG))
        self.assertEqual(self.seen.spec.prompt, self.pointer(path))
        self.assertEqual(self.seen.spec.access.reads, ("/run/state/prompt",))
        self.assertFalse(os.path.lexists(os.path.dirname(self.path)))

    def test_preflight_writes_no_prompt_file(self):
        prepared = tui.TuiMode.preflight(
            Spec(
                workdir=self.tmp,
                access=Access(cwd=self.tmp),
                name=NAME,
                client="fake",
                sid=SID,
                prompt=BIG,
            ),
            self.client,
            registry=self.session_cls._config().registry,
            host=self.host,
        )
        self.assertEqual(prepared.spec.prompt, self.pointer(self.path))
        self.assertFalse(os.path.lexists(os.path.dirname(self.path)))

    def test_a_prompt_within_the_budget_stays_in_argv(self):
        at = "a" * 99_998  # the longest prompt over_argv keeps in argv
        self.start(run=self.seen, prompt=at)
        self.assertEqual((self.prepared.argv[1], self.seen.spec.access.reads), (at, ()))
        self.assertFalse(os.path.lexists(os.path.dirname(self.path)))

    def test_an_unwritable_prompt_file_fails_the_start(self):
        real = os.open

        def refusing(path, *args, **kwargs):
            if str(path).endswith("prompt.md"):
                raise OSError(errno.EACCES, "Permission denied")
            return real(path, *args, **kwargs)

        with (
            mock.patch("os.open", refusing),
            self.assertRaisesRegex(
                SessionError,
                f"^cannot start tui session {NAME}: prompt file {re.escape(self.path)}: "
                "Permission denied$",
            ),
        ):
            self.start(run=self.seen, prompt=BIG)
        self.host.calls.worker.assert_not_called()


class OwnerTest(Base):
    """A start a session harness performs names its harness dir in session.json."""

    def test_an_owned_start_writes_its_owner(self):
        self.start()
        self.assertEqual(tui.TuiMode.snapshot(NAME)["owner"], self.owner())

    def test_an_owned_start_tells_its_host_the_owner_and_follows_a_remote_or_up_terminal(self):
        running = self.start()
        self.assertEqual(self.host.calls.worker.call_args.kwargs["metadata"]["owner"], self.owner())
        self.assertFalse(running.remote)
        running._terminal.remote = True
        self.assertTrue(running.remote)
        self.assertFalse(running.client_up)
        running._terminal.up = True
        self.assertTrue(running.client_up)

    def test_client_alive_asks_its_handles_liveness(self):
        mode = self.start()
        for state, alive in (
            (Liveness.LIVE, True),
            (Liveness.ABSENT, False),
            (Liveness.UNKNOWN, False),
        ):
            mode._terminal.calls.state.return_value = state
            self.assertIs(mode.client_alive(), alive)
        mode._terminal.calls.state.side_effect = Unanswered("its tmux did not tell")
        self.assertFalse(mode.client_alive())

    def test_an_owner_outside_the_harness_root_is_refused(self):
        path = os.path.join(self.dir, "session.json")
        outside = os.path.join(self.tmp, "a" * 32)
        os.mkdir(outside)
        link = os.path.join(self.ensemble, "harness", "b" * 32)
        os.makedirs(os.path.dirname(link), exist_ok=True)
        os.symlink(outside, link)
        for owner in (
            outside,
            link,
            self.owner("short"),
            self.owner("A" * 32),
            os.path.join(self.ensemble, "harness"),
            "rel/" + "a" * 32,
            5,
        ):
            with self.subTest(owner=owner):
                self.write_state(self.state(owner=owner))
                with self.assertRaises(SessionError) as caught:
                    tui.TuiMode.snapshot(NAME)
                self.assertEqual(str(caught.exception), f"no tui session w1: {path}: bad owner")
        self.write_state(self.state(owner=self.owner()))
        self.assertEqual(tui.TuiMode.snapshot(NAME)["owner"], self.owner())


class RecordRuleTest(Base):
    """An owned record (session.json with an owner) goes only with its own worker generation: the
    owner closes by its handle; anyone else asks the generation's liveness (the gate, too).
    """

    def setUp(self):
        super().setUp()
        self.liveness = mock.Mock(return_value=Liveness.ABSENT)
        self.host.worker_liveness = self.liveness
        self.store = tui.TuiStore.attach(NAME, host=self.host)

    def owned(self, generation: str = "g1", **changes) -> dict:
        """An owned session.json of `generation` and its display."""
        path = os.path.join(self.ensemble, "harness", "a" * 32)
        os.makedirs(path, 0o700, exist_ok=True)
        self.display = self.host.create(
            NAME, identity=f"display-{generation}", metadata={"sid": SID, "generation": generation}
        )
        state = self.state(owner=path, generation=generation, **changes)
        self.write_state(state)
        return state

    def handle(self, state: Liveness = Liveness.LIVE) -> mock.Mock:
        """Its owner's handle, which closes its own display."""
        handle = mock.create_autospec(ManagedProcess, instance=True)
        handle.state.return_value = state
        return handle

    def saved(self) -> dict:
        with open(os.path.join(self.dir, "session.json"), encoding="utf-8") as f:
            return json.load(f)

    def test_owned_stale_unknown_is_busy(self):
        state = self.owned()
        self.liveness.return_value = Liveness.UNKNOWN
        with self.assertRaises(SessionBusy) as caught:
            self.start()
        self.assertEqual(caught.exception.holder, "previous worker generation (live or unknown)")
        self.assertEqual(self.saved(), state)
        self.assertIs(self.host.processes[NAME], self.display)
        self.display.calls.close.assert_not_called()
        self.liveness.assert_called_with(NAME, "g1")

    def test_owned_stale_live_waits_then_goes_on(self):
        self.owned()
        self.liveness.side_effect = [Liveness.LIVE, Liveness.LIVE, Liveness.ABSENT]
        self.start()
        self.assertEqual(self.log.count(("sleep", tui.RECHECK)), 2)
        self.display.calls.close.assert_called_once_with()
        self.assertNotEqual(self.saved()["generation"], "g1")

    def test_owned_stale_live_refused_after_wait_untouched(self):
        state = self.owned()
        self.liveness.return_value = Liveness.LIVE
        with self.assertRaisesRegex(SessionError, "^tui session w1 is running"):
            self.start()
        self.assertGreaterEqual(self.clock.now, GUARD_WAIT)
        self.assertEqual(self.saved(), state)
        self.display.calls.close.assert_not_called()

    def test_owned_stale_absent_closes_the_display(self):
        self.owned()
        self.start()
        self.host.calls.release.assert_called_once_with(NAME, "g1")
        self.display.calls.close.assert_called_once_with()
        self.assertIsNot(self.host.processes[NAME], self.display)

    def test_owned_stale_ignores_a_killed_display(self):
        state = self.owned()
        self.host.processes.clear()  # its display killed; the worker lives on
        self.liveness.return_value = Liveness.LIVE
        with self.assertRaisesRegex(SessionError, "is running"):
            self.start()
        self.assertEqual(self.saved(), state)

    def test_owned_close_closes_the_handle_then_the_dir(self):
        self.owned()
        order = mock.Mock()
        handle = self.handle()
        handle.close.side_effect = lambda: order("handle")
        self.assertTrue(self.store.close(handle, "g1"))
        self.assertEqual(order.call_args_list, [mock.call("handle")])
        self.assertNoSessionDir()
        self.liveness.assert_not_called()

    def test_owned_stop_closes_the_handle(self):
        self.owned()
        handle = self.handle()
        self.store.stop(handle, generation="g1")
        handle.close.assert_called_once_with()
        self.assertNoSessionDir()

    def test_owned_close_failed_handle_keeps_the_record(self):
        for failure in ("unknown", "unanswered"):
            with self.subTest(failure=failure):
                state = self.owned()
                handle = self.handle(Liveness.UNKNOWN if failure == "unknown" else Liveness.LIVE)
                handle.close.side_effect = Unanswered("worker w1: guard exit unconfirmed")
                with self.assertRaisesRegex(Unanswered, "^worker w1: exit unconfirmed$"):
                    self.store.close(handle, "g1")
                self.assertEqual(handle.close.call_count, 0 if failure == "unknown" else 1)
                self.assertEqual(self.saved(), state)

    def test_owned_close_of_an_old_generation_keeps_the_new_record_and_display(self):
        state = self.owned("g2")
        handle = self.handle(Liveness.ABSENT)
        self.assertFalse(self.store.close(handle, "g1"))
        self.display.calls.close.assert_not_called()
        self.assertEqual(self.saved(), state)
        self.assertFalse(self.store.close(handle, "g1"))  # a drain-window repeat, still harmless
        self.liveness.assert_not_called()

    def test_non_owner_close_absent_cleans_up(self):
        for generation in (None, "g1"):
            with self.subTest(generation=generation):
                self.owned()
                self.assertTrue(self.store.close(None, generation))
                self.host.calls.release.assert_called_with(NAME, "g1")
                self.display.calls.close.assert_called_once_with()
                self.assertNoSessionDir()
                self.liveness.assert_called_with(NAME, "g1")

    def test_non_owner_close_whose_release_fails_keeps_the_record(self):
        state = self.owned()
        self.host.calls.release.side_effect = SessionError("tmux session w1: boom")
        with self.assertRaisesRegex(
            SessionError, r"\Acannot close tui session w1: tmux session w1: boom\Z"
        ):
            self.store.close()
        self.assertEqual(self.saved(), state)
        self.assertTrue(os.path.exists(os.path.join(self.root, f"{NAME}.lock")))

    def test_non_owner_close_live_or_unknown_is_busy_untouched(self):
        for liveness in (Liveness.LIVE, Liveness.UNKNOWN):
            with self.subTest(liveness=liveness):
                state = self.owned()
                self.liveness.return_value = liveness
                with self.assertRaises(SessionBusy) as caught:
                    self.store.close()
                self.assertEqual(caught.exception.holder, "worker generation (live or unknown)")
                self.assertEqual(
                    str(caught.exception),
                    "busy: lock w1 is held by worker generation (live or unknown)",
                )
                self.display.calls.close.assert_not_called()
                self.assertEqual(self.saved(), state)

    def test_non_owner_close_of_another_generation_touches_nothing(self):
        state = self.owned("g2")
        self.liveness.return_value = Liveness.LIVE
        self.assertFalse(self.store.close(None, "g1"))
        self.liveness.assert_not_called()
        self.assertNotIn(("sleep", tui.RECHECK), self.log)
        self.display.calls.close.assert_not_called()
        self.assertEqual(self.saved(), state)

    def test_owned_record_without_generation_is_unknown(self):
        state = self.owned()
        del state["generation"]
        self.write_state(state)
        with self.assertRaises(SessionBusy):
            self.store.close()
        with self.assertRaises(SessionBusy) as caught:
            tui._stale(self.store)
        self.assertEqual(caught.exception.holder, "previous worker generation (live or unknown)")
        self.liveness.assert_not_called()

    def test_non_owner_stop_takes_the_record_rule(self):
        state = self.owned()
        self.liveness.return_value = Liveness.UNKNOWN
        with self.assertRaises(SessionBusy):
            self.store.stop(generation="g1")
        self.store.stop(generation="g0")  # another generation's: untouched
        self.display.calls.close.assert_not_called()
        self.assertEqual(self.saved(), state)
        self.liveness.return_value = Liveness.ABSENT
        self.store.stop(generation="g1")
        self.assertNoSessionDir()

    def test_expire_keeps_a_live_owned_record(self):
        self.owned()
        self.host.processes.clear()
        old = time.time() - STALE_AGE - 60
        os.utime(self.dir, (old, old))  # idle, no hook log
        for liveness in (Liveness.LIVE, Liveness.UNKNOWN):
            self.liveness.return_value = liveness
            self.store.expire(set())
            self.assertTrue(os.path.isdir(self.dir))
        self.liveness.return_value = Liveness.ABSENT
        self.store.expire(set())
        self.assertNoSessionDir()
        self.assertNotIn(("sleep", tui.RECHECK), self.log)

    def test_unowned_close_removes_only_its_dir(self):
        self.write_state(self.state())
        display = self.host.create(NAME, metadata={"sid": SID, "generation": "fixture-generation"})
        self.assertTrue(self.store.close())
        display.calls.close.assert_not_called()
        self.assertNoSessionDir()
        self.liveness.assert_not_called()


class OwnedBase(Base):
    """A TuiMode started by its owner (inside a session harness) on a mock guard handle."""

    def setUp(self):
        super().setUp()
        self.handle = mock.create_autospec(InteractiveProcess, instance=True)
        self.handle.state.return_value = Liveness.LIVE
        self.handle.returncode.return_value = None
        self.handle.read.return_value = []
        self.host.worker = mock.Mock(return_value=self.handle)

    def owned_start(self) -> tui.TuiMode:
        return self.start()


class SettleRepliesTest(OwnedBase):
    """B2: the owner's pump settles its run's mid-turn replies, at most every SETTLE_EVERY s, with
    the workdir and sid its hooks get; a view's TuiMode never does.
    """

    def test_the_owner_pump_settles_its_runs_replies_once_a_second(self):
        mode = self.owned_start()
        clock = iter([100.0, 100.5, 101.0, 101.2])
        with (
            mock.patch.object(mode.run, "_settle_replies") as settle,
            mock.patch.object(tui.time, "monotonic", side_effect=lambda: next(clock)),
        ):
            for _ in range(4):
                mode._read()
        spec = mode.prepared
        self.assertEqual(settle.call_args_list, [mock.call(spec.spec.workdir, spec.info.sid)] * 2)

    def test_a_views_read_settles_nothing(self):
        mode = self.owned_start()
        mode._owned = False
        with mock.patch.object(mode.run, "_settle_replies") as settle:
            mode._read()
        settle.assert_not_called()


class ManagedStartTest(OwnedBase):
    """An owned start (inside a session harness) launches its client under the guard; its TuiMode
    drives that owner handle and only the handle ends it.
    """

    def test_owned_start_launches_a_managed_worker(self):
        mode = self.owned_start()
        self.host.worker.assert_called_once()
        args, kwargs = self.host.worker.call_args
        self.assertEqual(args[0], NAME)
        self.assertEqual(args[1].argv, mode.prepared.argv)
        self.assertTrue(kwargs["terminal"])
        self.assertEqual(kwargs["events"], self.events)
        self.assertIsNone(kwargs["display"])
        self.assertEqual(kwargs["metadata"]["generation"], tui.TuiMode.snapshot(NAME)["generation"])
        self.assertIs(mode._terminal, self.handle)

    def test_owned_poll_takes_worker_exit_code(self):
        mode = self.owned_start()
        self.handle.read.return_value = [WorkerExit(3)]
        self.handle.state.return_value = Liveness.ABSENT
        self.handle.returncode.return_value = 3
        self.host.processes.clear()  # its display went with the guard
        self.assertEqual(mode.poll(0), ([], True))
        self.assertEqual(mode.returncode(0), 3)

    def test_confirmed_guard_exit_marks_dead_once_without_a_display(self):
        mode = self.owned_start()
        self.handle.state.return_value = Liveness.ABSENT
        self.handle.returncode.return_value = 7
        self.assertEqual(mode.poll(0), ([], True))
        self.assertEqual(mode.poll(0), ([], True))
        with open(self.events, encoding="utf-8") as stream:
            self.assertEqual([line.split(" ", 1)[1] for line in stream], [f"{NAME} dead\n"])

    def test_owned_poll_ends_a_removed_handle_without_a_code(self):
        """A handle gone without a confirmed code (a Docker worker removed after a failure)."""
        mode = self.owned_start()
        self.handle.state.return_value = Liveness.ABSENT
        self.host.processes.clear()
        self.assertEqual(mode.poll(0), ([], True))
        self.assertEqual(mode.returncode(0), -signal.SIGHUP)

    def test_owned_poll_raises_on_channel_failure(self):
        mode = self.owned_start()
        self.handle.read.side_effect = Unanswered("worker w1: guard channel ended")
        with self.assertRaisesRegex(Unanswered, "guard channel ended"):
            mode.poll(0)

    def test_owned_poll_ignores_channel_hooks(self):
        mode = self.owned_start()
        self.handle.read.return_value = [WorkerHook("stop", {"x": 1}, "d1")]
        with self.assertLogs("session", "WARNING") as logged:
            self.assertEqual(mode.poll(0), ([], False))
            self.assertEqual(mode.poll(0), ([], False))
        self.assertEqual(len(logged.records), 1)
        self.assertIn("'stop'", logged.output[0])

    def test_owned_replaced_never_ends_the_owner(self):
        mode = self.owned_start()
        self.host.processes.clear()  # its display killed
        state = tui.TuiMode.snapshot(NAME)
        self.write_state({**state, "generation": "another"})
        self.assertEqual(mode.poll(0), ([], False))
        self.assertIsNone(mode.returncode(0))


class ActivityTest(OwnedBase):
    """The owner writes its worker's activity word, and for done and blocked one events line, per
    newly read hook record or newly shown dialog.
    """

    def activity(self) -> str:
        with open(os.path.join(self.dir, "activity"), encoding="utf-8") as f:
            return f.read()

    def lines(self) -> list[str]:
        try:
            with open(self.events, encoding="utf-8") as f:
                return [line.split(" ", 1)[1] for line in f.read().splitlines()]
        except FileNotFoundError:
            return []

    def test_mapped_hook_sets_activity_and_line(self):
        mode = self.owned_start()
        generation = mode.generation
        self.append(record("tool", tool="x"))
        mode.poll(0)
        self.assertEqual((self.activity(), self.lines()), (f"{generation} working\n", []))
        self.append(record("stop"))
        mode.poll(0)
        self.assertEqual(
            (self.activity(), self.lines()), (f"{generation} done\n", [f"{NAME} done"])
        )
        self.assertEqual(host_base.activity(NAME, generation), StatusState.DONE)
        self.assertIsNone(host_base.activity(NAME, "another"))

    def test_a_record_its_client_drops_marks_nothing(self):
        mode = self.owned_start()
        mode.run.hooks = {**mode.run.hooks, "start": HookKind.TURN_STARTED}
        self.append(record("start", turn_id="t1"))
        self.append(record("stop", turn_id="t1"))
        mode.poll(0)
        self.append(record("start", turn_id="t1"))  # its turn again: no new one
        mode.poll(0)
        self.assertEqual(self.activity(), f"{mode.generation} done\n")

    def test_the_clients_own_record_read_with_older_hooks_marks_last(self):
        mode = self.owned_start()
        observed = [[TurnEnd("")]]
        mode.run.observe = lambda: observed.pop() if observed else []
        self.append(record("tool", tool="x"))
        mode.poll(0)
        self.assertEqual(self.activity(), f"{mode.generation} done\n")

    def test_each_record_marks_once(self):
        mode = self.owned_start()
        self.append(record("ask", why="y"))
        mode.poll(0)
        mode.poll(0)
        self.assertEqual(self.lines(), [f"{NAME} blocked"])

    def test_without_an_events_file_only_the_word(self):
        self.events = None
        mode = self.owned_start()
        self.append(record("stop"))
        mode.poll(0)
        self.assertEqual(self.activity(), f"{mode.generation} done\n")

    def test_dialog_marks_blocked_once(self):
        mode = self.owned_start()
        self.handle.screen.return_value = "Sign in"
        mode.poll(0)
        mode.poll(0)
        self.assertEqual(self.activity(), f"{mode.generation} blocked\n")
        self.assertEqual(self.lines(), [f"{NAME} blocked"])
        self.assertTrue(mode.marked_blocked())

    def test_no_host_hook_in_session_json(self):
        self.owned_start()
        state = tui.TuiMode.snapshot(NAME)
        self.assertNotIn("host_hook", state)
        self.assertIn("display", state)

    def test_a_failed_mark_still_yields_the_dialog(self):
        mode = self.owned_start()
        self.handle.screen.return_value = "Sign in"
        os.mkdir(os.path.join(self.dir, "activity"))  # its mark cannot replace a directory
        with self.assertLogs("session", "WARNING"):
            self.assertEqual(mode.poll(0), ([Blocked("Sign in")], False))
