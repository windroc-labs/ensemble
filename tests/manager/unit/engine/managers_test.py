"""Every manager's holder state, session record and entries, read without a lease."""

import json
import os
from pathlib import Path
from unittest import mock

import jsonschema

from ensemble.manager import rearm
from ensemble.manager.engine.managers import (
    alive,
    managers,
    plan,
    read,
    read_record,
    resume_target,
    write_record,
)
from ensemble.manager.engine.roster import Roster
from ensemble.manager.engine.types import (
    ActiveHolder,
    HolderState,
    Manager,
    Plan,
    Refused,
    RosterError,
    SessionRecord,
    Unread,
    Unreadable,
    Verb,
)
from tests.manager import session_records
from tests.manager.roster_case import RosterCase
from tests.utils.process_fixtures import DEVNULL, CompletedProcess, TimeoutExpired

LIVE, DEAD = 4101, 4102


def _kill(pid: int, _signal: int) -> None:
    if pid != LIVE:
        raise ProcessLookupError(pid)


class RecordTest(RosterCase):
    def setUp(self):
        super().setUp()
        Path(self.directory).mkdir(parents=True, mode=0o700)
        self.path = os.path.join(self.directory, "session.json")

    def test_an_absent_record_is_none(self):
        self.assertIsNone(read_record(self.path))

    def test_valid_records_parse(self):
        for client, record in session_records.VALID.items():
            with self.subTest(client=client):
                Path(self.path).write_text(json.dumps(record) + "\n")
                self.assertEqual(read_record(self.path), SessionRecord(**record))

    def test_invalid_records_are_errors(self):
        for case, text in session_records.INVALID.items():
            with self.subTest(case=case):
                Path(self.path).write_text(text)
                with self.assertRaisesRegex(ValueError, f"^{self.path}: "):
                    read_record(self.path)

    def test_a_directory_or_oversized_record_is_an_error(self):
        Path(self.path).write_text(" " * (1 << 16) + json.dumps(session_records.VALID["claude"]))
        with self.assertRaisesRegex(ValueError, "not a readable regular file"):
            read_record(self.path)
        os.unlink(self.path)
        os.mkdir(self.path)
        with self.assertRaisesRegex(ValueError, "not a readable regular file"):
            read_record(self.path)

    def test_what_the_hook_writes_reads_back(self):
        for client, call in session_records.CALLS.items():
            with self.subTest(client=client):
                found = rearm.options(call.argv)
                self.assertIsNotNone(found)
                written = rearm.record(found, call.stdin)
                Path(self.path).write_text(json.dumps(written) + "\n")
                self.assertEqual(read_record(self.path), SessionRecord(**call.record))

    def test_verdicts_match_the_schema(self):
        with open(session_records.SCHEMA, encoding="utf-8") as f:
            validator = jsonschema.Draft202012Validator(json.load(f))
        texts = [json.dumps(r) for r in session_records.VALID.values()]
        for text in [*texts, *session_records.INVALID.values()]:
            with self.subTest(text=text):
                try:
                    schema_ok = validator.is_valid(json.loads(text))
                except json.JSONDecodeError:
                    schema_ok = False
                Path(self.path).write_text(text)
                try:
                    read_record(self.path)
                except ValueError:
                    reader_ok = False
                else:
                    reader_ok = True
                self.assertEqual(reader_ok, schema_ok)


