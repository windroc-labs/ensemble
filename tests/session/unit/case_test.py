import os
import tempfile

from tests.session.case import Case, configured
from tests.session.integration.clients.claude import fake_claude


class DefaultProbeTest(Case):
    def test_a_session_test_never_runs_a_usage_probe_it_did_not_fake(self):
        setup = configured({})._config()
        with self.assertRaisesRegex(AssertionError, "unexpected usage probe"):
            setup.probe(("claude", "-p", "/usage"), cwd="/", env={}, timeout=1)


class FakeClaudeOnPathTest(Case):
    def test_a_path_resolving_claude_to_the_fake_passes(self):
        with tempfile.TemporaryDirectory() as bin_dir:
            fake_claude.check(fake_claude.install(bin_dir))

    def test_a_path_resolving_claude_elsewhere_fails(self):
        with tempfile.TemporaryDirectory() as bin_dir, tempfile.TemporaryDirectory() as other:
            fake_claude.install(bin_dir)
            real = os.path.join(other, "claude")
            with open(real, "w", encoding="utf-8") as f:
                f.write("#!/bin/sh\n")
            os.chmod(real, 0o755)
            for path in (f"{other}:{bin_dir}", "/nonexistent"):
                with self.subTest(path=path), self.assertRaises(AssertionError):
                    fake_claude.check(path)
