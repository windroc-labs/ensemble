"""Session abstract interface and class methods."""

import inspect

from ensemble.session import Access, ConfigError, Mode, Session, Spec
from ensemble.session.engine.types import Hosts, ProcessOptions
from tests.session.case import Case
from tests.session.fakes.host import FakeHost
from tests.session.fakes.session import FakeSession

ABSTRACT = {
    Session: {
        "events",
        "send",
        "alive",
        "status",
        "content",
        "stop",
        "info",
        "reply",
        "close",
        "session_status",
        "record",
        "showable",
        "show",
        "hide",
        "attach_command",
        "screen",
        "press",
    }
}


class Abstract(Case):
    def test_each_interface_is_abstract_in_its_methods(self):
        for cls, methods in ABSTRACT.items():
            with self.subTest(cls=cls.__name__):
                self.assertTrue(inspect.isabstract(cls))
                self.assertEqual(cls.__abstractmethods__, methods)


class SessionClassMethods(Case):
    def test_policy_validation_needs_no_session_or_workdir(self):
        Session.validate_policy(flags=("--flag",), env={"TOKEN": "value", "A-B": "value"})
        for flags, env, message in (
            (("bad\0flag",), {}, "flags must be a tuple of strings without NUL"),
            ((), {"NOT=A=NAME": "value"}, "env must map variable names to strings without NUL"),
            ((), {"TOKEN": "bad\0value"}, "env must map variable names to strings without NUL"),
        ):
            with self.subTest(flags=flags, env=env):
                with self.assertRaises(ConfigError) as caught:
                    Session.validate_policy(flags=flags, env=env)
                self.assertEqual(str(caught.exception), message)
                with self.assertRaises(ConfigError) as spec_error:
                    Spec(workdir=self.home, access=Access(cwd=self.home), flags=flags, env=env)
                self.assertEqual(str(spec_error.exception), message)
                with self.assertRaises(ConfigError) as options_error:
                    ProcessOptions(flags=flags, env=env)
                self.assertEqual(str(options_error.exception), message)

    def test_a_policy_env_cannot_select_an_account(self):
        for value in ("work", ""):
            with self.subTest(value=value), self.assertRaises(ConfigError) as caught:
                Session.validate_policy(flags=(), env={"TOKEN": "v", "ENSEMBLE_ACCOUNT": value})
            self.assertEqual(
                str(caught.exception),
                "ENSEMBLE_ACCOUNT is set by --account or [clients.<client>].account or local_account",
            )

    def test_public_config_boundary_rejects_missing_hosts_without_a_session(self):
        with self.assertRaises(ConfigError) as caught:
            Session.configure_from({})
        self.assertEqual(str(caught.exception), "[hosts]: missing")

    def test_on_a_subclass_each_is_its_own(self):
        self.assertIsInstance(FakeSession.host(), FakeHost)
        self.assertEqual(FakeSession.display("d"), "d")
        own = type("Own", (Session,), {})
        spec = Spec(
            workdir=self.home,
            access=Access(cwd=self.home),
            client="fake",
            mode=Mode.HEADLESS,
            prompt="hi",
        )
        calls = {
            "configure": ((), {"clients": {}, "hosts": Hosts("tmux", {})}),
            "configure_from": (({},), {}),
            "start": ((spec,), {}),
            "attach": (("w1",), {}),
            "inspect": ((spec,), {}),
            "list": ((), {}),
            "layout": ((), {}),
            "writable_dirs": ((spec,), {}),
            "display": ((), {}),
            "host": ((), {}),
            "class_roots": (("fake",), {}),
        }
        for name, (args, kwargs) in calls.items():
            with self.subTest(name=name), self.assertRaises(NotImplementedError) as caught:
                getattr(own, name)(*args, **kwargs)
            self.assertEqual(str(caught.exception), f"Own.{name}: not implemented")
