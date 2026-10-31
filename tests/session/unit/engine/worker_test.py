"""Worker with a recorded Session class and Host: state, Record (stored in its worker
dir), the early watch, and resume and restart through its session harness (no process, no tmux;
files only under the test's HOME).
"""

import contextlib
import errno
import io
import itertools
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from ensemble.session import (
    ConfigError,
    EnvironmentSpec,
    Host,
    Mode,
    NotShowable,
    Session,
    SessionBusy,
    SessionError,
    cli,
)
from ensemble.session.engine import worker
from ensemble.session.engine.hosted_session import HostedSession
from ensemble.session.engine.types import (
    Blocked,
    Exited,
    IdleTimeout,
    LimitHit,
    Reply,
    TurnEnd,
    TurnStarted,
)
from ensemble.session.engine.worker import Worker, WorkerState
from ensemble.session.harness.channel import Channel
from ensemble.session.hosts.base import stored_record
from ensemble.session.modes.tui import TuiStore
from ensemble.utils import Record, Status, StatusState, file_lock
from tests.session import harnesses
from tests.session.case import SID, Case

OTHER = "1f8fad5b-d9cb-469f-a165-70867728950e"
ENVIRONMENT = EnvironmentSpec(image="dummy-env:test", gateway="workers", lock="dummy-lock")


class Killed(BaseException):
    """The start's process dying: no handler runs."""


class RemoteHost(Host):
    """A Docker-like Host: its tui runs remote."""

    _remote_tui = True

    def list(self) -> list[str]:
        return ["w1"]


def read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def state(**fields) -> WorkerState:
    return WorkerState(
        **{
            "name": "w1",
            "sid": SID,
            "client": "claude",
            "cwd": "/work",
            "host": "tmux",
            "flags": ("-m", "x"),
            "env": {"K": ""},
            "events": "/events",
            "display": "mgr",
            **fields,
        }
    )


def state_path() -> str:
    return os.path.join(state().workdir(), worker.STATE_FILE)


def pointer() -> str:
    return os.path.join(os.path.dirname(state().workdir()), worker.BY_NAME, "w1")


class Base(Case):
    def setUp(self):
        super().setUp()
        self.order = mock.Mock()
        self.host = mock.create_autospec(Host, instance=True)
        self.host.list.return_value = ["w1"]
        self.session_cls = mock.Mock()
        self.session_cls.host.return_value = self.host
        self.session_cls.retire.side_effect = lambda name, **_: TuiStore.attach(
            name, host=self.host
        ).close()
        self.session = self.session_cls.start.return_value
        self.session_cls.start.side_effect = self.launched
        self.session.showable = False
        self.session.opening_pending = None
        self.session.client_up = True
        self.session.dialog = None
        self.session.screen.return_value = ""
        self.session.events.return_value = iter([IdleTimeout(worker.EARLY)])
        self.order.attach_mock(self.session_cls.start, "start")
        self.order.attach_mock(self.session_cls.preflight, "preflight")
        self.said = []
        self.tui = os.path.join(self.home, ".ensemble", "tui", "w1")
        os.makedirs(self.tui)

    def worker(self, **fields) -> Worker:
        return Worker(state(**fields), session_cls=self.session_cls, say=self.said.append)

    def calls(self) -> list[str]:
        return [call[0] for call in self.order.mock_calls if "." not in call[0]]

    def launched(self, spec, **_):
        """A start's harness: w1's session.json of its sid, generation g1, no owner yet."""
        self.write_state(spec.sid)
        return mock.DEFAULT

    def stored(self, generation: str = "g1") -> Record | None:
        return stored_record("w1", generation)

    def unstore(self) -> None:
        os.unlink(os.path.join(self.tui, "record"))

    def owned(
        self,
        status: str | None = "running",
        *,
        sid: str = SID,
        generation: str = "g1",
    ) -> None:
        """w1's session.json of `sid` naming its owner; its harness held and in `status` (None: no
        harness holds it); a running one answers each request ok, its ops in self.asked.
        """
        owner = harnesses.HeldOwner(self, Channel.create())
        if status is not None:
            owner.hold(state=status, name="w1", sid=sid, generation=generation)
        if status == "running":
            self.asked = owner.serve({"ok": True, "data": {}}, until_cleanup=True)
        self.write_state(sid, generation=generation, owner=owner.channel.directory)

    def write_state(self, sid: str, *, generation: str = "g1", owner: str | None = None) -> None:
        record = {
            "client": "claude",
            "workdir": state().workdir(),
            "sid": sid,
            "cwd": "/work",
            "project": True,
            "transcript": "",
            "resume": "",
            "events": "/events",
            "words": {},
            "host": "tmux",
            "generation": generation,
            **({} if owner is None else {"owner": owner}),
        }
        with open(os.path.join(self.tui, "session.json"), "w", encoding="utf-8") as f:
            json.dump(record, f)