class ReadTest(RosterCase):
    def test_dead_launch_does_not_hide_another_clients_live_record(self):
        self.write_record(LIVE)
        with Roster(self.directory).hold("owner") as lease:
            binding = {
                "client": "codex",
                "sid": None,
                "cwd": self.home,
                "pid": DEAD,
                "birth": "old",
                "holder_pid": 41,
                "holder_id": lease.id,
                "holder_generation": lease.generation,
            }
            path = Path(self.directory, "live-client.json")
            path.write_text(json.dumps(binding))
            self.assertEqual(
                read(self.directory, running=rearm.client_running).state, HolderState.CLIENT_RUNNING
            )

    def test_dead_or_stale_binding_does_not_trust_a_recycled_record_pid(self):
        self.write_record(LIVE)
        record_path = Path(self.directory, "session.json")
        record_path.write_text(
            json.dumps({**json.loads(record_path.read_text()), "client": "codex"})
        )
        with Roster(self.directory).hold("owner") as lease:
            binding = {
                "client": "codex",
                "sid": "saved",
                "cwd": self.home,
                "pid": LIVE,
                "birth": "old",
                "holder_pid": 41,
                "holder_id": lease.id,
                "holder_generation": lease.generation,
            }
            path = Path(self.directory, "live-client.json")
            path.write_text(json.dumps(binding))
            self.assertEqual(
                read(self.directory, running=rearm.client_running).state, HolderState.HOLD_ONLY
            )
            path.write_text("{")
            self.assertEqual(
                read(self.directory, running=rearm.client_running).state, HolderState.HOLD_ONLY
            )

    def test_live_launch_is_client_running_before_the_first_hook(self):
        self.write_record(DEAD)
        with Roster(self.directory).hold("owner") as lease:
            binding = {
                "client": "codex",
                "sid": None,
                "cwd": self.home,
                "pid": LIVE,
                "birth": "launched",
                "holder_pid": 41,
                "holder_id": lease.id,
                "holder_generation": lease.generation,
            }
            Path(self.directory, "live-client.json").write_text(json.dumps(binding))
            real_isdir = os.path.isdir

            def ps(argv, **_options):
                return mock.Mock(
                    returncode=0,
                    stdout=b"launched\n"
                    if "-p" in argv
                    else f"{LIVE} 41 codex\n41 1 hold\n".encode(),
                )

            with (
                mock.patch(
                    "os.path.isdir",
                    side_effect=lambda path: False if path == "/proc/self" else real_isdir(path),
                ),
                mock.patch("subprocess.run", side_effect=ps),
            ):
                self.assertEqual(
                    read(self.directory, running=rearm.client_running).state,
                    HolderState.CLIENT_RUNNING,
                )
                self.assertEqual(
                    read_record(os.path.join(self.directory, "session.json")).pid, DEAD
                )
            with mock.patch("subprocess.run", return_value=mock.Mock(returncode=1, stdout=b"")):
                self.assertEqual(
                    read(self.directory, running=rearm.client_running).state, HolderState.HOLD_ONLY
                )

    def setUp(self):
        super().setUp()
        self.live()
        self.enterContext(mock.patch("os.kill", side_effect=_kill))

    def write_record(self, pid: int) -> SessionRecord:
        record = {**session_records.VALID["claude"], "pid": pid}
        Path(self.directory, "session.json").write_text(json.dumps(record))
        return SessionRecord(**record)

    def test_alive(self):
        self.assertTrue(alive(LIVE))
        self.assertFalse(alive(DEAD))
        with mock.patch("os.kill", side_effect=PermissionError):
            self.assertTrue(alive(1))
        with mock.patch("os.kill", side_effect=OverflowError):
            self.assertFalse(alive(1 << 31))

    def test_a_pid_beyond_any_process_is_hold_only(self):
        self.write_record(1 << 31)
        with (
            mock.patch("os.kill", side_effect=OverflowError),
            Roster(self.directory).hold("owner"),
        ):
            self.assertEqual(read(self.directory).state, HolderState.HOLD_ONLY)

    def test_unlocked_is_not_held_even_with_a_live_record(self):
        record = self.write_record(LIVE)
        self.assertEqual(
            read(self.directory),
            Manager("manager", HolderState.NOT_HELD, None, record, self.roster.entries()),
        )

    def test_locked_with_a_live_pid_is_client_running(self):
        record = self.write_record(LIVE)
        with Roster(self.directory).hold("owner") as lease:
            manager = read(self.directory)
        holder = ActiveHolder(lease.id, "owner")
        self.assertEqual(
            manager,
            Manager("manager", HolderState.CLIENT_RUNNING, holder, record, self.roster.entries()),
        )

    def test_locked_with_a_dead_pid_or_no_record_is_hold_only(self):
        with Roster(self.directory).hold("owner"):
            self.assertEqual(read(self.directory).state, HolderState.HOLD_ONLY)
            self.write_record(DEAD)
            self.assertEqual(read(self.directory).state, HolderState.HOLD_ONLY)

    def test_an_invalid_record_is_never_client_running(self):
        Path(self.directory, "session.json").write_text(session_records.INVALID["extra field"])
        self.assertEqual(read(self.directory).state, HolderState.NOT_HELD)
        with Roster(self.directory).hold("owner"):
            manager = read(self.directory)
        self.assertEqual(manager.state, HolderState.HOLD_ONLY)
        self.assertIsInstance(manager.record, Unread)
        self.assertEqual(manager.entries, self.roster.entries())

    def test_a_held_manager_with_a_corrupt_roster_keeps_its_holder_state(self):
        self.write_record(LIVE)
        with Roster(self.directory).hold("owner"):
            valid = Path(self.roster.path).read_text()
            Path(self.roster.path).write_text("{")
            manager = read(self.directory)
            Path(self.roster.path).write_text(valid)
        self.assertEqual(manager.state, HolderState.CLIENT_RUNNING)
        self.assertIsInstance(manager.entries, Unread)
        self.assertEqual(manager.holder, manager.entries)

    def test_a_failed_probe_is_unreadable(self):
        for name in ("holder.lock", "roster.lock"):
            with self.subTest(name=name):
                path = Path(self.directory, name)
                path.unlink(missing_ok=True)
                path.mkdir()
                manager = read(self.directory)
                self.assertIsInstance(manager, Unreadable)
                self.assertEqual(manager.name, "manager")
                self.assertIn("not a regular", manager.error)
                path.rmdir()
                path.touch(mode=0o600)

    def test_a_held_manager_is_read_without_changing_it(self):
        self.write_record(LIVE)
        with Roster(self.directory).hold("owner") as lease:
            before = {p: Path(self.directory, p).read_bytes() for p in os.listdir(self.directory)}
            self.assertEqual(read(self.directory).holder, ActiveHolder(lease.id, "owner"))
            after = {p: Path(self.directory, p).read_bytes() for p in os.listdir(self.directory)}
            self.assertEqual(after, before)
            other = Roster(self.directory)
            other.bind(lease)
            other.check_lease()

    def test_a_holder_comes_exactly_with_a_held_state(self):
        with self.assertRaises(ValueError):
            Manager("m", HolderState.CLIENT_RUNNING, None, None, {})
        with self.assertRaises(ValueError):
            Manager("m", HolderState.NOT_HELD, ActiveHolder("id", "owner"), None, {})


