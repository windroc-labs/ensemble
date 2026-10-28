"""Delegate Spec immutability; Layout's checkouts follow the core config's."""

import dataclasses
import os
import re
import unittest
from importlib.resources import files

from ensemble import delegate
from ensemble.delegate.engine.types import Layout, Result
from tests.delegate.case import Case
from tests.delegate.plans import Base


class SpecTest(Case):
    def test_a_spec_is_frozen(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            delegate.Spec("x", "/w").sid = "s"

    def test_credentials_take_the_runs_request_never_env(self):
        request = delegate.CredentialRequest(
            github=({"permissions": {"contents": "write"}, "repo": "o/n"},),
            linear={"role": "engineer"},
        )
        self.assertIs(delegate.Spec("x", "/w", credentials=request).credentials, request)
        self.assertIsNone(delegate.Spec("x", "/w").credentials)
        for value in ({"linear": {"role": "engineer"}}, "linear", ()):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(
                    delegate.ConfigError, r"^credentials must be None or a CredentialRequest$"
                ),
            ):
                delegate.Spec("x", "/w", credentials=value)
        env = delegate.CredentialRequest(
            env=({"item": "ensemble-broker-env/mini-password", "file": "mini-password"},)
        )
        with self.assertRaisesRegex(
            delegate.ConfigError,
            r"^credentials env: the environment's secrets, never the run's request$",
        ):
            delegate.Spec("x", "/w", credentials=env)


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