class StateTest(Case):
    def test_its_json_round_trips_and_names_every_field_once(self):
        found = state()
        data = json.loads(found.to_json())
        self.assertEqual(data["version"], 1)
        self.assertEqual((data["flags"], data["env"]), (["-m", "x"], {"K": ""}))
        self.assertEqual(WorkerState.from_json(data), found)

    def test_a_bad_field_or_shape_is_a_config_error(self):
        for field, value in (
            ("name", "a b"),
            ("sid", ".."),
            ("sid", SID.upper()),
            ("client", "Bad"),
            ("cwd", "rel"),
            ("host", "X"),
            ("flags", ["-m"]),
            ("flags", ("a\0",)),
            ("env", {"A=B": "1"}),
            ("env", {"K": "a\nb"}),
            ("events", "rel"),
            ("display", "a\nb"),
        ):
            with self.subTest(field=field, value=value), self.assertRaises(ConfigError):
                state(**{field: value})
        data = json.loads(state().to_json())
        for change in ({"version": 2}, {"extra": 1}, {"flags": "x"}, {"env": []}):
            with self.subTest(change=change), self.assertRaises(ConfigError):
                WorkerState.from_json({**data, **change})
        del data["events"]
        with self.assertRaises(ConfigError):
            WorkerState.from_json(data)

    def test_its_record_is_a_worker_answered_and_resumed_by_the_session_command(self):
        record = state().record()
        workdir = os.path.join(self.home, ".ensemble", "workers", SID)
        self.assertEqual(
            record,
            Record(
                1,
                "worker",
                "w1",
                ("w1",),
                SID,
                workdir,
                ("ensemble-session",),
                ("ensemble-session", "resume", "w1"),
                "mgr",
                "claude",
            ),
        )
        self.assertEqual(Record.parse(record.to_line()), record)

    def test_its_spec_is_an_interactive_tui_in_its_cwd(self):
        spec = state().spec(prompt="hi", resume=True)
        self.assertEqual(
            (spec.mode, spec.sid, spec.name, spec.client, spec.prompt, spec.resume, spec.events),
            (Mode.TUI, SID, "w1", "claude", "hi", True, "/events"),
        )
        self.assertEqual((spec.access.cwd, spec.access.project), ("/work", True))
        self.assertEqual((spec.flags, dict(spec.env)), (("-m", "x"), {"K": ""}))
        self.assertEqual(spec.workdir, state().workdir())


class StartTest(Base):
    def test_a_quiet_start_saves_its_state_then_stores_and_returns_its_record(self):
        found = self.worker().start(prompt="hi", resume=False)
        self.assertEqual(found, state().record())
        self.assertEqual(self.calls(), ["start"])
        self.assertEqual(self.stored(), found)
        saved = Worker.load("w1")
        self.assertEqual(saved.state, state())
        _, kwargs = self.session_cls.start.call_args
        self.assertEqual(kwargs, {"display": "mgr", "host": "tmux", "idle_timeout": worker.TICK})

    def test_a_dialog_ends_the_watch_at_once_and_still_stores_its_record(self):
        self.session.client_up = False
        self.session.events.return_value = iter([Reply("x"), Blocked("Edit"), Exited(1)])
        self.worker().start(prompt="", resume=False)
        self.assertEqual(self.said, ["session w1: waits on a dialog: Edit"])
        self.assertEqual(self.stored(), state().record())

    def test_an_early_exit_reports_its_last_lines_and_stores_nothing_keeping_it(self):
        self.session.events.return_value = iter([Exited(7)])
        with self.assertRaises(SessionError) as caught:
            self.worker().start(prompt="", resume=False)
        self.assertEqual(str(caught.exception), "w1: claude exited 7 while starting")
        self.assertIsNone(self.stored())
        self.session.close.assert_not_called()
        self.assertEqual(Worker.load("w1").state.sid, SID)

    def test_an_early_exit_reports_the_last_lines_its_terminal_shows(self):
        self.session.events.return_value = iter([Exited(7)])
        shown = [f"line {n}" for n in range(worker.REPORT_LINES + 5)]
        self.session.screen.return_value = "\n \n".join(shown)
        with self.assertRaises(SessionError) as caught:
            self.worker().start(prompt="", resume=False)
        self.assertEqual(
            str(caught.exception),
            "\n".join(
                [
                    "w1: claude exited 7 while starting; its last lines:",
                    *shown[-worker.REPORT_LINES :],
                ]
            ),
        )
        self.session.screen.assert_called_once_with(worker.SCREEN_LINES)  # through its view

    def test_an_early_exit_on_a_terminal_that_cannot_show_reports_only_its_exit(self):
        self.session.events.return_value = iter([Exited(7)])
        self.session.screen.side_effect = NotShowable("session w1: its host cannot show")
        with self.assertRaises(SessionError) as caught:
            self.worker().start(prompt="", resume=False)
        self.assertEqual(str(caught.exception), "w1: claude exited 7 while starting")

    def test_an_early_exit_its_harness_cannot_show_reports_its_hosts_kept_lines(self):
        self.session.events.return_value = iter([Exited(2)])
        self.session.screen.side_effect = SessionError("session w1: worker exited")
        self.host.last_lines.return_value = "a\n\nb"
        with self.assertRaises(SessionError) as caught:
            self.worker().start(prompt="", resume=False)
        self.assertEqual(
            str(caught.exception), "w1: claude exited 2 while starting; its last lines:\na\nb"
        )
        self.host.last_lines.assert_called_once_with("w1", SID, None)
        self.host.last_lines.side_effect = SessionError("tmux sessions unknown")
        self.session.events.return_value = iter([Exited(2)])
        with self.assertRaises(SessionError) as caught:
            self.worker().start(prompt="", resume=False)
        self.assertEqual(str(caught.exception), "w1: claude exited 2 while starting")

    def test_early_exit_reads_guard_diagnostic_for_its_original_generation(self):
        self.session.events.return_value = iter([Exited(2)])
        self.session.screen.side_effect = SessionError("worker exited")
        self.session.generation = "g1"
        self.host.last_lines.return_value = "final error"
        with self.assertRaises(SessionError) as caught:
            self.worker().start(prompt="", resume=False)
        self.assertEqual(
            str(caught.exception),
            "w1: claude exited 2 while starting; its last lines:\nfinal error",
        )
        self.host.last_lines.assert_called_once_with("w1", SID, "g1")

    def test_the_watch_ends_on_the_first_event_once_its_client_is_up(self):
        type(self.session).client_up = mock.PropertyMock(side_effect=[False, True, True])
        self.session.events.return_value = iter([Reply("a"), Reply("b"), Exited(1)])
        self.worker().start(prompt="", resume=False)
        self.assertEqual(self.stored(), state().record())

    def test_an_exit_that_reaches_the_watch_past_every_deadline_still_fails_the_start(self):
        """The exit chain lagging: idle ticks well past EARLY and COLOR_CAP, its client never up."""
        self.session.client_up = False
        late = worker.COLOR_CAP * 2
        self.session.events.return_value = iter([*[IdleTimeout(worker.TICK)] * late, Exited(2)])
        ticks = itertools.count()
        with self.assertRaisesRegex(SessionError, "^w1: claude exited 2 while starting"):
            Worker(state(), session_cls=self.session_cls, clock=lambda: next(ticks)).start(
                prompt="", resume=False
            )
        self.assertGreater(next(ticks), late)
        self.assertIsNone(self.stored())

    def test_its_state_is_saved_before_its_client_starts(self):
        seen = []

        def launch(spec, **kw):
            seen.append(Worker.load("w1").state)
            return self.launched(spec, **kw)

        self.session_cls.start.side_effect = launch
        self.worker().start(prompt="", resume=False)
        self.assertEqual(seen, [state()])

    def test_a_start_killed_after_its_client_starts_is_resumed(self):
        replace = os.replace

        def killed(source, target):
            if os.path.basename(target) == "record":
                raise Killed
            replace(source, target)

        with mock.patch("os.replace", side_effect=killed), self.assertRaises(Killed):
            self.worker().start(prompt="", resume=False)
        self.assertIsNone(self.stored())
        self.owned()  # its harness runs on
        found = Worker.load("w1", session_cls=self.session_cls).resume()
        self.assertEqual(found, state().record())
        self.assertEqual(self.stored(), found)
        self.session_cls.start.assert_called_once()

    def test_a_failed_start_puts_back_the_state_and_latest_start_it_found(self):
        Worker(state(sid=OTHER), session_cls=self.session_cls).start(prompt="", resume=False)
        self.session_cls.start.side_effect = SessionError("tui session w1 is running")
        with self.assertRaisesRegex(SessionError, "^tui session w1 is running$"):
            self.worker().start(prompt="", resume=False)
        self.assertFalse(os.path.exists(state_path()))
        self.assertEqual(Worker.load("w1").state.sid, OTHER)
        self.session_cls.start.side_effect = self.launched
        self.worker().start(prompt="", resume=False)
        kept = read(state_path())
        self.session_cls.start.side_effect = ConfigError("unknown client claude")
        with self.assertRaises(ConfigError):
            self.worker(flags=()).start(prompt="", resume=True)
        self.assertEqual(read(state_path()), kept)
        self.assertEqual(Worker.load("w1").state.sid, SID)

    def test_a_state_it_cannot_save_starts_nothing(self):
        os.makedirs(pointer(), 0o700)
        with self.assertRaisesRegex(
            SessionError, f"^w1: {os.strerror(errno.EISDIR)}; nothing started$"
        ):
            self.worker().start(prompt="", resume=False)
        self.session_cls.start.assert_not_called()
        self.assertFalse(os.path.exists(state_path()))


