"""HostedSession over fake clients: events, idle timeouts, exits, stop, process groups, inherited
pipes, launch environment, limits and resume. Custom clients: in-process module tables.
"""

import contextlib
import dataclasses
import io
import json
import os
import re
import signal
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

from ensemble.session import Session
from ensemble.session.engine.records import RunLog
from ensemble.session.engine.types import (
    Access,
    Activity,
    Blocked,
    Exited,
    IdleTimeout,
    Launch,
    LimitHit,
    Mode,
    Reply,
    Spec,
    Stderr,
    TurnEnd,
)
from tests.session import fake_client
from tests.session.case import Case
from tests.session.modules import client_loader
from tests.session.threads import capture_errors

WAIT = 20  # upper bound on any wait, in seconds
IDLE = 0.3  # idle_timeout, in seconds
SILENCE = 0.9  # 3 × IDLE
GAP = 0.2  # between stderr lines; that test's idle_timeout is 3 × GAP
STAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}[+-]\d\d:\d\d\Z")


class LaunchingClient(fake_client.FakeClient):
    """The fake client whose launch also takes its env, unset and cwd from its config."""

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
    """The fake client whose client is this Python running GRANDCHILD if its config's `script` is
    "grandchild", else CLOSED_PIPES.
    """

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


def running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def kill(pid: int) -> None:
    """SIGKILL to a test's stray process, if it still runs."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGKILL)


class Base(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.realpath(tmp.name)
        self.workdir = os.path.join(self.dir, "work")
        self.scenario = os.path.join(self.dir, "scenario.json")
        self.log = os.path.join(self.dir, "client.log")
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
        """Tables fake and launching, each with this test's scenario and log; `extra` keys go into
        launching's.
        """
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
        """A headless session of `client` on prompt hello: `spec` (else one in self.workdir) with
        `fields`.
        """
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

    def drain(self, handle, on=lambda _event: None) -> list:
        """handle.events() to its end, each event passed to on(event) as it comes; fails, stopping
        the handle, instead of hanging past WAIT.
        """
        found, errors = [], []

        def run():
            for event in handle.events():
                found.append(event)
                on(event)

        thread = threading.Thread(target=run, daemon=True)
        with capture_errors(thread, errors.append):
            thread.start()
            thread.join(WAIT)
        if thread.is_alive():
            handle.stop()
            self.fail(f"events() did not end within {WAIT}s, got {found}")
        if errors:
            raise errors[0]
        return found

    def logged(self) -> list[dict]:
        with open(self.log, encoding="utf-8") as f:
            return [json.loads(line) for line in f]

    def forget(self):
        """Drops the client's log."""
        if os.path.exists(self.log):
            os.unlink(self.log)

    def written(self) -> list[dict]:
        return RunLog(self.workdir).read()


class AcceptanceTest(Base):
    def test_streams_the_frozen_event_set_to_exited_and_records_the_session(self):
        marker = "[ensemble-progress:start] x"
        self.steps(
            out("session", id="native-1"),
            out("activity", name="Read"),
            out("message", text="one"),
            out("message", text=marker),
            out("message", text="three"),
            out("blocked", reason="approve"),
            out("result", text="done", pending=2),
            {"err": "warning: slow"},
            {"exit": 0},
        )
        handle = self.start()
        self.assertIsNone(handle.reply)
        seen = []

        def bindings() -> int:
            return [e["kind"] for e in self.written()].count("client_session")

        found = self.drain(handle, lambda event: seen.append((event, handle.reply, bindings())))
        self.assertEqual(
            [e for e in found if not isinstance(e, Stderr)],
            [
                Activity("Read"),
                Reply("one"),
                Reply(marker),
                Reply("three"),
                Blocked("approve"),
                TurnEnd("done", 2),
                Exited(0),
            ],
        )
        self.assertEqual(found.count(Stderr("warning: slow")), 1)
        self.assertEqual(found[-1], Exited(0))
        for event, reply, bound in seen:
            if isinstance(event, Reply):
                self.assertEqual((reply, bound), (event.text, 1))
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
        self.steps(out("activity", name="Read"), {"sleep": SILENCE})
        handle = self.start(idle_timeout=IDLE)
        marks, alive = [], []

        def on(event):
            marks.append((time.monotonic(), event))
            if isinstance(event, IdleTimeout):
                alive.append(running(self.logged()[0]["pid"]))

        found = self.drain(handle, on)
        self.assertEqual((found[0], found[-1]), (Activity("Read"), Exited(0)))
        idles = [(at, e) for at, e in marks if isinstance(e, IdleTimeout)]
        self.assertGreaterEqual(len(idles), 1)
        self.assertEqual(len(found), len(idles) + 2)
        self.assertEqual({e for _, e in idles}, {IdleTimeout(IDLE)})
        self.assertEqual(alive, [True] * len(idles))
        times = [marks[0][0]] + [at for at, _ in idles]
        for before, after in zip(times, times[1:], strict=False):
            self.assertGreaterEqual(after - before, IDLE - 0.01)

    def test_a_stderr_line_after_blocked_starts_no_timer(self):
        self.steps(
            out("blocked", reason="approve"),
            {"sleep": 0.1},
            {"err": "still waiting"},
            {"sleep": SILENCE},
        )
        found = self.drain(self.start(idle_timeout=IDLE))
        self.assertCountEqual(found[:-1], [Blocked("approve"), Stderr("still waiting")])
        self.assertEqual(found[-1], Exited(0))

    def test_no_idle_timeout_after_blocked_without_one_or_while_stderr_restarts_it(self):
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
                "after blocked",
                (out("blocked", reason="approve"), {"sleep": SILENCE}),
                IDLE,
                [Blocked("approve"), Exited(0)],
            ),
            (
                "no idle timeout",
                (out("activity", name="Read"), {"sleep": SILENCE}),
                None,
                [Activity("Read"), Exited(0)],
            ),
            (
                "stderr restarts",
                restarting,
                3 * GAP,
                [Stderr("1"), Stderr("2"), Stderr("3"), Stderr("4"), Exited(0)],
            ),
        ):
            with self.subTest(case=case):
                self.steps(*steps)
                self.assertEqual(self.drain(self.start(idle_timeout=idle)), want)


