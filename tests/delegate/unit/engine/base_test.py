"""delegate/engine/base.py: the Delegate ABC, no constructor and delegate/__init__.py's new exports
(the Spec: types_test.py).
"""

import inspect
import unittest

from ensemble import delegate, session
from ensemble.delegate import Delegate, composer, event
from ensemble.session import Host, Session
from tests.delegate.case import Case

ABSTRACT = {
    "create",
    "attach",
    "usage",
    "roles",
    "resolve",
    "trusted_dirs",
    "checkouts",
    "clean",
    "check_tui",
    "detach",
    "run",
    "alive",
    "status",
    "content",
    "outcome",
    "show",
    "hide",
    "attach_command",
    "close",
    "session_status",
    "resume",
}


class Exports(Case):
    def test_all_holds_the_new_names_each_bound(self):
        for name in ("Delegate", "Spec", "RunConfig", "DelegateEvent"):
            with self.subTest(name=name):
                self.assertIn(name, delegate.__all__)
        self.assertEqual([n for n in delegate.__all__ if not hasattr(delegate, n)], [])
        self.assertIs(delegate.RunConfig, composer.RunConfig)
        self.assertIs(delegate.DelegateEvent, event.DelegateEvent)

    def test_session_types_are_public_at_the_delegate_boundary(self):
        for name in ("Resumable", "Window", "CredentialRequest"):
            with self.subTest(name=name):
                self.assertIn(name, delegate.__all__)
                self.assertIs(getattr(delegate, name), getattr(session, name))

    def test_the_public_package_exports_the_harness_implementation(self):
        from ensemble.delegate.engine.harness_delegate import HarnessDelegate

        self.assertIn("HarnessDelegate", delegate.__all__)
        self.assertIs(delegate.HarnessDelegate, HarnessDelegate)

    def test_only_the_harness_concrete_implementation_is_exported(self):
        exported = [getattr(module, n) for module in (session, delegate) for n in module.__all__]
        concrete = [
            value
            for value in exported
            if inspect.isclass(value)
            and issubclass(value, (Session, Host, Delegate))
            and not inspect.isabstract(value)
        ]
        self.assertEqual(concrete, [delegate.HarnessDelegate])


class Abstract(Case):
    def test_delegate_is_abstract_in_every_member(self):
        self.assertTrue(inspect.isabstract(Delegate))
        self.assertEqual(Delegate.__abstractmethods__, ABSTRACT)

    def test_no_constructor_is_declared(self):
        self.assertNotIn("__init__", vars(Delegate))


if __name__ == "__main__":
    unittest.main()
