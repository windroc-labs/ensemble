"""HostedSession over fake clients (in-process module tables): events, idle timeouts, exits, stop,
process groups, inherited pipes, launch environment, limits and resume.
"""

import contextlib
import dataclasses
import io
import json
import os
import re
import signal
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from ensemble.session import Session
from ensemble.session.engine.records import RunLog
from ensemble.session.engine.types import (
    Access,
    Activity,
    Exited,
    IdleTimeout,
    Launch,
    LimitHit,
    Mode,
    Reply,
    Spec,
    Stderr,
    TurnEnd,
    TurnFailed,
)
from tests.session import fake_client
from tests.session.case import Case
from tests.session.modules import client_loader
from tests.session.threads import drain
from tests.utils.jsonl import read_jsonl
from tests.utils.sandbox import kill, scratch
from tests.utils.sandbox import pid_exists as running

WAIT = 20  # s, bound on any wait
IDLE = 0.3  # s, idle_timeout
SILENCE = 0.9  # 3 × IDLE
GAP = 0.2  # s between stderr lines, under a 3 × GAP idle_timeout
STAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}[+-]\d\d:\d\d\Z")


class LaunchingClient(fake_client.FakeClient):
    """The fake client whose launch takes env, unset and cwd from its config."""

    keys = fake_client.FakeClient.keys | {"env", "unset", "cwd"}

    def headless(self):
        return LaunchingRun(self)


class LaunchingRun(fake_client.FakeHeadless):
    def launch(self, spec: Spec):
        config = self.owner.config
        return dataclasses.replace(
            super().launch(spec),
            env=config.get("env", {}),
            unset=tuple(config.get("unset", ())),
            cwd=config.get("cwd", ""),
        )


LAUNCHING = types.ModuleType("ensemble.session.clients.launching")
LAUNCHING.LaunchingClient = LaunchingClient


GRANDCHILD = """import json, subprocess, sys, time
grandchild = subprocess.Popen(
    [sys.executable, "-I", "-c", "import time; time.sleep(60)"]
)
print(json.dumps({"type": "message", "text": str(grandchild.pid)}), flush=True)
time.sleep(60)
"""
CLOSED_PIPES = """import json, os, time
print(json.dumps({"type": "activity", "name": "Read"}), flush=True)
os.close(1)
os.close(2)
time.sleep(60)
"""


class ScriptClient(fake_client.FakeClient):
    """The fake client running GRANDCHILD (config `script` "grandchild"), else CLOSED_PIPES."""

    keys = fake_client.FakeClient.keys | {"script"}

    def headless(self):
        return ScriptRun(self)


class ScriptRun(fake_client.FakeHeadless):
    def launch(self, spec: Spec):
        if self.owner.config["script"] == "grandchild":
            return Launch((sys.executable, "-I", "-c", GRANDCHILD))
        return Launch((sys.executable, "-I", "-c", CLOSED_PIPES))


SCRIPT = types.ModuleType("ensemble.session.clients.script")
SCRIPT.ScriptClient = ScriptClient


def out(kind, **fields) -> dict:
    """A scenario step printing record {"type": kind, **fields}."""
    return {"out": {"type": kind, **fields}}


class Base(Case):
    def setUp(self):
        self.dir = scratch(self)
        self.workdir = os.path.join(self.dir, "work")
        self.scenario = os.path.join(self.dir, "scenario.json")
        self.log = os.path.join(self.dir, "client.log")
        self.gate = os.path.join(self.dir, "gate")
        self.steps()
        self.session_dependencies = {
            "class_loader": client_loader(
                {
                    "ensemble.session.clients.fake": fake_client,
                    "ensemble.session.clients.launching": LAUNCHING,
                    "ensemble.session.clients.script": SCRIPT,
                }
            )
        }
        self.configure()

    def steps(self, *steps):
        with open(self.scenario, "w", encoding="utf-8") as f:
            json.dump(list(steps), f)

    def configure(self, **extra):
        """Tables fake and launching on this test's scenario and log, `extra` in launching's."""
        tables = {
            name: {
                "implementation": f"ensemble.session.clients.{name}:{name.capitalize()}Client",
                "scenario": self.scenario,
                "log": self.log,
            }
            for name in ("fake", "launching")
        }
        tables["launching"].update(extra)
        self.configure_session(tables)

    def start(self, client="fake", spec=None, *, idle_timeout=None, **fields) -> Session:
        """A headless session of `client` on prompt hello, `spec` (default in workdir) + `fields`."""
        spec = dataclasses.replace(
            spec or Spec(workdir=self.workdir, access=Access(cwd=self.workdir)),
            client=client,
            mode=Mode.HEADLESS,
            prompt="hello",
            **fields,
        )
        handle = self.session_cls.start(spec, idle_timeout=idle_timeout)
        self.addCleanup(handle.stop)
        return handle

    def logged(self) -> list[dict]:
        return read_jsonl(self.log)

    def forget(self):
        """Drops the client log."""
        if os.path.exists(self.log):
            os.unlink(self.log)

    def written(self) -> list[dict]:
        return RunLog(self.workdir).read()


