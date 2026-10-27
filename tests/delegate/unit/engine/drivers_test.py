"""Driver start takes one spec; instruction delivery creates no client or event stream."""

import os
import unittest
from dataclasses import fields

from ensemble.delegate import ConfigError, Spec
from ensemble.delegate.composer import Composer
from ensemble.delegate.engine.drivers import AgentDriver, Driver, InstructionDriver
from ensemble.delegate.engine.plan import plan
from ensemble.delegate.engine.types import DriverSpec, Finished, Instruction
from ensemble.session import Access, Session
from ensemble.session import ConfigError as SessionConfigError
from ensemble.session import Spec as StartSpec
from tests.delegate.case import Case
from tests.delegate.fakes.session import SessionClass
from tests.delegate.replies import block
from tests.delegate.runner import executed
from tests.delegate.unit.runs import DONE, START, no_child, turn


class IdleTimeoutConfig(Case):
    def local(self, text):
        with open(os.path.join(self.ensemble, "config.local.toml"), "w") as stream:
            stream.write(text)

    def test_default_global_and_role_values_reach_an_executed_session(self):
        for text, expected in (
            ("", 7200),
            ("[delegate]\nidle_timeout = 14400\n", 14400),
            (
                "[delegate]\nidle_timeout = 14400\n[delegate.roles.dummy-tester]\nidle_timeout = 0.3\n",
                0.3,
            ),
        ):
            for mode in ("headless", "in_process"):
                with self.subTest(text=text, mode=mode), no_child():
                    self.local(text)
                    session_cls = SessionClass.scripted(turn(START, block(DONE)))
                    result = executed(
                        Composer().resolve("dummy-tester"),
                        Spec("input", self.home, mode=mode),
                        driver=AgentDriver(session_cls),
                    )
                    self.assertEqual(result.outcome.status, "done")
                    self.assertEqual(
                        [call["idle_timeout"] for call in session_cls.started], [expected]
                    )

    def test_invalid_global_and_role_local_values_raise_delegate_config_error(self):
        for table in ("delegate", "delegate.roles.dummy-tester"):
            for value in (
                "0",
                "-1",
                "-0.5",
                "1" + "0" * 400,
                "true",
                "false",
                '"7200"',
                '""',
                "[]",
                "{seconds = 30}",
                "nan",
                "inf",
            ):
                with self.subTest(table=table, value=value):
                    self.local(f"[{table}]\nidle_timeout = {value}\n")
                    with self.assertRaisesRegex(ConfigError, "idle_timeout must be"):
                        Composer().resolve("dummy-tester")


