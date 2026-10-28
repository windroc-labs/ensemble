"""src/ensemble/delegate/engine/environment.py: a run's environment from the config layers
(WDRC-109 FR-57, FR-59, FR-60) and its prefetch target from the input (FR-52).
"""

import copy
import json

from ensemble.delegate.engine.environment import prefetch_target, resolve, resolve_fields
from ensemble.delegate.engine.types import ConfigError, Resolved
from ensemble.session import EnvironmentSpec
from tests.delegate.case import Case

PACKAGED = {
    "docker": {
        "image": "@worker",
        "gateway": "workers",
        "plugins": [],
        "prefetch": False,
    }
}
SECRET = {"item": "ensemble-broker-env/dummy", "file": "dummy.txt"}


def config(**layers) -> dict:
    cfg = {"environment": copy.deepcopy(PACKAGED), "environments": {}, "roles": {}}
    for key, value in layers.items():
        cfg[key] = cfg[key] | value
    return cfg


def resolved(cfg: dict, role: str = "dummy-tester", host: str = "docker") -> Resolved | None:
    return resolve(cfg, role=role, host=host, packaged=PACKAGED)


class ResolveTest(Case):
    def test_credentials_remain_a_separate_opaque_value_for_default_equivalent_environment(self):
        value = {"future-kind": [1, {"anything": "unchanged"}]}
        cfg = config(environments={"dummy": {"credentials": value}})
        found = resolve_fields(cfg, host="docker", name="dummy", overrides={}, packaged=PACKAGED)
        assert found is not None
        self.assertEqual(json.loads(found.credentials), value)
        self.assertNotIn("credentials", found.spec.persisted())

    def test_a_role_without_an_environment_table_passes_none(self):
        self.assertIsNone(resolved(config(), "engineer"))
        self.assertIsNone(resolved(config(), "engineer", "tmux"))

    def test_named_environment_takes_default_gateway(self):
        cfg = config(
            environments={"dummy": {"image": "dummy-env:test"}},
            roles={"dummy-tester": {"environment": {"name": "dummy"}}},
        )
        found = resolved(cfg)
        assert found is not None
        self.assertEqual(found.spec, EnvironmentSpec(image="dummy-env:test", gateway="workers"))
        self.assertFalse(found.prefetch)

    def test_local_default_gateway_flows_into_named(self):
        cfg = config(
            environment={"docker": PACKAGED["docker"] | {"gateway": "x"}},
            environments={"dummy": {"image": "dummy-env:test"}},
            roles={"dummy-tester": {"environment": {"name": "dummy"}}},
        )
        found = resolved(cfg)
        assert found is not None
        self.assertEqual(found.spec.gateway, "x")
        engineer = resolved(cfg, "engineer")
        assert engineer is not None
        self.assertEqual(engineer.spec, EnvironmentSpec(image="@worker", gateway="x"))

    def test_role_credentials_replace_whole_declaration(self):
        other = {"item": "ensemble-broker-env/other", "file": "other.txt"}
        cfg = config(
            environments={"dummy": {"image": "dummy-env:test", "credentials": {"env": [SECRET]}}},
            roles={
                "dummy-tester": {
                    "environment": {"name": "dummy", "credentials": {"other-kind": [other]}}
                }
            },
        )
        found = resolved(cfg)
        assert found is not None
        self.assertEqual(json.loads(found.credentials), {"other-kind": [other]})

    def test_role_overrides_without_a_name_go_over_the_default(self):
        cfg = config(roles={"dummy-tester": {"environment": {"lock": "dummy-lock"}}})
        found = resolved(cfg)
        assert found is not None
        self.assertEqual(
            found.spec, EnvironmentSpec(image="@worker", gateway="workers", lock="dummy-lock")
        )

    def test_packaged_default_resolves_to_none(self):
        for table in ({}, {"prefetch": False}, {"gateway": "workers", "plugins": []}):
            with self.subTest(table=table):
                cfg = config(roles={"dummy-tester": {"environment": table}})
                self.assertIsNone(resolved(cfg))

    def test_any_difference_passes_environment(self):
        for table in (
            {"plugins": ["dummy-plugin"]},
            {"lock": "dummy-lock"},
            {"image": "dummy-env:test"},
            {"gateway": "x"},
        ):
            with self.subTest(table=table):
                found = resolved(config(roles={"dummy-tester": {"environment": table}}))
                assert found is not None
                self.assertEqual(found.spec.persisted() | table, found.spec.persisted())
        found = resolved(config(roles={"dummy-tester": {"environment": {"prefetch": True}}}))
        self.assertEqual(
            found, Resolved(EnvironmentSpec(image="@worker", gateway="workers"), prefetch=True)
        )

    def test_a_host_without_a_default_table_refuses_a_selection_or_override(self):
        cfg = config(
            environments={"dummy": {"image": "dummy-env:test"}},
            roles={"dummy-tester": {"environment": {"name": "dummy"}}},
        )
        for host in ("tmux", "d2"):
            with self.subTest(host=host), self.assertRaises(ConfigError) as caught:
                resolved(cfg, host=host)
            self.assertIn(f"[environment.{host}]", str(caught.exception))
            self.assertIn("dummy-tester", str(caught.exception))
        cfg = config(roles={"dummy-tester": {"environment": {"lock": "l"}}})
        with self.assertRaises(ConfigError) as caught:
            resolved(cfg, host="tmux")
        self.assertIn("[environment.tmux]", str(caught.exception))

    def test_resolve_fields_takes_a_name_without_a_role(self):
        cfg = config(environments={"dummy": {"image": "dummy-env:test", "lock": "dummy-lock"}})
        found = resolve_fields(cfg, host="docker", name="dummy", overrides={}, packaged=PACKAGED)
        assert found is not None
        self.assertEqual(found.spec.lock, "dummy-lock")
        with self.assertRaises(ConfigError) as caught:
            resolve_fields(cfg, host="docker", name="nope", overrides={}, packaged=PACKAGED)
        self.assertIn("[environments.nope]", str(caught.exception))

    def test_a_changed_packaged_default_is_compared_without_local_overrides(self):
        cfg = config(environment={"docker": PACKAGED["docker"] | {"plugins": ["p"]}})
        self.assertIsNotNone(resolved(cfg, "engineer"))