class AcceptanceTest(Base):
    def test_streams_success_events_to_exited_and_records_the_session(self):
        marker = "[ensemble-progress:start] x"
        self.steps(
            out("session", id="native-1"),
            out("activity", name="Read"),
            out("message", text="one"),
            out("message", text=marker),
            out("message", text="three"),
            out("result", text="done", pending=2),
            {"err": "warning: slow"},
            {"exit": 0},
        )
        handle = self.start()
        replies = ["one", marker, "three", "done"]
        self.assertIn(handle.reply, [None, *replies])  # the harness reads ahead
        seen = []

        def bindings() -> int:
            return [e["kind"] for e in self.written()].count("client_session")

        found = drain(self, handle, lambda event: seen.append((event, handle.reply, bindings())))
        self.assertEqual(
            [e for e in found if not isinstance(e, Stderr)],
            [
                Activity("Read"),
                Reply("one"),
                Reply(marker),
                Reply("three"),
                TurnEnd("done", 2),
                Exited(0),
            ],
        )
        self.assertEqual(found.count(Stderr("warning: slow")), 1)
        self.assertEqual(found[-1], Exited(0))
        for event, reply, bound in seen:
            if isinstance(event, Reply):
                self.assertIn(reply, replies[replies.index(event.text) :])
                self.assertEqual(bound, 1)
        self.assertEqual(handle.reply, "done")

        written = self.written()
        for entry in written:
            self.assertRegex(entry["ts"], STAMP)
        bare = [{k: v for k, v in e.items() if k != "ts"} for e in written]
        self.assertEqual(
            bare[0],
            {
                "kind": "session",
                "sid": handle.sid,
                "cwd": self.workdir,
                "project": False,
                "transcript": os.path.join(self.workdir, f"fake-{handle.sid}.jsonl"),
                "resume": f"fake --resume {handle.sid}",
            },
        )
        self.assertCountEqual(
            bare[1:],
            [
                {"kind": "client_session", "sid": handle.sid, "client_id": "native-1"},
                {"kind": "stderr", "text": "warning: slow"},
            ],
        )


class IdleTest(Base):
    def test_silence_yields_idle_timeouts_while_the_process_lives_then_exited(self):
        release = os.path.join(self.dir, "release")
        self.steps(out("activity", name="Read"), {"until": release})
        handle = self.start(idle_timeout=IDLE)
        marks, alive = [], []

        def on(event):
            marks.append((time.monotonic(), event))
            if isinstance(event, IdleTimeout) and len(alive) < 2:
                alive.append(running(self.logged()[0]["pid"]))
                if len(alive) == 2:
                    open(release, "w").close()

        found = drain(self, handle, on)
        self.assertEqual((found[0], found[-1]), (Activity("Read"), Exited(0)))
        idles = [(at, e) for at, e in marks if isinstance(e, IdleTimeout)]
        self.assertGreaterEqual(len(idles), 2)
        self.assertEqual(found[-2], TurnFailed("incomplete_turn"))
        self.assertEqual(len(found), len(idles) + 3)
        self.assertEqual({e for _, e in idles}, {IdleTimeout(IDLE)})
        self.assertEqual(alive, [True, True])
        times = [marks[0][0]] + [at for at, _ in idles]
        for before, after in zip(times, times[1:], strict=False):
            self.assertGreaterEqual(after - before, IDLE - 0.01)

    def test_a_headless_permission_request_fails_without_waiting_for_an_answer(self):
        self.steps(
            out("blocked", reason="approve"),
            {"sleep": 60},
        )
        began = time.monotonic()
        handle = self.start(idle_timeout=IDLE)
        # its harness ends the client only after logging the failure: start-up stays out of IDLE
        while not (client := read_jsonl(self.log, missing_ok=True)) or running(client[0]["pid"]):
            self.assertLess(time.monotonic() - began, WAIT)
            time.sleep(0.01)
        found = drain(self, handle)
        self.assertLess(time.monotonic() - began, WAIT)
        self.assertEqual(found[0], TurnFailed("permission_required", "approve"))
        self.assertIsInstance(found[-1], Exited)
        self.assertLess(found[-1].returncode, 0)
        self.assertFalse(any(isinstance(event, IdleTimeout | TurnEnd) for event in found))

    def test_no_idle_timeout_without_one_or_while_stderr_restarts_it(self):
        restarting = (
            {"err": "1"},
            {"sleep": GAP},
            {"err": "2"},
            {"sleep": GAP},
            {"err": "3"},
            {"sleep": GAP},
            {"err": "4"},
        )
        for case, steps, idle, want in (
            (
                "no idle timeout",
                (out("activity", name="Read"), {"sleep": SILENCE}),
                None,
                [Activity("Read"), TurnFailed("incomplete_turn"), Exited(0)],
            ),
            (
                "stderr restarts",
                restarting,
                3 * GAP,
                [
                    Stderr("1"),
                    Stderr("2"),
                    Stderr("3"),
                    Stderr("4"),
                    TurnFailed("incomplete_turn", "4"),
                    Exited(0),
                ],
            ),
        ):
            with self.subTest(case=case):
                self.steps(*steps)
                self.assertEqual(drain(self, self.start(idle_timeout=idle)), want)