class ExitTest(Base):
    def test_exited_is_the_process_exit_a_clients_own_dropped(self):
        for case, steps, want in (
            ("client's exited dropped", (out("exit"), {"exit": 0}), [Exited(0)]),
            (
                "non-zero exit",
                (out("activity", name="Read"), {"exit": 3}),
                [Activity("Read"), Exited(3)],
            ),
        ):
            with self.subTest(case=case):
                self.steps(*steps)
                self.assertEqual(self.drain(self.start()), want)

    def test_stop_mid_sleep_ends_with_a_negative_exited_and_is_idempotent(self):
        self.steps(out("activity", name="Read"), {"sleep": 60})
        handle = self.start()
        found = self.drain(
            handle, lambda event: handle.stop() if isinstance(event, Activity) else None
        )
        self.assertEqual(len(found), 2)
        self.assertEqual(found[0], Activity("Read"))
        self.assertIsInstance(found[1], Exited)
        self.assertLess(found[1].returncode, 0)
        handle.stop()
        self.assertFalse(handle.alive())
        self.assertEqual(list(handle.events()), [])


class ProcessGroupTest(Base):
    def script(self, script: str, **kwargs) -> Session:
        """A started session of client `script` with config key `script`."""
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

        found = self.drain(handle, on)
        self.assertEqual(len(found), 2)
        self.assertIsInstance(found[0], Reply)
        self.assertEqual(found[1], Exited(-signal.SIGKILL))

    def test_a_process_that_closed_its_pipes_and_lives_yields_idle_timeouts_until_stop_ends_it(
        self,
    ):
        handle = self.script("closed pipes", idle_timeout=IDLE)
        found = self.drain(
            handle, lambda event: handle.stop() if isinstance(event, IdleTimeout) else None
        )
        self.assertEqual(found, [Activity("Read"), IdleTimeout(IDLE), Exited(-signal.SIGKILL)])


class FailureTest(Base):
    def test_a_raising_parse_stops_the_process_and_propagates(self):
        self.steps(out("session"), {"sleep": 60})
        handle = self.start()
        with self.assertRaises(KeyError):
            self.drain(handle)
        self.assertFalse(running(self.logged()[0]["pid"]))
        self.assertFalse(handle.alive())
        self.assertEqual(list(handle.events()), [])

    def test_a_failed_client_session_line_stops_the_process_and_propagates(self):
        self.steps(out("session", id="native-1"), {"sleep": 60})
        handle = self.start()
        self.unwritable()
        with self.assertRaises(OSError):
            self.drain(handle)
        self.assertFalse(running(self.logged()[0]["pid"]))

    def test_a_failed_stderr_record_loses_the_line_not_the_run(self):
        self.steps({"err": "lost"})
        handle = self.start()
        self.unwritable()
        with mock.patch("sys.stderr", io.StringIO()) as printed:
            self.assertEqual(self.drain(handle), [Stderr("lost"), Exited(0)])
        self.assertIn("records: ", printed.getvalue())

    def unwritable(self) -> None:
        """A directory in place of the run log, which a record write refuses."""
        path = RunLog(self.workdir).path
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        os.mkdir(path)


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
                self.assertEqual(self.drain(self.start("launching")), [Exited(0)])
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
                self.assertEqual(self.drain(handle), [Exited(0)])
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
            self.drain(self.start()), [LimitHit(1.5), LimitHit(None), LimitHit(None), Exited(0)]
        )

    def test_resume_steps_run_only_on_a_resume(self):
        self.scenario_object(
            steps=[out("message", text="first")], resume=[out("message", text="again")]
        )
        first = self.start()
        self.assertEqual(self.drain(first), [Reply("first"), Exited(0)])
        self.assertEqual(
            self.drain(self.start(sid=first.sid, resume=True)), [Reply("again"), Exited(0)]
        )
        self.assertEqual(["--resume" in entry["argv"] for entry in self.logged()], [False, True])

    def test_an_object_without_resume_steps_runs_its_steps_on_a_resume(self):
        self.scenario_object(steps=[out("message", text="first")])
        self.assertEqual(self.drain(self.start(sid="r1", resume=True)), [Reply("first"), Exited(0)])


if __name__ == "__main__":
    unittest.main()
