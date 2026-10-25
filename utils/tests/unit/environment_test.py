"""Subprocess environment validation and the agent session marker."""

import os
from types import MappingProxyType
from unittest import mock

from ensemble.utils import ACCOUNT_ENV, AGENT_SESSION_ENV, environment_ok, nested_start

from utils.tests.case import Case


class Environment(Case):
    def test_exec_names_and_string_values_only(self):
        env = {"A-B": "", "1A": "a=b", "X;Y": "a\nb", "A B": "v", "Ä": "v"}
        for value, ok in (
            ({}, True),
            (env, True),
            (MappingProxyType(env), True),
            (None, False),
            ([("A", "v")], False),
            ({"": "v"}, False),
            ({"A=B": "v"}, False),
            ({"A\0B": "v"}, False),
            ({1: "v"}, False),
            ({"A": 1}, False),
            ({"A": None}, False),
            ({"A": "v\0"}, False),
        ):
            with self.subTest(env=value):
                self.assertIs(environment_ok(value), ok)


class AccountEnv(Case):
    def test_the_selected_client_account_has_one_name(self):
        self.assertEqual(ACCOUNT_ENV, "ENSEMBLE_ACCOUNT")


class NestedStart(Case):
    def test_none_outside_an_agent_session_and_the_reason_inside_one(self):
        self.assertEqual(AGENT_SESSION_ENV, "ENSEMBLE_AGENT_SESSION")
        with mock.patch.dict(os.environ, clear=True):
            self.assertIsNone(nested_start())
            os.environ[AGENT_SESSION_ENV] = ""
            self.assertIsNone(nested_start())
            os.environ[AGENT_SESSION_ENV] = "1"
            self.assertEqual(
                nested_start(),
                "refused inside an agent session ensemble started ($ENSEMBLE_AGENT_SESSION is set): "
                "only a client you start starts sessions and runs",
            )
