"""Tracker.archive: the archive rule once, over an adapter's archive_plan and _write_archive; the
port's snapshots and changed defaults."""

from datetime import UTC, datetime

from ensemble.orchestrator.engine.flow.tracker import Tracker
from ensemble.orchestrator.engine.flow.types import (
    ArchivePlan,
    ArchiveRefused,
    Changes,
    IssueState,
    Snapshot,
    SubIssue,
)
from orchestrator_tests.case import Case

DONE = SubIssue("k-2", "TASK-2", "Done", finished=True)
OPEN = SubIssue("k-3", "TASK-3", "Todo", finished=False)


def plan(*, archived=False, unfinished=()):
    return ArchivePlan("k-1", "TASK-1", "u", archived, (DONE, OPEN), tuple(unfinished))


def _unused(*_, **__):
    raise AssertionError("not part of the archive rule")


class Adapter(Tracker):
    """Only the archive hooks; every other abstract method fails if called."""

    locals().update(dict.fromkeys(Tracker.__abstractmethods__, _unused))

    def __init__(self, found, *, archives=True):
        self.found, self.reads, self.writes, self.archives = found, [], [], archives

    def archive_plan(self, key):
        self.reads.append(key)
        return self.found

    def _write_archive(self, key):
        self.writes.append(key)
        return self.archives


class Outcome(Case):
    def test_archived_then_unfinished_then_changed(self):
        cases = (
            (plan(archived=True), "unchanged"),
            (plan(unfinished=(OPEN,)), "refused"),
            (plan(), "changed"),
        )
        for found, outcome in cases:
            with self.subTest(outcome=outcome):
                self.assertEqual(found.outcome, outcome)


class Archive(Case):
    def test_nothing_unfinished_writes_once(self):
        adapter = Adapter(plan())
        self.assertTrue(adapter.archive("k-1"))
        self.assertEqual((adapter.reads, adapter.writes), (["k-1"], ["k-1"]))

    def test_archived_meanwhile_reports_its_write(self):
        adapter = Adapter(plan(), archives=False)
        self.assertFalse(adapter.archive("k-1"))
        self.assertEqual(adapter.writes, ["k-1"])

    def test_already_archived_writes_nothing(self):
        adapter = Adapter(plan(archived=True))
        self.assertFalse(adapter.archive("k-1"))
        self.assertEqual(adapter.writes, [])

    def test_anything_unfinished_refuses_with_the_plan(self):
        found = plan(unfinished=(OPEN,))
        adapter = Adapter(found)
        with self.assertRaises(ArchiveRefused) as raised:
            adapter.archive("k-1")
        self.assertIs(raised.exception.plan, found)
        self.assertEqual(adapter.writes, [])

    def test_a_given_plan_is_not_read_again(self):
        adapter = Adapter(plan(unfinished=(OPEN,)))
        self.assertTrue(adapter.archive("k-1", plan()))
        self.assertEqual((adapter.reads, adapter.writes), ([], ["k-1"]))


class Reader(Adapter):
    def __init__(self, found):
        super().__init__(None)
        self.snaps = found

    def read(self, ident):
        self.reads.append(ident)
        return self.snaps.get(ident)


class Defaults(Case):
    def test_snapshots_read_each_issue(self):
        found = Snapshot(IssueState.DONE, None)
        adapter = Reader({"TASK-1": found})
        self.assertEqual(adapter.snapshots(["TASK-1", "TASK-2"]), {"TASK-1": found, "TASK-2": None})
        self.assertEqual(adapter.reads, ["TASK-1", "TASK-2"])

    def test_no_change_feed(self):
        self.assertEqual(
            Reader({}).changed(datetime(2026, 1, 1, tzinfo=UTC), 3), Changes(None, None)
        )