def sid(n: int) -> str:
    return f"{n:08x}-0000-4000-8000-000000000000"


class ViewTest(Base):
    def setUp(self):
        super().setUp()
        self.session.showable = True
        self.session.attach_command.return_value = "tmux attach -t '=w1'"

    def start(self, *, stored: bool, view: bool | None) -> None:
        self.session.reset_mock()
        self.session.events.return_value = iter([IdleTimeout(worker.EARLY)])
        self.session.status.return_value = Status(
            name="w1",
            alive=True,
            state=StatusState.WORKING,
            outcome=None,
            attach=None,
            hidden=stored,
        )
        self.worker().start(prompt="", resume=False, hidden=view)

    def test_start_hides_shows_or_keeps_per_its_view(self):
        for view, stored, want in (
            (True, False, "hide"),
            (True, True, "hide"),
            (False, True, "show"),
            (False, False, "show"),
            (None, False, "show"),
            (None, True, None),
        ):
            with self.subTest(view=view, stored=stored):
                self.start(stored=stored, view=view)
                done = [call for call in ("show", "hide") if getattr(self.session, call).called]
                self.assertEqual(done, [] if want is None else [want])
                if want == "show":
                    self.session.show.assert_called_once_with(display="mgr")
                self.assertEqual(self.stored(), state().record())
        self.assertEqual(set(self.said), {"session w1: tmux attach -t '=w1'"})

    def test_a_failed_hide_is_said_and_the_start_goes_on(self):
        self.session.hide.side_effect = SessionError("boom")
        self.start(stored=False, view=True)
        self.assertEqual(self.said[-1], "session w1: not hidden: boom")
        self.assertEqual(self.stored(), state().record())

    def test_a_show_its_exited_client_refuses_leaves_only_the_early_exit(self):
        self.session.show.side_effect = SessionError("worker w1: exited")
        self.session.events.return_value = iter([Exited(2)])
        with self.assertRaisesRegex(SessionError, "^w1: claude exited 2 while starting"):
            self.worker().start(prompt="", resume=False, hidden=False)
        self.assertEqual(self.said, ["session w1: tmux attach -t '=w1'"])

    def test_a_failed_show_is_said_when_its_watch_fails(self):
        self.session.show.side_effect = SessionError("worker w1: gone")
        self.session.events.side_effect = SessionError("boom")
        with self.assertRaisesRegex(SessionError, "^boom$"):
            self.worker().start(prompt="", resume=False, hidden=False)
        self.assertEqual(
            self.said,
            ["session w1: tmux attach -t '=w1'", "session w1: not shown: worker w1: gone"],
        )
        self.assertIsNone(self.stored())

    def test_restart_and_resume_keep_its_view(self):
        self.session.events.return_value = iter([IdleTimeout(worker.EARLY)])
        self.session.status.return_value = Status(
            name="w1", alive=True, state=StatusState.WORKING, outcome=None, attach=None, hidden=True
        )
        started = self.worker()
        started.start(prompt="", resume=False, hidden=True)
        self.session.reset_mock()
        self.session.events.return_value = iter([IdleTimeout(worker.EARLY)])
        self.owned()
        self.session_cls.retire.side_effect = None  # its owner's confirmed close
        Worker.load("w1", session_cls=self.session_cls, say=self.said.append).restart()
        self.session.show.assert_not_called()
        self.session.hide.assert_not_called()


