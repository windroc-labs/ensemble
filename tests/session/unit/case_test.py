from tests.session.case import Case, configured


class DefaultProbeTest(Case):
    def test_a_session_test_never_runs_a_usage_probe_it_did_not_fake(self):
        setup = configured({})._config()
        with self.assertRaisesRegex(AssertionError, "unexpected usage probe"):
            setup.probe(("claude", "-p", "/usage"), cwd="/", env={}, timeout=1)