class ManagersTest(RosterCase):
    def test_an_absent_root_has_no_managers(self):
        self.assertEqual(managers(os.path.join(self.home, "absent")), [])

    def test_managers_are_named_directories_sorted_by_name(self):
        root = os.path.join(self.ensemble, "managers")
        for name in ("b", "a", "bad name", ".hidden"):
            os.makedirs(os.path.join(root, name))
        Path(root, "file").touch()
        os.symlink(os.path.join(root, "a"), os.path.join(root, "link"))
        self.assertEqual([m.name for m in managers(root)], ["a", "b"])


RECORD = SessionRecord("kind-a", "s-1", LIVE, "/home/user/project")
OWN, OTHER, ANONYMOUS = (
    ActiveHolder("id-1", "demo"),
    ActiveHolder("id-2", "work"),
    ActiveHolder("id-3", None),
)
RUNNING, HOLD_ONLY, NOT_HELD = (
    HolderState.CLIENT_RUNNING,
    HolderState.HOLD_ONLY,
    HolderState.NOT_HELD,
)


def _manager(state, holder=None, record=None, entries=None):
    return Manager("demo", state, holder, record, {} if entries is None else entries)


class PlanTest(RosterCase):
    def test_an_unreadable_manager_or_roster_is_refused_with_its_error(self):
        self.assertEqual(plan(Unreadable("demo", "lock: busy"), live=False), Refused("lock: busy"))
        broken = Unread("roster.json: not JSON")
        for state, holder in ((NOT_HELD, None), (RUNNING, broken), (HOLD_ONLY, broken)):
            with self.subTest(state=state):
                manager = _manager(state, holder, RECORD, broken)
                self.assertEqual(plan(manager, live=False), Refused("roster.json: not JSON"))

    def test_a_held_lock_without_a_recorded_holder_is_refused(self):
        manager = _manager(HOLD_ONLY, Unread("holder not recorded"), RECORD)
        self.assertEqual(plan(manager, live=False), Refused("holder not recorded"))

    def test_a_holder_outside_the_managers_own_session_is_refused_by_label(self):
        for state in (RUNNING, HOLD_ONLY):
            for holder, label in ((OTHER, "work"), (ANONYMOUS, "id-3")):
                with self.subTest(state=state, holder=holder):
                    self.assertEqual(
                        plan(_manager(state, holder, RECORD), live=True),
                        Refused(f"held by {label}"),
                    )

    def test_a_running_client_under_its_own_hold_is_attached(self):
        self.assertEqual(plan(_manager(RUNNING, OWN, RECORD), live=True), Plan(Verb.ATTACH, "demo"))

    def test_hold_only_resumes_its_record_under_the_hold_else_attaches(self):
        self.assertEqual(
            plan(_manager(HOLD_ONLY, OWN, RECORD), live=False), Plan(Verb.RESUME_HELD, "demo")
        )
        for record in (None, Unread("session.json: not JSON")):
            with self.subTest(record=record):
                self.assertEqual(
                    plan(_manager(HOLD_ONLY, OWN, record), live=False), Plan(Verb.ATTACH, "demo")
                )

    def test_not_held_resumes_its_record_under_a_new_hold_else_opens(self):
        self.assertEqual(
            plan(_manager(NOT_HELD, None, RECORD), live=False), Plan(Verb.RESUME_NEW, "demo")
        )
        for record in (None, Unread("session.json: not JSON")):
            with self.subTest(record=record):
                self.assertEqual(
                    plan(_manager(NOT_HELD, None, record), live=False), Plan(Verb.OPEN, "demo")
                )

    def test_a_live_client_without_a_holder_is_refused(self):
        self.assertEqual(
            plan(_manager(NOT_HELD, None, RECORD), live=True),
            Refused(f"its client {LIVE} runs without a holder; exit it first"),
        )