class EnvironmentTest(Base):
    def test_credential_declarations_are_stored_and_replayed(self):
        values = {"credentials": ' {"future-kind": [1]}\n', "environment_credentials": "{"}
        self.worker(**values).start(prompt="", resume=False)
        for again in ("resume", "restart"):
            self.session_cls.start.reset_mock()
            self.session.events.return_value = iter([IdleTimeout(worker.EARLY)])
            if again == "restart":
                self.owned()
                self.session_cls.retire.side_effect = None
            loaded = Worker.load("w1", session_cls=self.session_cls, say=self.said.append)
            self.assertEqual(
                WorkerState.from_json(json.loads(loaded.state.to_json())), loaded.state
            )
            getattr(loaded, again)()
            spec = self.session_cls.start.call_args.args[0]
            self.assertEqual({key: getattr(spec, key) for key in values}, values)

    def test_environment_is_stored_and_reused(self):
        found = state(environment=ENVIRONMENT)
        data = json.loads(found.to_json())
        self.assertEqual(data["environment"], ENVIRONMENT.persisted())
        self.assertEqual(WorkerState.from_json(data), found)
        self.assertIs(found.spec(prompt="", resume=True).environment, ENVIRONMENT)
        self.assertIsNone(json.loads(state().to_json())["environment"])
        self.worker(environment=ENVIRONMENT).start(prompt="", resume=False)
        for again in ("resume", "restart"):
            self.session_cls.start.reset_mock()
            self.session.events.return_value = iter([IdleTimeout(worker.EARLY)])
            if again == "restart":
                self.owned()
                self.session_cls.retire.side_effect = None
            loaded = Worker.load("w1", session_cls=self.session_cls, say=self.said.append)
            with self.subTest(again):
                getattr(loaded, again)()
                spec = self.session_cls.start.call_args.args[0]
                self.assertEqual((spec.environment, spec.resume), (ENVIRONMENT, True))

    def test_a_bad_environment_is_a_config_error(self):
        with self.assertRaisesRegex(ConfigError, "environment"):
            state(environment=ENVIRONMENT.persisted())
        data = json.loads(state().to_json())
        for value in ({"image": "x"}, "dummy", []):
            with self.subTest(value), self.assertRaises(ConfigError):
                WorkerState.from_json({**data, "environment": value})

    def test_pre_upgrade_worker_json_loads_without_environment(self):
        data = json.loads(state().to_json())
        del data["environment"]
        found = WorkerState.from_json(data)
        self.assertIsNone(found.environment)
        self.assertIsNone(found.spec(prompt="", resume=True).environment)

    def test_worker_json_requires_both_credential_fields(self):
        for key in ("credentials", "environment_credentials"):
            data = json.loads(state().to_json())
            del data[key]
            with self.subTest(key=key), self.assertRaises(ConfigError):
                WorkerState.from_json(data)

    def test_restart_while_held_raises_session_busy(self):
        self.worker(environment=ENVIRONMENT).start(prompt="", resume=False)
        busy = SessionBusy("dummy-lock", "ensemble-other-x (owner other)")
        self.session_cls.start.side_effect = busy
        self.owned()
        self.session_cls.retire.side_effect = None
        loaded = Worker.load("w1", session_cls=self.session_cls, say=self.said.append)
        with self.assertRaises(SessionBusy) as raised:
            loaded.restart()
        self.assertIs(raised.exception, busy)