class ExitTest(Base):
    def test_exited_is_the_process_exit_a_clients_own_dropped(self):
        for case, steps, want in (
            (
                "client's exited dropped",
                (out("exit"), {"exit": 0}),
                [TurnFailed("incomplete_turn"), Exited(0)],
            ),
            (
                "non-zero exit",
                (out("activity", name="Read"), {"exit": 3}),
                [Activity("Read"), TurnFailed("process_exit", "worker exited 3"), Exited(3)],
            ),
        ):
            with self.subTest(case=case):
                self.steps(*steps)
                self.assertEqual(drain(self, self.start()), want)

    def test_stop_mid_sleep_ends_with_a_negative_exited_and_is_idempotent(self):
        self.steps(out("activity", name="Read"), {"sleep": 60})
        handle = self.start()
        found = drain(
            self, handle, lambda event: handle.stop() if isinstance(event, Activity) else None
        )
        self.assertEqual(len(found), 3)
        self.assertEqual(found[0], Activity("Read"))
        self.assertEqual(found[1], TurnFailed("interrupted"))
        self.assertIsInstance(found[2], Exited)
        self.assertLess(found[2].returncode, 0)
        handle.stop()
        self.assertFalse(handle.alive())
        self.assertEqual(list(handle.events()), [])


class ProcessGroupTest(Base):
    def script(self, script: str, **kwargs) -> Session:
        """A started session of client `script`, config key `script`."""
        self.configure_session(
            {
                "script": {
                    "implementation": "ensemble.session.clients.script:ScriptClient",
                    "script": script,
                }
            }
        )
        return self.start("script", **kwargs)

    def test_stop_kills_a_grandchild_holding_the_pipes_so_events_end_with_exited(self):
        handle = self.script("grandchild")

        def on(event):
            if isinstance(event, Reply):
                self.addCleanup(kill, int(event.text))
                handle.stop()

        found = drain(self, handle, on)
        self.assertEqual(len(found), 3)
        self.assertIsInstance(found[0], Reply)
        self.assertEqual(found[1:], [TurnFailed("interrupted"), Exited(-signal.SIGHUP)])

    def test_a_process_that_closed_its_pipes_and_lives_yields_idle_timeouts_until_stop_ends_it(
        self,
    ):
        handle = self.script("closed pipes", idle_timeout=IDLE)
        found = drain(
            self, handle, lambda event: handle.stop() if isinstance(event, IdleTimeout) else None
        )
        self.assertEqual(
            found,
            [
                Activity("Read"),
                IdleTimeout(IDLE),
                TurnFailed("interrupted"),
                Exited(-signal.SIGHUP),
            ],
        )


