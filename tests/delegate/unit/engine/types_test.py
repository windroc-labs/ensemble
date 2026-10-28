"""Delegate Spec immutability; Layout's checkouts follow the core config's."""

import dataclasses
import json
import os
import re
import unittest
from importlib.resources import files

from ensemble import delegate
from ensemble.delegate.engine.types import Layout, Result
from tests.delegate.case import REPO, Case
from tests.delegate.plans import Base


class SpecTest(Case):
    def test_a_spec_is_frozen(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            delegate.Spec("x", "/w").sid = "s"


class LayoutTest(Case):
    def test_layout_stays_inside_delegate(self):
        self.assertNotIn("Layout", delegate.__all__)
        self.assertFalse(hasattr(delegate, "Layout"))

    def test_checkouts_are_where_core_config_commands_clone(self):
        with files("ensemble.delegate").joinpath("config.toml").open() as f:
            dirs = set(re.findall(r"--dir \{\{workdir\}\}/([^\s\"]+)", f.read()))
        self.assertEqual(len(dirs), 1)
        layout = Layout(os.path.join(self.home, "w"))
        checkout = os.path.join(layout.agent, *dirs, "o", "n-TASK-1")
        os.makedirs(checkout)
        self.assertEqual(layout.checkouts(), (os.path.realpath(checkout),))


class ResultTest(Case):
    def test_instruction_is_a_path_and_defaults_to_none(self):
        result = Result(0, None, instruction="/work/prompt.md")
        self.assertEqual(
            dataclasses.asdict(result),
            {
                "returncode": 0,
                "outcome": None,
                "error": "",
                "limit": None,
                "instruction": "/work/prompt.md",
            },
        )
        self.assertIsNone(Result(0, None).instruction)


class PlanTest(Base):
    def test_credentials_pass_to_session_without_merging(self):
        with open(
            os.path.join(
                REPO, "src/broker/protocol/docker-broker/fixtures/credential-spec-test-kind.json"
            )
        ) as file:
            fixture = json.load(file)
        caller = fixture["credentials"]
        environment = fixture["environment_credentials"]
        planned, _ = self.planned(credentials=caller, environment_credentials=environment)
        self.assertEqual(planned.session.credentials, caller)
        self.assertEqual(planned.session.environment_credentials, environment)

    def test_a_resolved_plan_has_a_session_id(self):
        planned, _ = self.planned()
        self.assertEqual(planned.sid, planned.spec.sid)

    def test_a_plan_without_a_session_id_is_invalid(self):
        planned, _ = self.planned()
        missing = dataclasses.replace(planned, spec=dataclasses.replace(planned.spec, sid=None))
        with self.assertRaisesRegex(delegate.ConfigError, "plan has no session id"):
            _ = missing.sid


if __name__ == "__main__":
    unittest.main()