class StaleTest(Base):
    def setUp(self):
        super().setUp()
        self.root = os.path.dirname(state().workdir())

    def planted(self, sid: str, days: float, *, latest: str | None = None) -> str:
        """workers/<sid>/worker.json `days` old; `latest`: by-name/<latest> names it."""
        path = os.path.join(self.root, sid)
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, worker.STATE_FILE), "w", encoding="utf-8") as f:
            f.write("{}")
        when = time.time() - days * 86400
        os.utime(os.path.join(path, worker.STATE_FILE), (when, when))
        if latest is not None:
            os.makedirs(os.path.join(self.root, worker.BY_NAME), exist_ok=True)
            with open(os.path.join(self.root, worker.BY_NAME, latest), "w", encoding="utf-8") as f:
                f.write(sid + "\n")
        return path

    def test_a_start_drops_only_stale_unheld_non_latest_sids(self):
        self.planted(sid(1), 31)
        self.planted(sid(2), 31, latest="w1")
        held = self.planted(sid(3), 31)
        self.planted(sid(4), 31, latest="w2")
        self.planted(sid(5), 29)
        with file_lock(os.path.join(held, "worker")):
            self.worker().start(prompt="", resume=False)
        self.assertEqual(
            sorted(os.listdir(self.root)),
            sorted([worker.BY_NAME, SID, sid(2), sid(3), sid(4), sid(5)]),
        )
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, worker.BY_NAME))), ["w1", "w2"])
        self.assertEqual(self.said, [])

    def test_odd_entries_stay(self):
        bare = os.path.join(self.root, sid(1))
        os.makedirs(bare)
        old = time.time() - 31 * 86400
        os.utime(bare, (old, old))
        elsewhere = self.planted(sid(2), 31)
        os.rename(elsewhere, os.path.expanduser("~/elsewhere"))
        os.symlink(os.path.expanduser("~/elsewhere"), os.path.join(self.root, sid(2)))
        self.planted("junk", 31)
        self.worker().start(prompt="", resume=False)
        self.assertEqual(
            sorted(os.listdir(self.root)), sorted([worker.BY_NAME, SID, sid(1), sid(2), "junk"])
        )
        self.assertEqual(os.listdir(os.path.expanduser("~/elsewhere")), [worker.STATE_FILE])
        self.assertEqual(self.said, [])

    def test_a_symlinked_lock_is_never_followed(self):
        path = self.planted(sid(1), 31)
        target = os.path.expanduser("~/outside.lock")
        os.symlink(target, os.path.join(path, "worker.lock"))
        self.worker().start(prompt="", resume=False)
        self.assertIn(sid(1), os.listdir(self.root))
        self.assertFalse(os.path.lexists(target))

    def test_a_pointer_it_cannot_read_drops_nothing(self):
        self.planted(sid(1), 31)
        os.makedirs(os.path.join(self.root, worker.BY_NAME, "w3"))
        self.worker().start(prompt="", resume=False)
        self.assertIn(sid(1), os.listdir(self.root))
        pointers = os.path.join(self.root, worker.BY_NAME)
        self.assertEqual(self.said, [f"worker cleanup skipped: {pointers}/w3: unreadable"])

    def test_pointers_it_cannot_list_drop_nothing(self):
        self.planted(sid(1), 31)
        pointers = os.path.join(self.root, worker.BY_NAME)
        refused = PermissionError(errno.EACCES, "refused", pointers)
        real = os.listdir

        def listdir(path="."):
            if path == pointers:
                raise refused
            return real(path)

        with mock.patch("os.listdir", side_effect=listdir):
            self.worker().start(prompt="", resume=False)
        self.assertIn(sid(1), os.listdir(self.root))
        self.assertEqual(self.said, [f"worker cleanup skipped: {refused}"])

    def test_a_workers_dir_it_cannot_list_is_said_and_the_start_goes_on(self):
        self.planted(sid(1), 31)
        refused = PermissionError(errno.EACCES, "refused", self.root)
        real = os.scandir

        def scandir(path="."):
            if path == self.root:
                raise refused
            return real(path)

        with mock.patch("os.scandir", side_effect=scandir):
            self.worker().start(prompt="", resume=False)
        self.assertIn(sid(1), os.listdir(self.root))
        self.assertEqual(self.said, [f"worker cleanup: {self.root}: {refused}"])

    def test_a_drop_error_is_said_and_the_start_goes_on(self):
        path = self.planted(sid(1), 31)
        refused = OSError(errno.EPERM, "refused", path)
        with mock.patch("shutil.rmtree", side_effect=refused):
            record = self.worker().start(prompt="", resume=False)
        self.assertEqual(record, state().record())
        self.assertIn(sid(1), os.listdir(self.root))
        self.assertEqual(self.said, [f"worker cleanup: {path}: {refused}"])

    def test_a_failed_start_keeps_the_previous_latest(self):
        Worker(state(sid=OTHER), session_cls=self.session_cls).start(prompt="", resume=False)
        old = time.time() - 31 * 86400
        os.utime(os.path.join(self.root, OTHER, worker.STATE_FILE), (old, old))
        self.session_cls.start.side_effect = SessionError("tui session w1 is running")
        with self.assertRaises(SessionError):
            self.worker().start(prompt="", resume=False)
        self.assertEqual(Worker.load("w1").state.sid, OTHER)


