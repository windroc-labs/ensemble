"""HostedSession's sessions of the fake client: real child processes."""

import contextlib
import json
import os
import threading
import unittest
import uuid
from unittest import mock

from ensemble.session import NotShowable, Session
from ensemble.session.clients.base import Client
from ensemble.session.engine.types import (
    Access,
    Exited,
    Mode,
    SessionError,
    SessionInfo,
    SessionStatus,
    Spec,
    TurnFailed,
    Usage,
    Window,
)
from tests.session.case import no_spawn
from tests.session.hosted_support import (
    LAUNCHED,
    PYTHONPATH,
    Base,
    EnvBase,
    HostedTuiBase,
    ProbeClient,
)


class SessionTest(Base):
    def test_send_and_screen_are_tui_only(self):
        session = self.start()
        for call, message in (
            (lambda: session.send("x"), "send: tui mode only"),
            (session.screen, "screen: tui mode only"),
        ):
            with self.subTest(message=message), self.assertRaises(SessionError) as caught:
                call()
            self.assertEqual(str(caught.exception), message)
        self.assertEqual(self.drain(session), [TurnFailed("incomplete_turn"), Exited(0)])

    def test_alive_while_the_process_runs_then_dead(self):
        self.steps({"sleep": 1})
        session = self.start()
        self.assertTrue(session.alive())
        self.assertEqual(self.drain(session), [TurnFailed("incomplete_turn"), Exited(0)])
        self.assertEqual((session.alive(), session.status().state), (False, "dead"))

    def test_attributes_info_reply_and_the_run_log(self):
        cwd = os.path.join(self.dir, "cwd")
        os.mkdir(cwd)
        session = self.start(spec=self.spec(access=Access(cwd=cwd, project=True)), sid="s1")
        self.assertIsInstance(session, Session)
        self.assertEqual((session.sid, session.client, session.mode), ("s1", "fake", "headless"))
        self.assertIs(session.mode, Mode.HEADLESS)
        info = SessionInfo(
            sid="s1",
            cwd=cwd,
            project=True,
            transcript=os.path.join(self.workdir, "fake-s1.jsonl"),
            resume="fake --resume s1",
        )
        self.assertEqual(session.info(), info)
        self.assertIsNone(session.reply)
        self.assertEqual(self.drain(session), [TurnFailed("incomplete_turn"), Exited(0)])
        (line,) = self.written()
        self.assertEqual(
            {k: v for k, v in line.items() if k != "ts"},
            {
                "kind": "session",
                "sid": "s1",
                "cwd": cwd,
                "project": True,
                "transcript": info.transcript,
                "resume": info.resume,
            },
        )

    def test_its_session_status_is_its_clients_for_its_sid_and_cwd(self):
        session = self.start(client="probe", sid="s1")
        self.assertEqual(
            session.session_status(), SessionStatus("yes", 1.5, f"probe s1 {self.workdir}")
        )
        with mock.patch.object(
            ProbeClient, "session_status", side_effect=PermissionError(13, "denied")
        ):
            self.assertEqual(session.session_status(), SessionStatus("unknown", None, ""))

    def test_inspect_an_ended_headless_session_without_starting_or_attaching(self):
        session = self.start(
            client="probe", sid="history", spec=self.spec(access=Access(cwd=self.dir))
        )
        self.assertEqual(self.drain(session), [TurnFailed("incomplete_turn"), Exited(0)])
        session.close()
        with no_spawn():
            status = self.session.inspect(
                Spec(
                    workdir=self.workdir,
                    access=Access(cwd=self.workdir),
                    client="probe",
                    sid=session.sid,
                )
            )
        self.assertEqual(status, SessionStatus("yes", 1.5, f"probe history {self.dir}"))

    def test_a_headless_session_cannot_show_and_has_no_record(self):
        session = self.start()
        self.assertFalse(session.showable)
        self.assertIsNone(session.record())
        for name, call in (
            ("show", session.show),
            ("attach_command", session.attach_command),
            ("screen", session.screen),
        ):
            with self.subTest(name=name), self.assertRaises(NotShowable) as caught:
                call()
            self.assertEqual(str(caught.exception), f"{name}: tui mode only")
        session.close()
        self.assertFalse(session.alive())


