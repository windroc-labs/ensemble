"""Recorded quota limits gate recovery using the saved Delegate kind and client."""

import os
from datetime import UTC, datetime, timedelta

from ensemble.orchestrator.engine import runs
from ensemble.orchestrator.engine.gate import Gate
from tests.orchestrator.case import Case
from tests.orchestrator.gate_fixtures import quota_config, quota_provider

NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)


class RecordedQuotaKeys(Case):
    def test_saved_kind_and_client_limit_blocks_drifted_resume_without_probing(self):
        provider = quota_provider("current-client", 10)
        cfg = quota_config(provider, "current-kind")
        path = os.path.join(self.home, "runs.jsonl")
        runs.note_limit(
            path,
            issue="TASK-1",
            sid="saved-sid",
            client="saved-client",
            delegate_kind="saved-kind",
            resets_at=(NOW + timedelta(hours=1)).isoformat(),
            attempt=1,
        )
        events = []
        gate = Gate(
            path,
            NOW,
            lambda event, *args, **fields: events.append((event, args, fields)),
            cfg=cfg,
            dry=False,
        )
        self.assertTrue(gate.blocked("engineer", "TASK-1", key=("saved-kind", "saved-client")))
        self.assertEqual((provider.queried, gate.seen), ([], {}))
        self.assertEqual(
            events,
            [
                (
                    "usage-skip",
                    ("TASK-1",),
                    {
                        "once": True,
                        "client": "saved-client",
                        "reason": "limit",
                        "until": (NOW + timedelta(hours=1)).isoformat(),
                    },
                )
            ],
        )
        self.assertFalse(gate.blocked("engineer", "TASK-2", key=("current-kind", "current-client")))
        self.assertEqual(provider.queried, ["engineer"])

    def test_limit_in_one_kind_does_not_block_another_kind_with_the_same_client(self):
        provider = quota_provider("shared-client", 10)
        cfg = quota_config(provider, "current-kind")
        path = os.path.join(self.home, "runs.jsonl")
        runs.note_limit(
            path,
            issue="TASK-1",
            sid="saved-sid",
            client="shared-client",
            delegate_kind="other-kind",
            resets_at=(NOW + timedelta(hours=1)).isoformat(),
            attempt=1,
        )
        gate = Gate(path, NOW, cfg=cfg, dry=False)
        self.assertFalse(gate.blocked("engineer", "TASK-2", key=("current-kind", "shared-client")))
        self.assertEqual(provider.queried, ["engineer"])

    def test_expired_saved_limit_probes_and_caches_the_recorded_client(self):
        provider = quota_provider("current-client", 95)
        saved = quota_provider("saved-client", 95)
        cfg = quota_config(provider, "current-kind")
        cfg.delegates["saved-kind"] = saved
        path = os.path.join(self.home, "runs.jsonl")
        runs.note_limit(
            path,
            issue="TASK-1",
            sid="saved-sid",
            client="saved-client",
            delegate_kind="saved-kind",
            resets_at=(NOW - timedelta(seconds=1)).isoformat(),
            attempt=1,
        )
        gate = Gate(path, NOW, cfg=cfg, dry=False)
        self.assertTrue(gate.blocked("engineer", "TASK-1", key=("saved-kind", "saved-client")))
        self.assertTrue(gate.blocked("engineer", "TASK-2", key=("saved-kind", "saved-client")))
        self.assertEqual((provider.queried, saved.queried), ([], ["engineer"]))
        self.assertIn(("saved-kind", "saved-client"), gate.seen)
