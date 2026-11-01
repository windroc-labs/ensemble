"""Actual-client quota keys, tick-local probes and per-account headroom decisions at the Delegate
boundary.
"""

import math

from ensemble.orchestrator.engine.gate import Gate
from tests.orchestrator.case import Case
from tests.orchestrator.gate_fixtures import quota_config, quota_provider

SHARED = ("first", "shared-client")


class DelegateQuotaKeys(Case):
    def test_role_tiers_do_not_change_the_client_probe_cache(self):
        provider = quota_provider("shared-client", 20)
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
        provider = quota_provider("catalog-client", 20)
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
        provider = quota_provider("shared-client", -5)
        gate = self.gate(provider)
        self.assertTrue(gate.blocked("engineer", "TASK-1", key=SHARED))
        self.assertTrue(gate.blocked("pm", "TASK-2", key=SHARED))
        self.assertTrue(gate.blocked("engineer", "TASK-3", key=SHARED))
        self.assertEqual(provider.queried, ["engineer"])

    def test_allowed_usage_is_not_cached_across_ticks(self):
        provider = quota_provider("shared-client", 20)
        for _ in range(2):
            gate = self.gate(provider)
            self.assertFalse(gate.blocked("engineer", "TASK-1", key=SHARED))
            self.assertFalse(gate.blocked("pm", "TASK-2", key=SHARED))
        self.assertEqual(provider.queried, ["engineer", "engineer"])

    def test_distinct_kinds_sharing_a_client_have_separate_probe_contexts(self):
        exhausted = quota_provider("shared-client", -5)
        available = quota_provider("shared-client", 20)
        gate = self.gate(exhausted)
        self.assertTrue(gate.blocked("engineer", "TASK-1", key=SHARED))
        gate.cfg.delegate_kind = "second"
        gate.cfg.delegate_cls = available
        gate.cfg.delegates["second"] = available
        self.assertFalse(gate.blocked("engineer", "TASK-2", key=("second", "shared-client")))
        self.assertEqual((exhausted.queried, available.queried), (["engineer"], ["engineer"]))

    def test_recorded_client_is_probed_through_its_recorded_provider(self):
        provider = quota_provider("current-client", -5)
        saved = quota_provider("saved-client", -5)
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
        provider = quota_provider("current-client", 20)
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


class HeadroomDecision(Case):
    def decide(self, *lefts, limit=None):
        provider = quota_provider("claude", *lefts)
        cfg = quota_config(provider, "first")
        if limit is not None:
            cfg.usage_limit = {"claude": limit}
        events = []
        gate = Gate(lambda event, *_, **f: events.append((event, f)), cfg=cfg, dry=False)
        return gate.blocked("engineer", "TASK-1", key=("first", "claude")), events, provider

    def test_any_open_account_allows(self):
        for lefts in ((0, 5), (math.inf,), (None, -5, 0.5), (math.inf, None)):
            with self.subTest(lefts=lefts):
                blocked, events, _ = self.decide(*lefts)
                self.assertFalse(blocked)
                self.assertEqual(events, [])

    def test_a_panel_less_account_allows(self):
        self.assertFalse(self.decide(math.inf)[0])

    def test_no_open_account_blocks(self):
        for lefts, reason, shown in (
            ((None, 0, -5), "usage", "a0=unknown a1=0 a2=-5"),
            ((0,), "usage", "a0=0"),
            ((None,), "usage unknown", None),
            ((), "usage unknown", None),
        ):
            with self.subTest(lefts=lefts):
                blocked, events, _ = self.decide(*lefts)
                self.assertTrue(blocked)
                fields = {"once": True, "client": "claude", "reason": reason}
                if shown is not None:
                    fields["usage"] = shown
                self.assertEqual(events, [("usage-skip", fields)])

    def test_off_reads_no_usage(self):
        blocked, events, provider = self.decide(-5, limit=False)
        self.assertFalse(blocked)
        self.assertEqual((events, provider.queried), ([], []))
