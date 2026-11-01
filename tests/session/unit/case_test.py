from tests.session.case import Case


class DefaultProbeTest(Case):
    def test_a_unit_test_never_runs_a_usage_probe_it_did_not_fake(self):
        self.configure_session({})
        setup = self.session_cls._config()
        with self.assertRaisesRegex(AssertionError, "unexpected usage probe"):
            setup.probe(("claude", "-p", "/usage"), cwd="/", env={}, timeout=1)
