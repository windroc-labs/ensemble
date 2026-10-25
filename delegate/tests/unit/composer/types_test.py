"""composer/types.py: RunConfig's effort is the session layer's Effort."""

from dataclasses import replace

from ensemble.delegate.composer import ConfigError, RunConfig
from ensemble.session import Effort

from delegate.tests.case import Case


def run_config(effort) -> RunConfig:
    return RunConfig(
        role="writer",
        tier=2,
        effort=effort,
        client="a",
        output={"type": "local"},
        idle_timeout=60,
        show="",
    )


class RunConfigEffort(Case):
    def test_each_effort_and_its_value_is_accepted(self):
        for effort in (*Effort, "xhigh"):
            with self.subTest(effort=effort):
                self.assertIs(run_config(effort).effort, Effort(effort))

    def test_another_value_is_refused_naming_each_effort(self):
        for bad, shown in (("ultra", "'ultra'"), ("LOW", "'LOW'"), (3, "3"), (["low"], "['low']")):
            with self.subTest(bad=bad), self.assertRaises(ConfigError) as cm:
                run_config(bad)
            self.assertEqual(
                str(cm.exception),
                f"effort must be one of low, medium, high, xhigh, max, got {shown}",
            )


class RunConfigIdleTimeout(Case):
    def test_it_has_no_default(self):
        with self.assertRaisesRegex(TypeError, "idle_timeout"):
            RunConfig(role="writer", tier=2, effort="high", client="a", output={"type": "local"})

    def test_positive_integer_and_fractional_seconds_are_accepted(self):
        for seconds in (1, 0.3, 14400):
            with self.subTest(seconds=seconds):
                self.assertEqual(
                    replace(run_config("high"), idle_timeout=seconds).idle_timeout, seconds
                )

    def test_invalid_values_raise_delegate_config_error(self):
        for value in (
            0,
            -1,
            -0.5,
            True,
            False,
            "7200",
            "",
            None,
            [],
            {},
            float("nan"),
            float("inf"),
            10**400,
        ):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(
                    ConfigError, "idle_timeout must be a positive finite number of seconds"
                ),
            ):
                replace(run_config("high"), idle_timeout=value)
