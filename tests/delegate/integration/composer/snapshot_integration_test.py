"""A Composer reads current configuration once and keeps that snapshot for its lifetime."""

from pathlib import Path
from unittest import mock

from ensemble.delegate.composer import Composer, RunParams
from tests.delegate.case import Case


class ConfigSnapshot(Case):
    def test_later_file_changes_and_reader_failure_do_not_change_existing_composer(self):
        local = Path(self.ensemble, "config.local.toml")
        local.write_text(
            '[delegate]\nlanguage = "First"\ntrusted_dirs = ["/first"]\n', encoding="utf-8"
        )
        first = Composer()
        local.write_text(
            '[delegate]\nlanguage = "Second"\ntrusted_dirs = ["/second"]\n', encoding="utf-8"
        )
        second = Composer()
        params = RunParams(input="Echo.", out="/corpus/work/out.md", workdir="/corpus/work")
        with mock.patch("tomllib.load", side_effect=AssertionError("configuration reread")):
            self.assertEqual(first.resolve("dummy-tester").language, "First")
            self.assertEqual(second.resolve("dummy-tester").language, "Second")
            self.assertEqual(first.trusted_dirs(), frozenset({"/first"}))
            self.assertEqual(second.trusted_dirs(), frozenset({"/second"}))
            self.assertEqual(
                list(first.index()), ["researcher", "pm", "engineer", "verifier", "dummy-tester"]
            )
            self.assertIn(
                "are in First", first.render(first.resolve("dummy-tester", "echo"), params)
            )
            self.assertIn(
                "are in Second", second.render(second.resolve("dummy-tester", "echo"), params)
            )