class ColorWatchTest(Base):
    UNSET = "session w1: color not set: {}; ensemble-session send w1 '/color'"

    def watch(self, sent: float, events=None, text="/color", dialog=None, prompt="") -> int:
        """Starts w1 with `prompt` on a clock ticking 1 s per call, its opening `text` pending
        until tick `sent`, `dialog` in its state; the ticks used. opening_pending's call count
        varies by Python (3.11's isinstance calls it).
        """
        ticks = itertools.count()
        now = [0]

        def tick():
            now[0] = next(ticks)
            return now[0]

        type(self.session).opening_pending = mock.PropertyMock(
            side_effect=lambda: text if now[0] < sent else None
        )
        type(self.session).client_up = mock.PropertyMock(side_effect=lambda: now[0] >= worker.EARLY)
        type(self.session).dialog = mock.PropertyMock(return_value=dialog)
        self.session.events.return_value = events or itertools.repeat(IdleTimeout(worker.TICK))
        Worker(state(), session_cls=self.session_cls, say=self.said.append, clock=tick).start(
            prompt=prompt, resume=False
        )
        return next(ticks)

    def test_an_exit_in_the_launch_turn_after_up_and_the_opening_fails_the_start(self):
        idle = IdleTimeout(worker.TICK)
        with self.assertRaisesRegex(SessionError, "^w1: claude exited 0 while starting"):
            self.watch(6, events=iter([TurnStarted(), *[idle] * 7, Exited(0)]), prompt="hi")
        self.assertEqual(self.said, [])
        self.assertIsNone(self.stored())

    def test_the_launch_turn_ending_or_hitting_a_limit_ends_the_watch(self):
        for ending in (TurnEnd("hi"), LimitHit()):
            with self.subTest(ending=ending):
                with contextlib.suppress(FileNotFoundError):
                    self.unstore()
                idle = IdleTimeout(worker.TICK)
                events = itertools.chain(
                    [TurnStarted(), *[idle] * 7, ending], itertools.repeat(idle)
                )
                self.assertEqual(self.watch(6, events=events, prompt="hi"), 10)
                self.assertEqual(self.said, [])
                self.assertEqual(self.stored(), state().record())

    def test_a_launch_turn_still_running_at_the_cap_starts_quietly(self):
        idle = itertools.repeat(IdleTimeout(worker.TICK))
        events = itertools.chain([TurnStarted()], idle)
        self.assertEqual(self.watch(6, events=events, prompt="hi"), worker.COLOR_CAP + 1)
        self.assertEqual(self.said, [])
        self.assertEqual(self.stored(), state().record())

    def test_a_dialog_ends_the_launch_watch_at_up(self):
        events = itertools.chain([TurnStarted()], itertools.repeat(IdleTimeout(worker.TICK)))
        self.assertEqual(
            self.watch(0, events=events, dialog="Trust", prompt="hi"), worker.EARLY + 1
        )
        self.assertEqual(self.said, ["session w1: waits on a dialog: Trust"])

    def test_the_watch_returns_once_sent_by_the_early_deadline_else_at_the_cap(self):
        for sent, ticks, said in (
            (0, worker.EARLY + 1, []),
            (8, 9, []),
            (float("inf"), worker.COLOR_CAP + 1, [self.UNSET.format("not ready within 15 s")]),
        ):
            with self.subTest(sent=sent):
                self.said.clear()
                with contextlib.suppress(FileNotFoundError):
                    self.unstore()
                self.assertEqual(self.watch(sent), ticks)
                self.assertEqual(self.said, said)
                self.assertEqual(self.stored(), state().record())

    def test_a_started_turn_keeps_the_watch_as_long_and_says_unset_at_the_cap(self):
        for sent, ticks, said in (
            (8, 9, []),
            (float("inf"), worker.COLOR_CAP + 1, [self.UNSET.format("not ready within 15 s")]),
        ):
            with self.subTest(sent=sent):
                self.said.clear()
                with contextlib.suppress(FileNotFoundError):
                    self.unstore()
                events = itertools.chain(
                    [TurnStarted()], itertools.repeat(IdleTimeout(worker.TICK))
                )
                self.assertEqual(self.watch(sent, events=events), ticks)
                self.assertEqual(self.said, said)
                self.assertEqual(self.stored(), state().record())

    def test_a_dialog_just_after_a_turn_started_once_up_is_said(self):
        idle = IdleTimeout(worker.TICK)
        events = itertools.chain(
            [idle] * (worker.EARLY + 1), [TurnStarted(), Blocked("Edit")], itertools.repeat(idle)
        )
        self.watch(float("inf"), events=events)
        self.assertEqual(
            self.said,
            ["session w1: waits on a dialog: Edit", self.UNSET.format("waits on a dialog")],
        )
        self.assertEqual(self.stored(), state().record())

    def test_a_dialog_in_its_state_is_said_wherever_the_watch_ends(self):
        """Its Blocked not yet read: up with no opening, its opening sent, or at the cap."""
        said = "session w1: waits on a dialog: Edit"
        for case, sent, turned, ticks, lines in (
            ("no opening", 0, False, worker.EARLY + 1, [said]),
            ("no opening, a turn started", 0, True, worker.EARLY + 1, [said]),
            ("its opening sent", 8, False, 9, [said]),
            ("its opening sent, a turn started", 8, True, 9, [said]),
            (
                "at the cap",
                float("inf"),
                False,
                worker.COLOR_CAP + 1,
                [said, self.UNSET.format("waits on a dialog")],
            ),
            (
                "at the cap, a turn started",
                float("inf"),
                True,
                worker.COLOR_CAP + 1,
                [said, self.UNSET.format("waits on a dialog")],
            ),
        ):
            with self.subTest(case=case):
                self.said.clear()
                with contextlib.suppress(FileNotFoundError):
                    self.unstore()
                idle = itertools.repeat(IdleTimeout(worker.TICK))
                events = itertools.chain([TurnStarted()], idle) if turned else idle
                self.assertEqual(self.watch(sent, events=events, dialog="Edit"), ticks)
                self.assertEqual(self.said, lines)
                self.assertEqual(self.stored(), state().record())

    def test_a_dialog_while_pending_says_both(self):
        self.watch(float("inf"), events=iter([Blocked("Trust")]))
        self.assertEqual(
            self.said,
            ["session w1: waits on a dialog: Trust", self.UNSET.format("waits on a dialog")],
        )
        self.assertEqual(self.stored(), state().record())

    def test_the_line_names_the_pending_text(self):
        self.watch(float("inf"), text="/x 'y'")
        self.assertEqual(
            self.said,
            [
                "session w1: color not set: not ready within 15 s; "
                "ensemble-session send w1 '/x '\\''y'\\'''"
            ],
        )

    def test_an_exit_while_pending_fails_without_the_color_line(self):
        with self.assertRaises(SessionError):
            self.watch(float("inf"), events=iter([Exited(1)]))
        self.assertEqual(self.said, [])
        self.assertIsNone(self.stored())

    def plain(self, *events, alive: bool) -> mock.Mock:
        """A session no guard reports up, its client running as `alive` says; its session alive."""
        plain = mock.Mock(
            spec=["showable", "events", "alive", "client_alive", "screen"], showable=False
        )
        plain.alive.return_value = True
        plain.screen.side_effect = SessionError("session w1: its harness is gone")
        plain.client_alive.return_value = alive
        plain.events.return_value = iter(events) if events else itertools.repeat(IdleTimeout(1))
        self.session_cls.start.return_value = plain
        return plain

    def test_a_session_without_a_guard_returns_once_its_client_runs_at_the_early_deadline(self):
        plain = self.plain(alive=True)
        ticks = itertools.count()
        Worker(
            state(), session_cls=self.session_cls, say=self.said.append, clock=lambda: next(ticks)
        ).start(prompt="", resume=False)
        self.assertEqual(next(ticks), worker.EARLY + 1)
        self.assertEqual(self.said, [])
        plain.client_alive.assert_called_once_with()

    def test_a_session_without_a_guard_whose_client_ended_waits_for_its_exit(self):
        """Its session still alive (a kept dead pane), its exit not yet polled."""
        late = worker.COLOR_CAP * 2
        plain = self.plain(*[IdleTimeout(worker.TICK)] * late, Exited(2), alive=False)
        ticks = itertools.count()
        with self.assertRaisesRegex(SessionError, "^w1: claude exited 2 while starting"):
            Worker(state(), session_cls=self.session_cls, clock=lambda: next(ticks)).start(
                prompt="", resume=False
            )
        self.assertEqual(plain.client_alive.call_count, late - worker.EARLY + 1)
        plain.alive.assert_not_called()
        self.assertIsNone(self.stored())

    def test_a_plain_session_returns_once_alive_at_the_early_deadline(self):
        plain = mock.create_autospec(Session, instance=True)
        plain.showable = False
        plain.alive.return_value = True
        plain.events.return_value = itertools.repeat(IdleTimeout(worker.TICK))
        self.session_cls.start.return_value = plain
        ticks = itertools.count()
        Worker(state(), session_cls=self.session_cls, clock=lambda: next(ticks)).start(
            prompt="", resume=False
        )
        self.assertEqual(next(ticks), worker.EARLY + 1)