class PrefetchTargetTest(Case):
    def target(self, *lines: str) -> tuple[str, str | None]:
        return prefetch_target("\n".join(lines) + "\n\nThe task.\nRepo: ignored/below\n")

    def refused(self, *lines: str) -> str:
        with self.assertRaises(ConfigError) as caught:
            self.target(*lines)
        self.assertTrue(str(caught.exception).startswith("prefetch"), caught.exception)
        return str(caught.exception)

    def test_one_repo_and_an_optional_ref(self):
        self.assertEqual(self.target("Reference: T-1", "Repo: o/r", "Ref: v1.2"), ("o/r", "v1.2"))
        self.assertEqual(self.target("Reference: T-1", "Repo: o/r"), ("o/r", None))
        self.assertEqual(
            self.target("Reference: T-1", "Repo: ~/src/app", "Ref: release/1"),
            ("~/src/app", "release/1"),
        )

    def test_base_is_ignored(self):
        self.assertEqual(self.target("Reference: T-1", "Repo: o/r", "Base: dev"), ("o/r", None))

    def test_two_repo_lines_even_equal(self):
        self.assertIn("Repo:", self.refused("Reference: T-1", "Repo: o/r", "Repo: o/r"))

    def test_two_ref_lines(self):
        self.assertIn("Ref:", self.refused("Reference: T-1", "Repo: o/r", "Ref: a", "Ref: b"))

    def test_unsafe_refs(self):
        for ref in ("--upload-pack=x", "refs/pull/1/head", "deadbeefcafe", "a b", ""):
            with self.subTest(ref=ref):
                self.assertIn("Ref:", self.refused("Reference: T-1", "Repo: o/r", f"Ref: {ref}"))

    def test_an_unreadable_repo(self):
        self.assertIn("Repo:", self.refused("Reference: T-1", "Repo: not a repo!"))

    def test_a_block_opening_with_repo_needs_no_reference_line(self):
        self.assertEqual(self.target("Repo: o/r", "Ref: main"), ("o/r", "main"))
        self.assertEqual(self.target("Repo: o/r"), ("o/r", None))

    def test_no_repo_line_in_the_reference_block(self):
        self.refused("Reference: T-1", "Ref: v1")
        self.refused("Ref: v1", "Repo: o/r")
        self.refused("Brief", "Repo: o/r")
