"""DelegateContract, FR-47's Delegate contract (FR-13 to FR-16): a subclass also inherits Case and
defines delegate_cls, ROLE (a role it runs), SHOWABLE (its sessions can display), make(workdir,
**kw) (a Delegate of kw's role, else ROLE, and a Spec for workdir with kw's other fields),
end(record) (its sessions gone, no outcome) and finish(record) (its sessions gone, a valid outcome).
Each case uses only the Delegate ABC and these hooks.
"""

import os
import shutil
import uuid

from ensemble.delegate import ConfigError, Delegate
from ensemble.delegate.engine.types import Begun, Finished
from ensemble.session import NotShowable, Usage
from ensemble.utils import Record, Status

NO_ROLE = "no-such-role"
OUTER = {
    "kind": "pipeline",
    "name": "TASK-1",
    "ctl": ("ensemble-orchestrator",),
    "resume": ("ensemble-orchestrator", "resume", "TASK-1"),
}


def _files(top: str, *, but: str) -> dict[str, bytes]:
    found = {}
    for root, dirs, names in os.walk(top):
        dirs[:] = [d for d in dirs if not os.path.join(root, d).startswith(but)]
        for name in names:
            with open(os.path.join(root, name), "rb") as file:
                found[os.path.relpath(os.path.join(root, name), top)] = file.read()
    return found