class LoadTest(Base):
    def test_by_its_latest_start_or_a_given_sid_of_its_own(self):
        self.worker().start(prompt="", resume=False)
        self.assertEqual(Worker.load("w1").state.sid, SID)
        self.assertEqual(Worker.load("w1", sid=SID).state.name, "w1")
        with self.assertRaisesRegex(ConfigError, f"^sid {SID} is worker w1's, not w2's$"):
            Worker.load("w2", sid=SID)
        with self.assertRaisesRegex(ConfigError, f"^sid {OTHER} is not worker w1's"):
            Worker.load("w1", sid=OTHER)
        Worker(state(sid=OTHER), session_cls=self.session_cls).start(prompt="", resume=False)
        self.assertEqual(Worker.load("w1", sid=OTHER).state.sid, OTHER)
        with self.assertRaisesRegex(
            ConfigError, f"^sid {SID} is not worker w1's .its latest: {OTHER}"
        ):
            Worker.load("w1", sid=SID)
        for sid in ("..", SID.upper()):
            with self.subTest(sid=sid), self.assertRaises(ConfigError):
                Worker.load("w1", sid=sid)

    def test_a_start_with_another_workers_sid_is_refused_before_anything(self):
        self.worker().start(prompt="", resume=False)
        with self.assertRaisesRegex(ConfigError, f"^sid {SID} is worker w1's$"):
            self.worker(name="w2").start(prompt="", resume=True)
        self.session_cls.start.assert_called_once()
        self.assertEqual(Worker.load("w1").state, state())
        with self.assertRaisesRegex(SessionError, "^w2: not a worker"):
            Worker.load("w2")

    def test_of_two_starts_claiming_one_sid_at_once_only_the_first_starts(self):
        launching, go = threading.Event(), threading.Event()

        def launch(spec, **kw):
            launching.set()
            go.wait(10)
            return self.launched(spec, **kw)

        def run(each: Worker) -> Record:
            with each.locked():
                return each.start(prompt="", resume=False)

        self.session_cls.start.side_effect = launch
        first, second = self.worker(), self.worker(name="w2")
        first.check(prompt="", resume=False)
        with ThreadPoolExecutor(2) as pool:
            started = pool.submit(run, first)
            self.assertTrue(launching.wait(10))
            second.check(prompt="", resume=False)
            refused = pool.submit(run, second)
            go.set()
            self.assertEqual(started.result(10).name, "w1")
            with self.assertRaisesRegex(ConfigError, f"^sid {SID} is worker w1's$"):
                refused.result(10)
        self.session_cls.start.assert_called_once()

    def test_no_or_unreadable_state_is_not_a_worker(self):
        with self.assertRaisesRegex(SessionError, "^w1: not a worker"):
            Worker.load("w1")
        with self.assertRaisesRegex(SessionError, f"^w1: no worker {OTHER}"):
            Worker.load("w1", sid=OTHER)
        self.worker().start(prompt="", resume=False)
        path = os.path.join(state().workdir(), worker.STATE_FILE)
        for text in ("{", "[]", json.dumps({"version": 1})):
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            with self.subTest(text=text), self.assertRaisesRegex(SessionError, "bad worker.json"):
                Worker.load("w1")
        with self.assertRaises(ConfigError):
            Worker.load("a b")


class ResumeTest(Base):
    def test_a_worker_no_harness_owns_is_resumed_with_no_prompt_closing_nothing(self):
        self.worker().resume()
        self.assertEqual(self.calls(), ["preflight", "start"])
        self.assertEqual(self.stored(), state().record())
        self.session_cls.retire.assert_not_called()
        spec = self.session_cls.start.call_args.args[0]
        self.assertEqual((spec.prompt, spec.resume, spec.sid), ("", True, SID))

    def test_a_start_it_cannot_make_is_refused_before_the_old_one_is_touched(self):
        self.session_cls.preflight.side_effect = ConfigError("unknown client claude")
        with self.assertRaisesRegex(ConfigError, "unknown client"):
            self.worker().resume()
        self.assertEqual(self.calls(), ["preflight"])

    def test_another_sessions_owner_is_never_asked_or_closed(self):
        self.owned(sid=OTHER)
        with self.assertRaisesRegex(SessionError, "^w1: no session to restart; resume starts it$"):
            self.worker().restart()
        self.assertEqual(self.calls(), [])
        self.worker().resume()
        self.assertEqual(self.calls(), ["preflight", "start"])
        self.assertEqual(self.stored(), state().record())
        self.assertEqual(self.asked, [])
        self.session_cls.retire.assert_not_called()


class RestartTest(Base):
    def test_a_start_refused_once_the_old_session_is_gone_is_no_longer_a_config_error(self):
        self.owned()
        self.session_cls.retire.side_effect = None
        self.session_cls.start.side_effect = ConfigError("unknown client claude")
        with self.assertRaisesRegex(
            SessionError, "^w1: old session closed, none started: unknown client claude$"
        ):
            self.worker().restart()

    def test_no_session_is_refused_untouched(self):
        with self.assertRaisesRegex(SessionError, "^w1: no session to restart; resume starts it$"):
            self.worker().restart()
        self.assertEqual(self.calls(), [])