class FailureTest(Base):
    def test_a_raising_parse_stops_the_process_and_fails_its_harness(self):
        self.steps(out("session"), {"sleep": 60})
        with self.assertLogs("session", "ERROR") as logs:
            handle = self.start()
            found = drain(self, handle)
        self.assertIn("KeyError: 'id'", logs.output[0])
        self.assertEqual(found[0], TurnFailed("interrupted"))
        self.assertIsInstance(found[-1], Exited)
        self.assertFalse(running(self.logged()[0]["pid"]))
        self.assertFalse(handle.alive())
        self.assertEqual(list(handle.events()), [])

    def test_a_failed_client_session_line_stops_the_process_and_fails_its_harness(self):
        self.steps({"until": self.gate}, out("session", id="native-1"), {"sleep": 60})
        with self.assertLogs("session", "ERROR") as logs:
            handle = self.start()
            self.unwritable()
            found = drain(self, handle)
        self.assertIn("IsADirectoryError", logs.output[0])
        self.assertEqual(found[0], TurnFailed("interrupted"))
        self.assertFalse(running(self.logged()[0]["pid"]))

    def test_a_failed_stderr_record_loses_the_line_not_the_run(self):
        self.steps({"until": self.gate}, {"err": "lost"})
        handle = self.start()
        self.unwritable()
        with mock.patch("sys.stderr", io.StringIO()) as printed:
            self.assertEqual(
                drain(self, handle),
                [Stderr("lost"), TurnFailed("incomplete_turn", "lost"), Exited(0)],
            )
        self.assertIn("records: ", printed.getvalue())

    def unwritable(self) -> None:
        """A directory in place of the run log, so a record write fails; then opens the gate."""
        path = RunLog(self.workdir).path
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        os.mkdir(path)
        Path(self.gate).touch()


class LaunchEnvironmentTest(Base):
    def test_unset_then_env_apply_to_the_callers_environment(self):
        for extra, shown in (
            ({}, "caller"),
            ({"unset": ["FAKE_ENV"]}, None),
            ({"unset": ["FAKE_ENV"], "env": {"FAKE_ENV": "launch"}}, "launch"),
        ):
            with self.subTest(extra=extra), mock.patch.dict(os.environ, {"FAKE_ENV": "caller"}):
                self.forget()
                self.configure(**extra)
                self.assertEqual(
                    drain(self, self.start("launching")), [TurnFailed("incomplete_turn"), Exited(0)]
                )
                self.assertEqual(self.logged()[0]["env"]["FAKE_ENV"], shown)

    def test_the_child_runs_in_cwd_with_pwd_set(self):
        cwd = os.path.join(self.dir, "cwd")
        os.mkdir(cwd)
        other = os.path.join(self.dir, "other")
        os.mkdir(other)
        for client, extra, access, want in (
            ("launching", {"cwd": cwd}, Access(cwd=other), cwd),
            ("fake", {}, Access(cwd=other), other),
        ):
            with (
                self.subTest(client=client, want=want),
                mock.patch.dict(os.environ, {"PWD": "/"}),
            ):
                self.forget()
                self.configure(**extra)
                handle = self.start(client, Spec(workdir=self.workdir, access=access))
                self.assertEqual(drain(self, handle), [TurnFailed("incomplete_turn"), Exited(0)])
                (entry,) = self.logged()
                self.assertEqual((entry["cwd"], entry["env"]["PWD"]), (want, want))
                self.assertEqual(handle.info().cwd, want)
                self.assertEqual(self.written()[-1]["cwd"], want)


class FakeTest(Base):
    def scenario_object(self, **scenario):
        with open(self.scenario, "w", encoding="utf-8") as f:
            json.dump(scenario, f)

    def test_a_limit_record_is_a_limit_hit(self):
        self.steps(out("limit", resets_at=1.5), out("limit", resets_at=None), out("limit"))
        self.assertEqual(
            drain(self, self.start()),
            [LimitHit(1.5), LimitHit(None), LimitHit(None), TurnFailed("usage_limit"), Exited(0)],
        )

    def test_resume_steps_run_only_on_a_resume(self):
        self.scenario_object(
            steps=[out("message", text="first")], resume=[out("message", text="again")]
        )
        first = self.start()
        self.assertEqual(
            drain(self, first), [Reply("first"), TurnFailed("incomplete_turn"), Exited(0)]
        )
        self.assertEqual(
            drain(self, self.start(sid=first.sid, resume=True)),
            [Reply("again"), TurnFailed("incomplete_turn"), Exited(0)],
        )
        self.assertEqual(["--resume" in entry["argv"] for entry in self.logged()], [False, True])

    def test_an_object_without_resume_steps_runs_its_steps_on_a_resume(self):
        self.scenario_object(steps=[out("message", text="first")])
        self.assertEqual(
            drain(self, self.start(sid="r1", resume=True)),
            [Reply("first"), TurnFailed("incomplete_turn"), Exited(0)],
        )


if __name__ == "__main__":
    unittest.main()
