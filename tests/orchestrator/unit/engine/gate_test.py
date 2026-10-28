"""Actual-client quota keys and tick-local probes at the Delegate boundary."""

from ensemble.orchestrator.engine.gate import Gate
from tests.orchestrator.case import Case
from tests.orchestrator.gate_fixtures import quota_config, quota_provider

SHARED = ("first", "shared-client")


class DelegateQuotaKeys(Case):
    def test_role_tiers_do_not_change_the_client_probe_cache(self):
        provider = quota_provider("shared-client", 10)
        core = {"roles": {"engineer": {"tier": 1}, "pm": {"tier": 4}}}
        gate = self.gate(provider, core=core)
        for role in ("engineer", "pm", "engineer"):
            self.assertFalse(gate.blocked(role, "TASK-1", key=SHARED))
        self.assertEqual(provider.queried, ["engineer"])
        self.assertEqual(
            provider.overlays,
            [(core, {"roles": {"engineer": {"client": "shared-client"}}})],
        )

    def gate(self, provider, *, kind="first", say=None, client=None, core=None):
        cfg = quota_config(
            provider, kind, core=core, clients=None if client is None else {"engineer": client}
        )
        return Gate(say, cfg=cfg, dry=False)

    def test_the_configured_overlay_client_keys_limits_and_reaches_the_probe(self):
        core = {"roles": {"engineer": {"client": "overlay-client"}}}
        provider = quota_provider("catalog-client", 10)
        self.assertFalse(
            self.gate(provider, client="overlay-client", core=core).blocked(
                "engineer", "TASK-1", key=("first", "overlay-client")
            )
        )
        self.assertEqual(
            (provider.queried, provider.overlays),
            (["engineer"], [(core, {"roles": {"engineer": {"client": "overlay-client"}}})]),
        )

    def test_roles_sharing_a_client_share_one_probe(self):
        provider = quota_provider("shared-client", 95)
        gate = self.gate(provider)
        self.assertTrue(gate.blocked("engineer", "TASK-1", key=SHARED))
        self.assertTrue(gate.blocked("pm", "TASK-2", key=SHARED))
        self.assertTrue(gate.blocked("engineer", "TASK-3", key=SHARED))
        self.assertEqual(provider.queried, ["engineer"])

    def test_allowed_usage_is_not_cached_across_ticks(self):
        provider = quota_provider("shared-client", 10)
        for _ in range(2):
            gate = self.gate(provider)
            self.assertFalse(gate.blocked("engineer", "TASK-1", key=SHARED))
            self.assertFalse(gate.blocked("pm", "TASK-2", key=SHARED))
        self.assertEqual(provider.queried, ["engineer", "engineer"])

    def test_distinct_kinds_sharing_a_client_have_separate_probe_contexts(self):
        exhausted = quota_provider("shared-client", 95)
        available = quota_provider("shared-client", 10)
        gate = self.gate(exhausted)
        self.assertTrue(gate.blocked("engineer", "TASK-1", key=SHARED))
        gate.cfg.delegate_kind = "second"
        gate.cfg.delegate_cls = available
        gate.cfg.delegates["second"] = available
        self.assertFalse(gate.blocked("engineer", "TASK-2", key=("second", "shared-client")))
        self.assertEqual((exhausted.queried, available.queried), (["engineer"], ["engineer"]))

    def test_recorded_client_is_probed_through_its_recorded_provider(self):
        provider = quota_provider("current-client", 95)
        saved = quota_provider("saved-client", 95)
        events = []
        gate = self.gate(
            provider, say=lambda event, *args, **fields: events.append((event, args, fields))
        )
        gate.cfg.delegates["saved-kind"] = saved
        self.assertTrue(gate.blocked("engineer", "TASK-1", key=("saved-kind", "saved-client")))
        self.assertEqual((provider.queried, saved.queried), ([], ["engineer"]))
        self.assertEqual(
            saved.overlays, [({}, {"roles": {"engineer": {"client": "saved-client"}}})]
        )
        self.assertEqual(events[0][0], "usage-skip")
        self.assertEqual(events[0][2]["client"], "saved-client")
        self.assertTrue(gate.blocked("engineer", "TASK-2", key=("first", "current-client")))
        self.assertEqual(provider.queried, ["engineer"])

    def test_missing_recorded_provider_blocks_without_probing_default(self):
        provider = quota_provider("current-client", 10)
        events = []
        gate = self.gate(
            provider, say=lambda event, *args, **fields: events.append((event, args, fields))
        )
        self.assertTrue(gate.blocked("engineer", "TASK-1", key=("missing", "saved-client")))
        self.assertEqual(provider.queried, [])
        skip = {"once": True, "client": "saved-client", "reason": "usage unknown"}
        self.assertEqual(events, [("usage-skip", ("TASK-1",), skip)])
        self.assertFalse(gate.blocked("engineer", "TASK-2", key=("first", "current-client")))
        self.assertEqual(provider.queried, ["engineer"])