class OwnedTest(Base):
    """A worker its session harness owns (session.json names the harness and its sid): resume asks
    its running owner, else starts a new harness through the gate; it never closes.
    """

    def test_owned_resume_with_running_owner_starts_and_closes_nothing(self):
        self.owned()
        self.session_cls.attach.return_value.showable = False
        self.assertEqual(self.worker().resume(), state().record())
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.stored(), state().record())
        self.session_cls.retire.assert_not_called()
        self.unstore()
        self.assertEqual(self.worker().resume(), state().record())
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.stored(), state().record())
        self.assertEqual(self.asked, [])
        self.assertEqual(self.said, [])

    def test_resume_owned_shows_unless_hidden_without_asking_the_harness(self):
        """A display lost with its tmux server comes back; the harness is asked nothing."""
        self.owned()
        view = self.session_cls.attach.return_value
        view.showable = True
        view.attach_command.return_value = "tmux attach -t '=w1'"
        view.status.return_value = Status(
            name="w1",
            alive=True,
            state=StatusState.WORKING,
            outcome=None,
            attach=None,
            hidden=False,
        )
        self.assertEqual(self.worker().resume(), state().record())
        self.assertEqual(self.asked, [])
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.stored(), state().record())
        self.session_cls.attach.assert_called_once_with("w1", sid=SID, host="tmux")
        view.show.assert_called_once_with(display="mgr")
        self.assertEqual(self.said, ["session w1: tmux attach -t '=w1'"])
        self.session_cls.retire.assert_not_called()

    def test_owned_resume_keeps_a_hidden_worker_hidden(self):
        self.owned()
        view = self.session_cls.attach.return_value
        view.showable = True
        view.status.return_value = Status(
            name="w1", alive=True, state=StatusState.WORKING, outcome=None, attach=None, hidden=True
        )
        self.worker().resume()
        view.show.assert_not_called()
        view.hide.assert_not_called()

    def test_owned_resume_says_an_unshown_worker_and_returns_its_record(self):
        self.owned()
        self.session_cls.attach.side_effect = SessionError("no tui session w1")
        self.assertEqual(self.worker().resume(), state().record())
        self.assertEqual(self.said, ["session w1: not shown: no tui session w1"])
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.stored(), state().record())

    def test_owned_resume_without_owner_starts_and_closes_nothing(self):
        self.owned(None)
        self.worker().resume()
        self.assertEqual(self.calls(), ["preflight", "start"])
        self.assertEqual(self.stored(), state().record())
        self.session_cls.retire.assert_not_called()
        spec = self.session_cls.start.call_args.args[0]
        self.assertEqual((spec.prompt, spec.resume, spec.sid), ("", True, SID))

    def test_owned_restart_closes_through_the_owner(self):
        self.owned()
        self.session_cls.retire.side_effect = None  # its owner's confirmed close
        self.worker().restart()
        self.session_cls.retire.assert_called_once_with("w1", host="tmux")
        self.assertEqual(self.calls(), ["start"])
        self.assertEqual(self.stored(), state().record())
        self.assertTrue(self.session_cls.start.call_args.args[0].resume)

    def test_owned_restart_without_owner_refused(self):
        self.owned(None)
        with self.assertRaisesRegex(SessionError, "^w1: no session to restart; resume starts it$"):
            self.worker().restart()
        self.assertEqual(self.calls(), [])
        self.session_cls.retire.assert_not_called()

    def test_exited_owner_is_not_running(self):
        self.owned("exited")
        self.worker().resume()
        self.assertEqual(self.calls(), ["preflight", "start"])
        self.assertEqual(self.stored(), state().record())


class RecordTest(Base):
    """Its Record lives in its worker dir, `<generation>\t<Record line>`, for the generation its
    session.json names, whatever its Host can store.
    """

    def test_start_and_resume_store_record(self):
        found = self.worker().start(prompt="", resume=False)
        path = os.path.join(self.tui, "record")
        self.assertEqual(read(path), f"g1\t{found.to_line()}\n".encode())
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.unstore()
        self.owned(generation="g2")
        self.assertEqual(self.worker().resume(), found)
        self.assertEqual((self.stored("g2"), self.stored("g1")), (found, None))
        self.assertEqual(self.calls(), ["start"])  # the resume started nothing

    def test_other_generation_reads_none(self):
        found = self.worker().start(prompt="", resume=False)
        self.write_state(SID, generation="g2")
        self.assertIsNone(self.stored("g2"))
        with self.assertRaisesRegex(SessionError, "^session w1 is gone$"):
            TuiStore.attach("w1", host=self.host).store_record("g1", found)
        self.assertEqual(self.stored("g1"), found)  # kept, read only as g1's
        self.owned(sid=OTHER)
        self.unstore()
        self.worker().resume()  # another sid's session: a resumed start of its own
        self.assertEqual(self.stored(), found)

    def test_a_remote_tui_host_starts_and_resumes_with_the_record_in_its_worker_dir(self):
        self.session_cls.host.return_value = RemoteHost("docker", {})
        found = self.worker(host="docker").start(prompt="", resume=False)
        self.assertEqual(self.stored(), found)
        self.unstore()
        self.owned()
        self.assertEqual(self.worker(host="docker").resume(), found)
        self.assertEqual(self.stored(), found)

    def test_cli_start_and_resume_on_a_docker_like_host_exit_0(self):
        with open(os.path.join(self.ensemble, "config.local.toml"), "w", encoding="utf-8") as f:
            f.write(
                "[session.hosts.docker]\n"
                'implementation = "tests.session.unit.engine.worker_test:RemoteHost"\n'
            )
        self.addCleanup(setattr, HostedSession, "_setup", None)
        self.session_cls.host.return_value = RemoteHost("docker", {})
        session_cls, said = self.session_cls, self.said

        class Bound(Worker):
            def __init__(self, state: WorkerState, **kw) -> None:
                super().__init__(state, session_cls=session_cls, say=said.append, **kw)

        def invoke(*argv: str) -> tuple[int, str, str]:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(list(argv), worker_cls=Bound)
            return code, out.getvalue(), err.getvalue()

        argv = ("start", "w1", "--client", "claude", "--cwd", self.home, "--display", "none")
        code, out, err = invoke(*argv, "--host", "docker")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, f"{self.stored().to_line()}\n")
        self.assertEqual(Record.parse(out), Worker.load("w1").state.record())
        self.unstore()
        self.owned()
        self.assertEqual(invoke("resume", "w1"), (0, out, ""))
        self.assertEqual(self.stored(), Record.parse(out))