class DelegateContract:
    delegate_cls: type[Delegate]
    ROLE: str
    SHOWABLE: bool

    def make(self, workdir: str, **kw) -> Delegate:
        raise NotImplementedError

    def end(self, record: Record) -> None:
        raise NotImplementedError

    def finish(self, record: Record) -> None:
        raise NotImplementedError

    def workdir(self) -> str:
        path = os.path.join(self.home, f"run-{uuid.uuid4().hex[:8]}")
        os.mkdir(path)
        return path

    def detached(self, outer=None) -> tuple[Record, str]:
        workdir = self.workdir()
        record = self.make(workdir).detach(outer=outer)
        self.addCleanup(self.delegate_cls.attach(workdir).close)
        return record, workdir

    def test_an_unknown_role_is_refused_with_config_error_writing_nothing(self):
        workdir = self.workdir()
        with self.assertRaises(ConfigError):
            self.make(workdir, role=NO_ROLE)
        self.assertEqual(os.listdir(workdir), [])

    def test_detach_returns_a_record_line_with_the_default_outer_fields(self):
        record, workdir = self.detached()
        self.assertIsInstance(record, Record)
        self.assertEqual(Record.parse(record.to_line()), record)
        self.assertEqual(
            (record.kind, record.ctl, record.detail), ("role", ("ensemble-delegate",), self.ROLE)
        )
        self.assertEqual(record.resume[:2], ("ensemble-delegate", "resume"))
        self.assertEqual(len(record.resume), 3)
        for path in (record.workdir, record.resume[2]):
            self.assertEqual(os.path.realpath(path), os.path.realpath(workdir))
        self.assertTrue(record.sessions)

    def test_detach_takes_the_outer_starters_kind_name_ctl_and_resume(self):
        record, _ = self.detached(OUTER)
        self.assertEqual(
            (record.kind, record.name, record.ctl, record.resume), tuple(OUTER.values())
        )

    def test_attach_by_record_or_workdir_views_the_live_run(self):
        record, workdir = self.detached()
        for key in (record, workdir):
            with self.subTest(key=type(key).__name__):
                view = self.delegate_cls.attach(key)
                self.assertTrue(view.alive())
                status = view.status()
                self.assertIsInstance(status, Status)
                self.assertEqual((status.name, status.alive), (record.name, True))

    def test_attach_with_another_sessions_sid_raises_config_error(self):
        record, workdir = self.detached()
        self.assertTrue(self.delegate_cls.attach(workdir, sid=record.sid).alive())
        with self.assertRaises(ConfigError):
            self.delegate_cls.attach(workdir, sid="00000000-0000-4000-8000-0000000000ff")

    def test_attach_of_a_dir_without_a_run_raises_config_error(self):
        with self.assertRaises(ConfigError):
            self.delegate_cls.attach(self.workdir())

    def test_close_twice_is_fine_and_leaves_it_not_alive(self):
        record, _ = self.detached()
        view = self.delegate_cls.attach(record)
        view.close()
        view.close()
        self.assertFalse(view.alive())
        self.assertFalse(self.delegate_cls.attach(record).alive())

    def test_checkouts_and_clean_reach_only_the_agent_files(self):
        workdir = self.workdir()
        foreign = os.path.join(workdir, "writeback.json")
        with open(foreign, "w", encoding="utf-8") as file:
            file.write("ledger")
        self.assertIsNone(self.delegate_cls.checkouts(workdir))
        self.make(workdir)
        self.assertEqual(self.delegate_cls.checkouts(workdir), ())
        view = self.delegate_cls.attach(workdir)
        checkout = os.path.join(view.status().cwd, "src", "o", "n-TASK-1")
        os.makedirs(checkout)
        self.assertEqual(self.delegate_cls.checkouts(workdir), (os.path.realpath(checkout),))
        name, records = view.status().name, _files(workdir, but=view.status().cwd)
        self.delegate_cls.clean(workdir, dry=True)
        self.assertEqual(self.delegate_cls.checkouts(workdir), (os.path.realpath(checkout),))
        self.delegate_cls.clean(workdir)
        self.delegate_cls.clean(workdir)
        self.assertIsNone(self.delegate_cls.checkouts(workdir))
        self.assertEqual(_files(workdir, but=checkout), records)
        self.assertEqual(self.delegate_cls.attach(workdir).status().name, name)
        with open(foreign, encoding="utf-8") as file:
            self.assertEqual(file.read(), "ledger")

    def test_clean_refuses_agent_files_reached_through_a_symlink(self):
        workdir = self.workdir()
        self.make(workdir)
        cwd = self.delegate_cls.attach(workdir).status().cwd
        elsewhere = os.path.join(self.home, f"elsewhere-{uuid.uuid4().hex[:8]}")
        os.makedirs(os.path.join(elsewhere, "src", "o", "n-TASK-1"))
        os.replace(cwd, elsewhere + "-aside")
        os.symlink(elsewhere, cwd)
        self.assertEqual(self.delegate_cls.checkouts(workdir), ())
        for dry in (True, False):
            with self.subTest(dry=dry), self.assertRaises(ConfigError):
                self.delegate_cls.clean(workdir, dry=dry)
        self.assertTrue(os.path.isdir(os.path.join(elsewhere, "src", "o", "n-TASK-1")))

    def test_clean_refuses_a_live_run_deleting_nothing(self):
        _record, workdir = self.detached()
        for dry in (False, True):
            with self.subTest(dry=dry), self.assertRaises(ConfigError):
                self.delegate_cls.clean(workdir, dry=dry)
        self.assertEqual(self.delegate_cls.checkouts(workdir), ())

    def test_clean_without_agent_files_checks_the_run(self):
        record, workdir = self.detached()
        shutil.rmtree(self.delegate_cls.attach(workdir).status().cwd)
        for dry in (False, True):
            with self.subTest(dry=dry), self.assertRaises(ConfigError):
                self.delegate_cls.clean(workdir, dry=dry)
        self.end(record)
        files = _files(workdir, but="\0")
        self.delegate_cls.clean(workdir)
        self.assertIsNone(self.delegate_cls.checkouts(workdir))
        self.assertEqual(_files(workdir, but="\0"), files)

    def test_resume_after_its_sessions_ended_starts_them_once_with_the_same_sid(self):
        record, _ = self.detached()
        self.end(record)
        view = self.delegate_cls.attach(record)
        self.assertFalse(view.alive())
        first = view.resume()
        self.assertEqual(view.resume(), first)
        self.assertEqual((first.sid, first.sessions), (record.sid, record.sessions))
        self.assertTrue(view.alive())

    def test_resume_in_a_given_mode_starts_them_once_with_the_same_sid(self):
        record, _ = self.detached()
        self.end(record)
        view = self.delegate_cls.attach(record)
        resumed = view.resume(mode="headless")
        self.assertEqual(resumed.sid, record.sid)
        self.assertEqual(view.resume(mode="headless"), resumed)
        self.assertTrue(view.alive())

    def test_resume_after_a_valid_outcome_starts_nothing_and_returns_the_record(self):
        record, _ = self.detached()
        self.finish(record)
        view = self.delegate_cls.attach(record)
        self.assertIsNotNone(view.outcome())
        self.assertEqual(view.resume(), record)
        self.assertFalse(view.alive())

    def test_show_and_attach_command_reach_only_showable_sessions(self):
        record, _ = self.detached()
        view = self.delegate_cls.attach(record)
        if self.SHOWABLE:
            self.assertIsInstance(view.attach_command(), str)
            return
        self.assertIsNone(view.attach_command())
        self.assertIsNone(view.status().attach)
        with self.assertRaises(NotShowable):
            view.show()

    def test_run_reports_begun_first_and_finished_last_and_returns_its_outcome(self):
        seen = []
        result = self.make(self.workdir()).run(seen.append)
        self.assertEqual(result.returncode, 0)
        self.assertIsNotNone(result.outcome)
        self.assertIsInstance(seen[0], Begun)
        self.assertIsInstance(seen[-1], Finished)
        self.assertEqual(seen[-1].result, result)

    def test_class_methods_answer_for_its_roles_and_refuse_an_unknown_one(self):
        cls = self.delegate_cls
        self.assertIn(self.ROLE, cls.roles())
        self.assertEqual(cls.resolve(self.ROLE).role, self.ROLE)
        self.assertIsInstance(cls.trusted_dirs(), frozenset)
        usage = cls.usage(self.ROLE)
        self.assertTrue(usage is None or isinstance(usage, Usage), usage)
        for method in (cls.resolve, cls.usage):
            with self.subTest(method=method.__name__), self.assertRaises(ConfigError):
                method(NO_ROLE)
