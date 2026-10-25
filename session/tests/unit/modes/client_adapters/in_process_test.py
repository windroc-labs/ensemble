"""InProcess contract tests."""

from ensemble.session.engine.types import Access, Reply, Spec
from ensemble.session.modes.client_adapters.in_process import InProcess

from session.tests.case import Case
from session.tests.client_adapters_support import StubInProcess


class CheckOnlyInProcess(InProcess):
    def check(self, spec: Spec):
        return None


class InProcessTest(Case):
    def test_run_is_its_only_required_method(self):
        spec = Spec(workdir="/w", access=Access(cwd="/w"), prompt="p")
        self.assertEqual(list(StubInProcess().run(spec)), [Reply("p")])
        with self.assertRaisesRegex(TypeError, "run"):
            CheckOnlyInProcess()

    def test_checks_nothing_by_default(self):
        run = StubInProcess()
        for resume in (False, True):
            with self.subTest(resume=resume):
                self.assertIsNone(
                    run.check(Spec(workdir="/w", access=Access(cwd="/w"), sid="s", resume=resume))
                )