class ResumeTargetTest(RosterCase):
    def setUp(self):
        super().setUp()
        self.live()
        self.cwd = os.path.join(self.home, "pro'ject \x15 $x")
        os.mkdir(self.cwd)
        self.enterContext(mock.patch("os.kill", side_effect=_kill))
        self.run = self.enterContext(mock.patch("subprocess.run"))
        self.answer('["kind-a", "--resume", "s-1"]')

    def answer(self, stdout, returncode=0, stderr=""):
        self.run.return_value = CompletedProcess((), returncode, stdout, stderr)

    def write(self, **fields):
        record = {"client": "kind-a", "sid": "s-1", "pid": DEAD, "cwd": self.cwd, **fields}
        Path(self.directory, "session.json").write_text(json.dumps(record))

    def refused(self, pattern):
        with self.assertRaisesRegex(RosterError, pattern):
            resume_target(self.directory)

    def test_the_recorded_client_resumes_in_its_cwd(self):
        self.write()
        self.assertEqual(
            resume_target(self.directory),
            (SessionRecord("kind-a", "s-1", DEAD, self.cwd), ("kind-a", "--resume", "s-1")),
        )
        self.assertEqual(
            self.run.call_args.args[0], ("ensemble-session", "resume-argv", "kind-a", "--", "s-1")
        )
        self.assertEqual(self.run.call_args.kwargs["stdin"], DEVNULL)

    def test_no_or_an_unreadable_record_is_refused(self):
        self.refused("no session record")
        Path(self.directory, "session.json").write_text("{")
        self.refused("session.json: not JSON")
        self.run.assert_not_called()

    def test_a_live_recorded_client_is_refused(self):
        self.write(pid=LIVE)
        self.refused(f"its client {LIVE} still runs")
        self.run.assert_not_called()

    def test_a_cwd_that_is_no_directory_is_refused_printably(self):
        self.write(cwd="/absent/\x1b[2Jdir")
        self.refused(r"recorded cwd '/absent/\\x1b\[2Jdir' is not a directory")
        self.run.assert_not_called()

    def test_a_failed_resume_argv_is_refused_with_its_reason(self):
        self.write()
        for answer, pattern in (
            (("", 2, "ensemble-session: config: no client kind-a\n"), "no client kind-a"),
            (("", 3, ""), "exit 3"),
            (("not json", 0, ""), "want a JSON array"),
            (('{"a": 1}', 0, ""), "want a JSON array"),
            (('["kind-a", ""]', 0, ""), "want a JSON array"),
            (("[]", 0, ""), "want a JSON array"),
        ):
            with self.subTest(answer=answer):
                self.answer(*answer)
                self.refused(f"cannot resume its kind-a session: .*{pattern}")
        for error in (TimeoutExpired((), 30), OSError("no ensemble-session")):
            with self.subTest(error=error):
                self.run.side_effect = error
                self.refused("cannot resume its kind-a session")


class WriteRecordTest(RosterCase):
    def test_replaces_the_record_with_one_read_record_reads_back(self):
        with open(session_records.SCHEMA, encoding="utf-8") as f:
            validator = jsonschema.Draft202012Validator(json.load(f))
        os.makedirs(self.directory)
        path = Path(self.directory, "session.json")
        path.write_text("old\n", encoding="utf-8")
        for client, fields in session_records.VALID.items():
            with self.subTest(client):
                record = SessionRecord(**fields)
                write_record(self.directory, record)
                self.assertEqual(read_record(str(path)), record)
                validator.validate(json.loads(path.read_text(encoding="utf-8")))
                self.assertEqual(os.listdir(self.directory), ["session.json"])