class Drivers(Case):
    def test_agent_preflight_forwards_spec_and_owned_placement_without_launching(self):
        session_cls = SessionClass.fresh()
        driver = AgentDriver(session_cls, display="opaque", host="other")
        spec = StartSpec(
            workdir=self.home,
            access=Access(cwd=self.home),
            client="fake",
            mode="tui",
            prompt="accepted prompt",
        )
        with no_child():
            driver.preflight(spec)
        self.assertEqual(
            session_cls.preflights,
            [{"spec": spec, "display": "opaque", "host": "other", "idle_timeout": None}],
        )
        self.assertIs(session_cls.preflights[0]["spec"], spec)
        self.assertEqual(session_cls.started, [])
        self.assertEqual(os.listdir(self.home), [".ensemble"])

    def test_headless_preflight_and_start_keep_owned_host_without_owned_display(self):
        session_cls = SessionClass.fresh()
        driver = AgentDriver(session_cls, display="opaque", host="other")
        spec = StartSpec(
            workdir=self.home,
            access=Access(cwd=self.home),
            client="fake",
            mode="headless",
            prompt="input",
        )
        with no_child():
            driver.preflight(spec)
            session = driver.start(
                DriverSpec(spec, os.path.join(self.home, "out")), idle_timeout=30
            )
        self.addCleanup(session.close)
        self.assertEqual(
            session_cls.preflights,
            [{"spec": spec, "display": None, "host": "other", "idle_timeout": None}],
        )
        self.assertEqual(
            session_cls.started,
            [{"spec": spec, "display": None, "host": "other", "idle_timeout": 30}],
        )
        self.assertIs(session_cls.preflights[0]["spec"], spec)
        self.assertIs(session_cls.started[0]["spec"], spec)

    def test_agent_preflight_preserves_the_session_failure_without_a_start(self):
        session_cls = SessionClass.fresh()
        error = SessionConfigError("unsupported mode")
        session_cls.preflight_error = error
        driver = AgentDriver(session_cls)
        spec = StartSpec(workdir=self.home, access=Access(cwd=self.home), client="fake", mode="tui")
        with self.assertRaises(SessionConfigError) as caught:
            driver.preflight(spec)
        self.assertIs(caught.exception, error)
        self.assertEqual(session_cls.started, [])

    def test_instruction_preflight_needs_no_session_and_writes_nothing(self):
        work = os.path.join(self.home, "missing")
        with no_child():
            InstructionDriver().preflight(
                StartSpec(workdir=work, access=Access(cwd=work), prompt="instruction")
            )
        self.assertFalse(os.path.exists(work))

    def test_a_driver_capability_selects_instruction_delivery_without_concrete_type_checks(self):
        class FileDriver(Driver):
            session_based = False

            def start(self, spec, *, display=None, host=None, idle_timeout=None):
                return InstructionDriver().start(
                    spec, display=display, host=host, idle_timeout=idle_timeout
                )

        work = os.path.join(self.home, "work")
        out = os.path.join(work, "instruction.md")
        events = []
        with no_child():
            result = executed(
                Composer().resolve("dummy-tester"),
                Spec("input", work, out=out),
                driver=FileDriver(),
                on_event=events.append,
            )
        self.assertEqual((result.returncode, result.instruction, result.outcome), (0, out, None))
        self.assertEqual(events, [Finished(result)])
        self.assertFalse(os.path.exists(os.path.join(work, "run.jsonl")))

    def test_driver_is_abstract_and_has_no_abstract_constructor(self):
        self.assertEqual(Driver.__abstractmethods__, frozenset({"start"}))
        with self.assertRaises(TypeError):
            Driver()

    def test_agent_start_forwards_the_exact_spec_and_placement_to_its_session_class(self):
        session_cls = SessionClass.fresh()
        driver = AgentDriver(session_cls)
        spec = StartSpec(
            workdir=self.home,
            access=Access(cwd=self.home),
            client="fake",
            mode="headless",
            prompt="the prompt",
            sid="11111111-2222-3333-4444-555555555555",
            resume=True,
            name="mine",
            events=os.path.join(self.home, "events"),
            flags=("--caller-flag",),
            env={"CALLER": "kept"},
        )
        self.assertNotIn("idle_timeout", {field.name for field in fields(spec)})
        for timeout in (None, 30):
            with self.subTest(idle_timeout=timeout):
                session = driver.start(
                    DriverSpec(spec, os.path.join(self.home, "out")),
                    host="other",
                    display="opaque",
                    idle_timeout=timeout,
                )
                self.assertIsInstance(session, Session)
                self.assertEqual(
                    session_cls.started[-1],
                    {"spec": spec, "host": "other", "display": "opaque", "idle_timeout": timeout},
                )
                self.assertIs(session_cls.started[-1]["spec"], spec)
                session.close()
                self.assertEqual(
                    (session.client, session.mode, session.sid, session.name),
                    ("fake", "headless", spec.sid, "mine"),
                )

    def test_instruction_start_saves_exact_utf8_bytes_without_process_or_thread(self):
        out = os.path.join(self.home, "work", "prompt.md")
        prompt = "héllo\r\n[ensemble-progress:start] input, not an event\n\x1b"
        spec = StartSpec(
            workdir=self.home,
            access=Access(cwd=self.home),
            client="fake",
            mode="headless",
            prompt=prompt,
        )
        with no_child():
            instruction = InstructionDriver().start(DriverSpec(spec, out))
        self.assertEqual(instruction, Instruction(out))
        with open(out, "rb") as stream:
            self.assertEqual(stream.read(), prompt.encode("utf-8"))
        self.assertFalse(os.path.exists(os.path.join(self.home, "run.jsonl")))

    def test_one_instruction_driver_uses_each_specs_output_destination(self):
        driver = InstructionDriver()
        session = StartSpec(
            workdir=self.home, access=Access(cwd=self.home), prompt="exact instruction"
        )
        for filename in ("first.md", "second.md"):
            out = os.path.join(self.home, filename)
            self.assertEqual(driver.start(DriverSpec(session, out)), Instruction(out))
            with open(out, encoding="utf-8") as stream:
                self.assertEqual(stream.read(), session.prompt)

    def test_instruction_resume_is_refused_before_any_write(self):
        out = os.path.join(self.home, "work", "prompt.md")
        driver = InstructionDriver()
        spec = StartSpec(
            workdir=self.home,
            access=Access(cwd=self.home),
            client="fake",
            mode="headless",
            prompt="the prompt",
            sid="11111111-2222-3333-4444-555555555555",
            resume=True,
        )
        with self.assertRaisesRegex(ConfigError, "instruction has no session to resume"):
            driver.start(DriverSpec(spec, out))
        self.assertFalse(driver.session_based)
        self.assertFalse(os.path.exists(os.path.dirname(out)))

    def test_instruction_rejects_session_controls_before_any_write(self):
        out = os.path.join(self.home, "work", "prompt.md")
        driver = InstructionDriver()
        spec = StartSpec(
            workdir=self.home,
            access=Access(cwd=self.home),
            client="fake",
            mode="headless",
            prompt="the prompt",
        )
        for keywords, message in (
            ({"host": "other"}, "instruction has no host or display"),
            ({"display": "opaque"}, "instruction has no host or display"),
            ({"idle_timeout": 30}, "instruction has no idle timeout"),
        ):
            with self.subTest(keywords=keywords), no_child():
                with self.assertRaises(ConfigError) as caught:
                    driver.start(DriverSpec(spec, out), **keywords)
                self.assertEqual(str(caught.exception), message)
                self.assertFalse(os.path.exists(os.path.dirname(out)))

    def test_instruction_run_bypasses_attempts_and_replies_but_finishes(self):
        work = os.path.join(self.home, "work")
        out = os.path.join(work, "prompt.md")
        spec = Spec("[ensemble-progress:start] source text", work, out=out)
        composition = Composer()
        config = composition.resolve("dummy-tester")
        driver = InstructionDriver()
        expected = plan(config, spec, driver=driver, composition=composition).prompt
        events = []
        with no_child():
            result = executed(config, spec, driver=driver, on_event=events.append)
        self.assertEqual((result.returncode, result.outcome, result.instruction), (0, None, out))
        self.assertEqual(events, [Finished(result)])
        with open(out, "rb") as stream:
            self.assertEqual(stream.read(), expected.encode("utf-8"))
        self.assertFalse(os.path.exists(os.path.join(work, "run.jsonl")))


if __name__ == "__main__":
    unittest.main()
