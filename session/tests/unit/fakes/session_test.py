"""session/tests/fakes/session.py: FakeSession plays its script in the caller's process, with no
Host to display on.
"""

import inspect
import unittest

from ensemble.session import Mode, NotShowable, Session, SessionError, Usage
from ensemble.session.engine.types import Access, ConfigError, Exited, Hosts, Reply, Spec, TurnEnd

from session.tests.case import Case
from session.tests.fakes.host import FakeHost
from session.tests.fakes.session import FakeSession


class Scripted(Case):
    def spec(self, **fields) -> Spec:
        return Spec(
            **{
                "workdir": self.home,
                "access": Access(cwd=self.home),
                "client": "fake",
                "mode": Mode.HEADLESS,
                "prompt": "the prompt",
                **fields,
            }
        )

    def start(self, cls, name=None, display=None, idle_timeout=None, **fields):
        return cls.start(self.spec(name=name, **fields), display=display, idle_timeout=idle_timeout)

    def test_its_class_methods_take_the_abcs_parameters(self):
        for name in (
            "configure",
            "start",
            "attach",
            "list",
            "usage",
            "layout",
            "writable_dirs",
            "display",
            "host",
        ):
            with self.subTest(name=name):
                fake, abc = (
                    [(p.name, p.kind, p.default) for p in inspect.signature(m).parameters.values()]
                    for m in (getattr(FakeSession, name), getattr(Session, name))
                )
                self.assertEqual(fake, abc)

    def test_events_are_the_script_then_exited_0_and_reply_the_last_one(self):
        session = self.start(FakeSession.scripted(Reply("first"), TurnEnd("second"), TurnEnd()))
        self.assertEqual(
            list(session.events()), [Reply("first"), TurnEnd("second"), TurnEnd(), Exited(0)]
        )
        self.assertEqual(session.reply, "second")
        self.assertEqual(list(session.events()), [])

    def test_a_script_ending_with_exited_keeps_its_returncode(self):
        self.assertEqual(list(self.start(FakeSession.scripted(Exited(3))).events()), [Exited(3)])

    def test_start_records_its_arguments_and_names_the_session_from_the_sid(self):
        cls = FakeSession.scripted()
        session = self.start(cls, sid="abc12345-6789", resume=True, display="d", idle_timeout=2)
        self.assertEqual((session.sid, session.name), ("abc12345-6789", "fake-abc12345"))
        self.assertEqual((session.client, session.mode), ("fake", Mode.HEADLESS))
        self.assertEqual(cls.started[0]["spec"].prompt, "the prompt")
        self.assertEqual((cls.started[0]["display"], cls.started[0]["idle_timeout"]), ("d", 2))
        self.assertEqual(session.info().cwd, self.home)
        session.send("more")
        self.assertEqual(session.sent, ["more"])

    def test_a_spec_without_client_or_mode_is_refused(self):
        for field in ("client", "mode"):
            spec = Spec(
                workdir=self.home,
                access=Access(cwd=self.home),
                **{"client": "fake", "mode": Mode.TUI, field: None},
            )
            with self.subTest(field=field), self.assertRaises(ConfigError) as caught:
                FakeSession.scripted().start(spec)
            self.assertEqual(str(caught.exception), f"spec.{field}: required to start")

    def test_a_live_name_is_refused_until_closed(self):
        cls = FakeSession.scripted()
        first = self.start(cls, name="w1")
        with self.assertRaises(SessionError):
            self.start(cls, name="w1")
        first.close()
        self.assertFalse(first.alive())
        self.assertTrue(self.start(cls, name="w1").alive())

    def test_attach_and_list_reach_only_live_sessions(self):
        cls = FakeSession.scripted()
        one, two = self.start(cls, name="w1"), self.start(cls, name="w2")
        self.assertIs(cls.attach("w1"), one)
        self.assertEqual([row.name for row in cls.list()], ["w1", "w2"])
        self.assertEqual([row.name for row in cls.list("w2")], ["w2"])
        two.stop()
        self.assertEqual([row.name for row in cls.list()], ["w1"])
        for name in ("w2", "nope"):
            with self.subTest(name=name), self.assertRaises(SessionError):
                cls.attach(name)

    def test_it_cannot_show(self):
        session = self.start(FakeSession.scripted())
        self.assertFalse(session.showable)
        for method in (session.show, session.attach_command, session.screen):
            with self.subTest(method=method.__name__), self.assertRaises(NotShowable):
                method()

    def test_class_methods_answer_as_a_session_layer_with_one_fake_host(self):
        cls = FakeSession.scripted()
        self.assertIsNone(cls.usage("fake"))
        self.assertIsNone(cls.layout())
        self.assertEqual(cls.writable_dirs(self.spec()), ())
        self.assertEqual(cls.display("d"), "d")
        host = cls.host()
        self.assertIsInstance(host, FakeHost)
        self.assertFalse(host.attachable)
        self.assertEqual((host.name, cls.host("h").name), ("fake", "h"))

    def test_classes_from_scripted_share_no_state(self):
        first, second = FakeSession.scripted(Reply("a")), FakeSession.scripted()
        self.start(first, name="w1")
        first.configure(clients={}, hosts=Hosts("fake", {}))
        first.usage_result = Usage(None, None)
        self.assertEqual((second.started, second.live, second.script), ([], {}, ()))
        self.assertIsNone(second.configured)
        self.assertIsNone(second.usage("fake"))
        self.assertEqual(first.configured["hosts"].default, "fake")
        self.assertEqual(first.usage("fake"), Usage(None, None))
        self.assertEqual(len(first.started), 1)
        self.start(second, name="w1")

    def test_a_fresh_class_plays_an_empty_script_on_state_no_subclass_shares(self):
        cls = FakeSession.scripted()
        session = self.start(cls, name="w1")
        cls.configure(clients={}, hosts=Hosts("fake", {}))
        self.assertEqual(list(session.events()), [Exited(0)])
        self.assertIs(cls.attach("w1"), session)
        self.assertEqual([row.name for row in cls.list()], ["w1"])
        self.assertEqual([call["spec"].name for call in cls.started], ["w1"])
        other = cls.scripted()
        self.assertEqual((other.started, other.live, other.configured), ([], {}, None))
        self.start(other, name="w1")
        self.assertEqual(len(cls.started), 1)


if __name__ == "__main__":
    unittest.main()