class TuiStartTest(HostedTuiBase):
    def test_a_start_builds_only_its_client_once_in_its_harness(self):
        """The harness (here a thread, tests/session/harnesses.py) builds the client it runs once;
        the caller's checks build their own.
        """
        built, load = [], self.session_dependencies["class_loader"]

        def counted(path, base):
            found = load(path, base)
            if base is not Client:
                return found

            class Counted(found):
                def __init__(self, config):
                    if threading.current_thread().name.startswith("harness "):
                        built.append(path)
                    super().__init__(config)

            return Counted

        self.session_dependencies = {**self.session_dependencies, "class_loader": counted}
        self.configure(tui=True)
        for start in (lambda: self.drain(self.start()), lambda: self.tui(name="w1")):
            with self.subTest(start=start):
                built.clear()
                start()
                self.assertEqual(built, ["ensemble.session.clients.fake:FakeClient"])


class SessionEnvTest(EnvBase):
    def test_a_headless_session_gets_it_and_its_usage_probe_does_not(self):
        self.run_once(client="env", spec=self.spec(access=self.access))
        with open(self.log, encoding="utf-8") as f:
            (entry,) = [json.loads(line) for line in f]
        self.assertEqual(entry["env"]["PYTHONPATH"], PYTHONPATH)
        self.assertEqual(self.session.usage("env"), Usage(Window(7.0, None), None))
        self.assertEqual(self.probe.call_args.kwargs["env"]["PYTHONPATH"], "/parent")


class ChecksTest(Base):
    def test_headless_flags_and_env_reach_the_client_and_child(self):
        LAUNCHED.clear()
        spec = self.spec(flags=("--custom", "value"), env={"FAKE_ENV": "spec"})
        session, _ = self.run_once(client="env", spec=spec)
        self.assertEqual(
            (LAUNCHED[0].flags, dict(LAUNCHED[0].env), LAUNCHED[0].sid),
            (spec.flags, dict(spec.env), session.sid),
        )
        with open(self.log, encoding="utf-8") as f:
            self.assertEqual(json.loads(f.readline())["env"]["FAKE_ENV"], "spec")


class SidTest(Base):
    def test_sid_names_a_new_or_resumed_session_and_reaches_launch_neither_picks_a_uuid4(self):
        for kwargs in ({"sid": "r1", "resume": True}, {"sid": "s1"}, {}):
            with self.subTest(**kwargs):
                with contextlib.suppress(FileNotFoundError):
                    os.remove(self.log)
                session, argv = self.run_once(**kwargs)
                sid = kwargs.get("sid", session.sid)
                self.assertEqual((session.sid, session.info().sid), (sid, sid))
                self.assertEqual("--resume" in argv, kwargs.get("resume", False))
                self.assertEqual(argv[argv.index("--sid") + 1], sid)
                self.assertEqual(self.written()[-1]["sid"], sid)
        self.assertEqual(uuid.UUID(sid).version, 4)
        self.assertEqual(str(uuid.UUID(sid)), sid)


class ClientNoteTest(Base):
    def test_the_note_follows_the_prompt_only_with_commands_and_a_nonempty_note(self):
        git = Access(cwd=self.workdir, commands=("git",))
        for case, kwargs, prompt in (
            ("with commands", {"spec": self.spec(access=git)}, "hello\n\nFAKE NOTE"),
            ("without commands", {}, "hello"),
            ("an empty note", {"client": "quiet", "spec": self.spec(access=git)}, "hello"),
        ):
            with self.subTest(case=case):
                with contextlib.suppress(FileNotFoundError):
                    os.remove(self.log)
                _, argv = self.run_once(**kwargs)
                self.assertEqual(argv[-1], prompt)


if __name__ == "__main__":
    unittest.main()
