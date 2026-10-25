"""prune.json: prune's cache of Linear between ticks."""

import json
import os
import shutil
import tempfile
from datetime import UTC, datetime, timedelta

from ensemble.orchestrator.engine import prune_state
from ensemble.orchestrator.engine.flow.types import IssueState, Snapshot
from ensemble.orchestrator.engine.types import Entry, PruneState, Refusal
from orchestrator_tests.case import Case

NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)


class Store(Case):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.path = os.path.join(self.dir, "logs", "prune.json")

    def write(self, text):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            f.write(text)

    def test_round_trip(self):
        state = PruneState(
            synced=NOW,
            issues={
                "TASK-1": Entry(
                    Snapshot(IssueState.DONE, NOW - timedelta(hours=3), NOW - timedelta(hours=1)),
                    NOW,
                ),
                "TASK-2": Entry(Snapshot(None, None), NOW),
                "TASK-3": Entry(None, NOW - timedelta(hours=1)),
            },
            refused={
                "TASK-4": Refusal(
                    {"TASK-4": NOW - timedelta(hours=2), "TASK-5": None},
                    "archive: sub-issue TASK-5",
                    NOW,
                )
            },
            unbatched={"OPS-7"},
        )
        prune_state.save(self.path, state)
        self.assertEqual(prune_state.load(self.path), (state, None))
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["prune.json"])

    def test_no_file_is_empty(self):
        self.assertEqual(prune_state.load(self.path), (PruneState(), None))

    def test_unreadable_is_empty_with_why(self):
        good = {"synced": None, "issues": {}, "refused": {}, "unbatched": []}
        entry = {
            "state": "done",
            "since": None,
            "updated": None,
            "gone": False,
            "read": NOW.isoformat(),
        }
        no_updated = {k: v for k, v in entry.items() if k != "updated"}
        refusal = {"versions": {"TASK-1": None}, "reason": "x", "read": NOW.isoformat()}
        old = {"unfinished": [], "reason": "x", "read": NOW.isoformat()}
        for name, text in (
            ("not json", "{"),
            ("not an object", "[]"),
            ("a key missing", json.dumps({"synced": None})),
            ("a key more", json.dumps(good | {"x": 1})),
            ("bad time", json.dumps(good | {"synced": "yesterday"})),
            ("naive time", json.dumps(good | {"synced": "2026-10-20T12:00:00"})),
            ("unknown state", json.dumps(good | {"issues": {"TASK-1": entry | {"state": "x"}}})),
            ("entry not an object", json.dumps(good | {"issues": {"TASK-1": 1}})),
            ("gone not a bool", json.dumps(good | {"issues": {"TASK-1": entry | {"gone": 1}}})),
            ("unbatched not a list", json.dumps(good | {"unbatched": "TASK-1"})),
            ("entry without updated", json.dumps(good | {"issues": {"TASK-1": no_updated}})),
            (
                "reason not text",
                json.dumps(good | {"refused": {"TASK-1": refusal | {"reason": 1}}}),
            ),
            ("old unfinished list", json.dumps(good | {"refused": {"TASK-1": old}})),
            (
                "version not a time",
                json.dumps(good | {"refused": {"TASK-1": refusal | {"versions": {"TASK-1": 1}}}}),
            ),
        ):
            with self.subTest(name):
                self.write(text)
                state, error = prune_state.load(self.path)
                self.assertEqual(state, PruneState())
                self.assertIsInstance(error, str)
                self.assertTrue(error)

    def test_a_directory_is_unreadable(self):
        os.makedirs(self.path)
        state, error = prune_state.load(self.path)
        self.assertEqual(state, PruneState())
        self.assertIsNotNone(error)
