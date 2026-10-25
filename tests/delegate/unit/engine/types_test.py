"""Delegate Spec fields, defaults and immutability; Layout's checkouts follow the core config's."""

import dataclasses
import os
import re
import unittest
from importlib.resources import files

from ensemble import delegate
from ensemble.delegate.engine.types import Layout, Result
from tests.delegate.case import Case
from tests.delegate.plans import Base

SPEC = {
    "input": None,
    "workdir": None,
    "task": None,
    "mode": "headless",
    "client": None,
    "sid": None,
    "resume": False,
    "name": None,
    "events": None,
    "out": None,
    "repo": None,
    "cwd": None,
    "team": None,
}


class SpecTest(Case):
    def test_a_frozen_spec_its_fields_and_defaults(self):
        found = {
            f.name: None if f.default is dataclasses.MISSING else f.default
            for f in dataclasses.fields(delegate.Spec)
        }
        self.assertEqual(found, SPEC)
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
