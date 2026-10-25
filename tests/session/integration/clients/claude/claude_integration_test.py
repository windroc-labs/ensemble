"""HostedSession with client claude end to end: the real ClaudeClient, its [clients.claude] table
given to a configured session class, runs tests/session/integration/clients/claude/fake_claude.py
(on PATH as `claude`) through start, usage and inspect; one case runs the session in a caller
process of its own (CALLER) to interrupt it. HOME and CLAUDE_CONFIG_DIR are temp dirs.
"""

import contextlib
import dataclasses
import io
import json
import os
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from ensemble.session import Session
from ensemble.session.engine.accounts import TTL
from ensemble.session.engine.records import RunLog
from ensemble.session.engine.types import (
    Access,
    Activity,
    ConfigError,
    Exited,
    IdleTimeout,
    LimitHit,
    Mode,
    Reply,
    SessionInfo,
    SessionStatus,
    Spec,
    Stderr,
    TurnEnd,
    Window,
)
from ensemble.utils import exhausted
from tests.session.case import PLUGIN_BIN, PY, Case, child_env
from tests.session.fakes.container_host import PATH as CONTAINER_HOST
from tests.session.integration.clients.claude import fake_claude
from tests.utils.sandbox import git

WAIT = 20  # upper bound on any wait, in seconds
IDLE = 0.5  # idle_timeout, in seconds
SETTLE = 0.5  # seconds a caller gets to wait in events() again after printing an event
SID = "0f8fad5b-d9cb-469f-a165-70867728950e"
RESETS = 1760000000
TABLE = {
    "implementation": "ensemble.session.clients.claude:ClaudeClient",
    "tiers": {"4": "haiku"},
    "efforts": {"low": "low"},
}
PROBE = [
    "-p",
    "--no-session-persistence",
    "--strict-mcp-config",
    "--setting-sources",
    "user",
    "--settings",
    '{"remoteControlAtStartup":false,"disableAllHooks":true}',
    "/usage",
]
USAGE = (
    "Current session: 25% used · resets 4:39am (America/New_York)\n"
    "Current week (all models): 100% used\n"
)
# A caller: argv[1] is the claude table, argv[2] the workdir. It prints each event's name; on a
# KeyboardInterrupt (SIGINT, even when inherited ignored) it prints that and stays alive, the fake's
# parent.
CALLER = """import json, signal, sys, time
from ensemble.session import Access, Host, Mode, Session, Spec
signal.signal(signal.SIGINT, signal.default_int_handler)
Session.configure(clients={"claude": json.loads(sys.argv[1])}, hosts=Host.parse_hosts({"default": "tmux", "tmux": {"implementation": "ensemble.session.hosts.tmux:TmuxHost"}}))
handle = Session.start(Spec(workdir=sys.argv[2], access=Access(cwd=sys.argv[2]), client="claude",
                            mode=Mode.HEADLESS, prompt="hello"))
try:
    for event in handle.events():
        print(type(event).__name__, flush=True)
except KeyboardInterrupt:
    print("KeyboardInterrupt", flush=True)
    time.sleep(60)
"""


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
        self.file = os.path.join(self.dir, "scenario.json")
        self.log = os.path.join(self.dir, "claude.log")
        bin_dir = os.path.join(self.dir, "bin")
        os.mkdir(bin_dir)
        patch = mock.patch.dict(
            os.environ,
            {
                "PATH": fake_claude.install(bin_dir),
                "CLAUDE_CONFIG_DIR": os.path.join(self.dir, "config"),
                fake_claude.ENV: self.file,
            },
        )
        patch.start()
        self.addCleanup(patch.stop)
        self.configure_session({"claude": TABLE})
        self.scenario()

    def scenario(self, **fields):
        with open(self.file, "w", encoding="utf-8") as f:
            json.dump({"log": self.log, **fields}, f)

    def start(self, prompt="hello", spec=None, *, idle_timeout=None, **fields) -> Session:
        """A headless claude session on `prompt`: `spec` (else one in self.workdir) with `fields`."""
        spec = dataclasses.replace(
            spec or Spec(workdir=self.workdir, access=Access(cwd=self.workdir)),
            client="claude",
            mode=Mode.HEADLESS,
            prompt=prompt,
            **fields,
        )
        handle = self.session_cls.start(spec, idle_timeout=idle_timeout)
        self.addCleanup(handle.stop)
        return handle

    def inspect(self, sid: str) -> SessionStatus:
        return self.session_cls.inspect(
            Spec(workdir=self.workdir, access=Access(cwd=self.workdir), client="claude", sid=sid)
        )

    def drain(self, handle, on=lambda _event: None) -> list:
        """handle.events() to its end, each event passed to on(event) as it comes; fails, stopping
        the handle, instead of hanging past WAIT.
        """
        self.addCleanup(handle.stop)
        found = []

        def consume():
            for event in handle.events():
                found.append(event)
                on(event)

        executor = ThreadPoolExecutor(max_workers=1)
        result = executor.submit(consume)
        try:
            result.result(timeout=WAIT)
        except TimeoutError:
            handle.stop()
            self.fail(f"events() did not end within {WAIT}s, got {found}")
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        return found

    def logged(self) -> list[dict]:
        with open(self.log, encoding="utf-8") as f:
            return [json.loads(line) for line in f]

    def written(self) -> list[dict]:
        return [{k: v for k, v in e.items() if k != "ts"} for e in RunLog(self.workdir).read()]

    def resume_command(self, sid: str, cwd: str | None = None) -> str:
        cwd = shlex.quote(cwd or self.workdir)
        store = shlex.quote(os.path.realpath(os.path.expanduser("~/.ensemble/sessions/claude")))
        return f"cd {cwd} && ensemble-session client-resume claude {store} {cwd} {sid}"


class RunTest(Base):
    def test_start_and_resume_git_select_the_run_checkout_not_the_parent_environment(self):
        deployment = os.path.join(self.dir, "deployment")
        project = os.path.join(self.dir, "project")
        os.mkdir(deployment)
        os.mkdir(project)
        git("init", deployment)
        git("init", project)
        self.scenario(steps=[{"kind": "git"}])
        for cwd, want in (
            (project, {"returncode": 0, "stdout": project}),
            (self.workdir, {"returncode": 128, "stdout": ""}),
        ):
            with (
                self.subTest(cwd=cwd),
                mock.patch.dict(
                    os.environ,
                    {"GIT_DIR": os.path.join(deployment, ".git"), "GIT_WORK_TREE": deployment},
                ),
            ):
                spec = Spec(workdir=self.workdir, access=Access(cwd=cwd))
                first = self.start(spec=spec)
                self.assertEqual(self.drain(first)[-1], Exited(0))
                again = self.start("more", spec=spec, sid=first.sid, resume=True)
                self.assertEqual(self.drain(again)[-1], Exited(0))
                self.assertEqual(json.loads(first.reply), want)
                self.assertEqual(json.loads(again.reply), want)

    def test_start_and_resume_exclude_project_sources_unless_access_enables_them(self):
        for project, sources in ((False, "user"), (True, "user,project,local")):
            with self.subTest(project=project):
                spec = Spec(workdir=self.workdir, access=Access(cwd=self.workdir, project=project))
                first = self.start(spec=spec)
                self.assertEqual(self.drain(first)[-1], Exited(0))
                again = self.start("more", spec=spec, sid=first.sid, resume=True)
                self.assertEqual(self.drain(again)[-1], Exited(0))
                for entry in self.logged()[-2:]:
                    argv = entry["argv"]
                    self.assertEqual(argv[argv.index("--setting-sources") + 1], sources)

    def test_a_run_streams_its_messages_to_exited_and_records_the_session(self):
        self.scenario(
            steps=["one", {"kind": "tool", "name": "Read"}, "two", "three"],
            rate_limit={"status": "allowed", "unifiedWindows": {"five_hour": {"utilization": 0.1}}},
        )
        handle = self.start(
            spec=Spec(workdir=self.workdir, access=Access(cwd=self.workdir), tier=4, effort="low")
        )
        sid = handle.sid
        self.assertEqual(
            self.drain(handle),
            [
                Reply("one"),
                Activity("Read"),
                Reply("two"),
                Reply("three"),
                TurnEnd("three", 0),
                Exited(0),
            ],
        )
        self.assertEqual(handle.reply, "three")
        (entry,) = self.logged()
        self.assertEqual(entry["cwd"], self.workdir)
        self.assertEqual(
            entry["argv"][:6], ["-p", "--session-id", sid, "--model", "haiku", "--effort"]
        )
        self.assertEqual(entry["argv"][-2:], ["--", "hello"])

        transcript = handle.info().transcript
        self.assertTrue(os.path.isfile(transcript))
        session = {
            "kind": "session",
            "sid": sid,
            "cwd": self.workdir,
            "project": False,
            "transcript": transcript,
            "resume": self.resume_command(sid),
        }
        self.assertEqual(
            self.written(), [session, {"kind": "client_session", "sid": sid, "client_id": sid}]
        )
        self.assertEqual(
            RunLog(self.workdir).session(sid),
            SessionInfo(**{k: v for k, v in session.items() if k != "kind"}),
        )

    def test_resume_continues_the_transcript(self):
        self.scenario(steps=["first"])
        first = self.start()
        self.assertEqual(self.drain(first)[-1], Exited(0))
        self.scenario(steps=["second"])
        again = self.start("more", sid=first.sid, resume=True)
        self.assertEqual(again.sid, first.sid)
        self.assertEqual(
            self.drain(again),
            [Reply("second"), TurnEnd("second", 0), Exited(0)],
        )
        self.assertEqual(self.logged()[1]["argv"][:3], ["-p", "--resume", first.sid])
        self.assertNotIn("--session-id", self.logged()[1]["argv"])
        self.assertEqual(again.info().transcript, first.info().transcript)
        with open(again.info().transcript, encoding="utf-8") as f:
            turns = [(r["type"], r["message"]["content"]) for r in map(json.loads, f)]
        self.assertEqual(
            turns,
            [
                ("user", "hello"),
                ("assistant", [{"type": "text", "text": "first"}]),
                ("user", "more"),
                ("assistant", [{"type": "text", "text": "second"}]),
            ],
        )
        self.assertEqual([e["kind"] for e in self.written()], ["session", "client_session"] * 2)

    def test_a_run_in_its_own_cwd_is_recorded_there_and_resumes_there(self):
        proj = os.path.join(self.dir, "proj")
        os.mkdir(proj)
        access = Access(cwd=proj, dirs=(self.workdir,))
        first = self.start(spec=Spec(workdir=self.workdir, access=access))
        sid, transcript = first.sid, first.info().transcript
        self.assertEqual(self.drain(first)[-1], Exited(0))
        self.assertTrue(os.path.isfile(transcript))
        self.assertEqual(os.listdir(self.workdir), ["run.jsonl"])
        resume = self.resume_command(sid, proj)
        self.assertEqual(
            RunLog(self.workdir).session(sid),
            SessionInfo(sid=sid, cwd=proj, project=False, transcript=transcript, resume=resume),
        )

        again = self.start(
            "more",
            Spec(workdir=self.workdir, access=Access(cwd=proj)),
            sid=sid,
            resume=True,
        )
        self.assertEqual(self.drain(again)[-1], Exited(0))
        calls = self.logged()
        self.assertEqual([call["cwd"] for call in calls], [proj, proj])
        self.assertEqual(calls[1]["argv"][:3], ["-p", "--resume", sid])
        self.assertNotIn("--session-id", calls[1]["argv"])
        with open(transcript, encoding="utf-8") as f:
            users = [r["message"]["content"] for r in map(json.loads, f) if r["type"] == "user"]
        self.assertEqual(users, ["hello", "more"])
        self.assertEqual([e["cwd"] for e in self.written() if e["kind"] == "session"], [proj, proj])

    def test_a_session_killed_outside_the_driver_ends_with_its_signal_and_resumes(self):
        self.scenario(steps=["first"], hang=True)
        first = self.start()
        sid = first.sid

        def on(event):
            if isinstance(event, Reply):
                os.kill(self.logged()[0]["pid"], signal.SIGKILL)

        self.assertEqual(self.drain(first, on), [Reply("first"), Exited(-signal.SIGKILL)])
        self.assertEqual(self.inspect(sid).resumable, "yes")
        self.scenario(steps=["second"])
        again = self.start("more", sid=sid, resume=True)
        self.assertEqual(self.drain(again), [Reply("second"), TurnEnd("second", 0), Exited(0)])
        self.assertEqual(self.logged()[1]["argv"][:3], ["-p", "--resume", sid])

    def test_a_caller_sid_names_the_session(self):
        handle = self.start(sid=SID)
        self.assertEqual(handle.sid, SID)
        self.assertEqual(self.drain(handle), [TurnEnd("", 0), Exited(0)])
        self.assertEqual(self.logged()[0]["argv"][:3], ["-p", "--session-id", SID])
        self.assertEqual(os.path.basename(handle.info().transcript), f"{SID}.jsonl")
        self.assertTrue(os.path.isfile(handle.info().transcript))

    def test_a_sid_that_is_no_uuid_starts_nothing(self):
        with self.assertRaisesRegex(ConfigError, "session ids are UUIDs"):
            self.start(sid="not-a-uuid")
        self.assertFalse(os.path.exists(self.log))
        self.assertEqual(self.written(), [])

    def test_a_usage_limit_is_one_limit_hit(self):
        for resets_at, want in ((RESETS, LimitHit(RESETS)), (None, LimitHit(None))):
            with self.subTest(resets_at=resets_at):
                self.scenario(steps=["working", {"kind": "limit", "resets_at": resets_at}], exit=1)
                handle = self.start()
                self.assertEqual(
                    self.drain(handle),
                    [
                        Reply("working"),
                        want,
                        Reply(fake_claude.LIMIT),
                        TurnEnd("", 0),
                        Exited(1),
                    ],
                )

    def test_a_non_zero_exit(self):
        self.scenario(steps=["partial"], exit=3)
        handle = self.start()
        self.assertEqual(
            self.drain(handle),
            [Reply("partial"), TurnEnd("", 0), Exited(3)],
        )

    def test_resuming_a_session_without_a_transcript_exits_1_and_its_stderr_is_recorded(self):
        said = f"No conversation found with session ID: {SID}"
        self.assertEqual(self.drain(self.start(sid=SID, resume=True)), [Stderr(said), Exited(1)])
        self.assertEqual([e["kind"] for e in self.written()], ["session", "stderr"])
        self.assertEqual(self.written()[1]["text"], said)

    def test_a_silent_session_yields_idle_timeouts_until_stop_ends_it(self):
        self.scenario(steps=["working"], hang=True)
        handle = self.start(idle_timeout=IDLE)
        replied = []

        def on(event):
            if isinstance(event, Reply):
                replied.append(event)
            elif isinstance(event, IdleTimeout) and replied:
                handle.stop()

        found = self.drain(handle, on)
        self.assertEqual(
            [e for e in found if not isinstance(e, IdleTimeout)],
            [Reply("working"), Exited(-signal.SIGKILL)],
        )
        self.assertEqual(found[-2], IdleTimeout(IDLE))


class InterruptTest(Base):
    def until(self, caller: subprocess.Popen, check, what: str) -> None:
        """Returns once check() holds; fails naming `what` once the caller ends first or WAIT s
        pass.
        """
        deadline = time.monotonic() + WAIT
        while not check():
            if caller.poll() is not None or time.monotonic() > deadline:
                self.fail(f"the caller never {what} (rc {caller.poll()}): {self.printed()}")
            time.sleep(0.05)

    def printed(self) -> list[str]:
        with open(self.out, encoding="utf-8") as f:
            return f.read().split()

    def test_an_interrupt_while_events_waits_kills_the_hung_session(self):
        self.scenario(steps=["working"], hang=True)
        self.out = os.path.join(self.dir, "caller.out")
        with open(self.out, "w", encoding="utf-8") as f:
            caller = subprocess.Popen(
                [*PY, "-c", CALLER, json.dumps(TABLE), self.workdir],
                env=child_env(),
                stdin=subprocess.DEVNULL,
                stdout=f,
                stderr=subprocess.STDOUT,
            )
        self.addCleanup(caller.wait)
        self.addCleanup(caller.kill)
        self.until(caller, lambda: "Reply" in self.printed(), "printed the Reply")
        (entry,) = self.logged()
        self.addCleanup(kill, entry["pid"])
        # SIGINT must land while events() waits, not in the loop body, where it would only close the
        # iterator.
        time.sleep(SETTLE)
        caller.send_signal(signal.SIGINT)
        self.until(
            caller, lambda: "KeyboardInterrupt" in self.printed(), "caught the KeyboardInterrupt"
        )
        self.assertEqual(self.printed(), ["Reply", "KeyboardInterrupt"])
        self.assertFalse(running(entry["pid"]))


class UsageTest(Base):
    def test_the_probes_usage_panel_is_the_usage(self):
        self.scenario(usage=USAGE)
        found = self.session_cls.usage("claude")
        self.assertEqual((found.five_hour.used_percent, found.weekly), (25.0, Window(100.0, None)))
        self.assertIsNotNone(found.five_hour.resets_at)
        (entry,) = self.logged()
        self.assertEqual(entry["argv"], PROBE)
        probe = os.path.join(self.ensemble, "probe")
        self.assertEqual(entry["cwd"], os.path.realpath(probe))
        self.assertEqual(stat.S_IMODE(os.lstat(probe).st_mode), 0o700)
        self.assertEqual(self.written(), [])

    def test_unknown_is_none(self):
        for name, fields in (
            ("failed run", {"exit": 1, "usage": ""}),
            ("model reply", {"usage": "Hello! How can I help with /usage?\n"}),
            ("no claude", {"usage": USAGE}),
        ):
            with (
                self.subTest(name),
                mock.patch.dict(os.environ),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.scenario(**fields)
                if name == "no claude":
                    os.environ["PATH"] = self.dir
                if name == "no claude":
                    with self.assertRaisesRegex(ConfigError, "missing or not executable"):
                        self.session_cls.usage("claude")
                else:
                    self.assertIsNone(self.session_cls.usage("claude"))


def panel(five, week):
    return f"Current session: {five}% used\nCurrent week (all models): {week}% used\n"


class AccountUsageTest(Base):
    """`default` and `work` on a Host running containers: the fake claude answers /usage by its
    CLAUDE_CONFIG_DIR (default's unset, whatever the caller's), and Session.usage is the reading of
    the account a start would pick.
    """

    def setUp(self):
        super().setUp()
        self.work = os.path.join(self.dir, "work-config")
        table = {**TABLE, "accounts": {"work": {"dir": self.work}}}
        self.configure_session({"claude": table}, {"implementation": CONTAINER_HOST})

    def usage(self, default, work, now=None):
        self.scenario(usage={"": panel(*default), self.work: panel(*work)})
        with contextlib.redirect_stderr(io.StringIO()):
            if now is None:
                return self.session_cls.usage("claude")
            with mock.patch("time.time", return_value=now):
                return self.session_cls.usage("claude")

    def probed(self) -> list[str]:
        return sorted(entry["config_dir"] for entry in self.logged())

    def test_the_account_with_more_left_is_the_usage(self):
        found = self.usage((70, 10), (20, 30))
        self.assertEqual((found.five_hour.used_percent, found.weekly.used_percent), (20.0, 30.0))
        self.assertEqual(self.probed(), ["", self.work])
        self.assertTrue(all(entry["argv"] == PROBE for entry in self.logged()))

    def test_an_account_over_a_threshold_loses(self):
        found = self.usage((92, 5), (50, 60))
        self.assertEqual((found.five_hour.used_percent, found.weekly.used_percent), (50.0, 60.0))

    def test_every_account_over_is_exhausted(self):
        self.assertTrue(exhausted(self.usage((95, 10), (10, 100))))

    def test_a_reading_within_the_ttl_runs_no_probe(self):
        now = time.time()
        first = self.usage((70, 10), (20, 30), now)
        self.assertEqual(len(self.logged()), 2)
        self.assertEqual(self.usage((10, 10), (90, 90), now + TTL - 1), first)
        self.assertEqual(len(self.logged()), 2)
        found = self.usage((10, 10), (90, 90), now + TTL)
        self.assertEqual((found.five_hour.used_percent, found.weekly.used_percent), (10.0, 10.0))
        self.assertEqual(len(self.logged()), 4)


class SessionInfoTest(Base):
    def test_a_run_session_is_resumable_another_is_not(self):
        handle = self.start(sid=SID)
        self.assertEqual(self.drain(handle)[-1], Exited(0))
        self.assertEqual(
            self.inspect(SID),
            SessionStatus(
                "yes", os.lstat(handle.info().transcript).st_mtime, self.resume_command(SID)
            ),
        )
        other = str(uuid.uuid4())
        RunLog(self.workdir).note(
            "session", sid=other, cwd=self.workdir, project=False, transcript="", resume=""
        )
        self.assertEqual(
            self.inspect(other),
            SessionStatus("no", None, self.resume_command(other)),
        )

    def test_fake_credentials_stay_out_of_public_text_and_replay_on_resume(self):
        secrets = {"CFG_TOKEN": "sk-fake-1", "ACCESS_TOKEN": "sk-fake-2", "SPEC_TOKEN": "sk-fake-3"}
        self.configure_session({"claude": {**TABLE, "env": {"CFG_TOKEN": "sk-fake-1"}}})
        spec = Spec(
            workdir=self.workdir, access=Access(cwd=self.workdir, env={"ACCESS_TOKEN": "sk-fake-2"})
        )
        handle = self.start(spec=spec, sid=SID, env={"SPEC_TOKEN": "sk-fake-3"})
        self.assertEqual(self.drain(handle)[-1], Exited(0))
        resume = handle.info().resume
        status = self.inspect(SID)
        with open(os.path.join(self.workdir, "run.jsonl"), encoding="utf-8") as f:
            log = f.read()
        for text in (resume, status.resume_command, log):
            for value in secrets.values():
                self.assertNotIn(value, text)
        self.assertEqual((resume, status.resume_command), (self.resume_command(SID),) * 2)
        record, recorder = os.path.join(self.dir, "resumed.json"), os.path.join(self.dir, "rec")
        os.mkdir(recorder)
        with open(os.path.join(recorder, "claude"), "w", encoding="utf-8") as f:
            f.write(
                f"#!{sys.executable}\nimport json, os, sys\n"
                "with open(os.environ['RESUME_RECORD'], 'w') as f:\n"
                "    json.dump({'argv': sys.argv[1:], 'cwd': os.getcwd(), 'env': dict(os.environ)}, f)\n"
            )
        os.chmod(os.path.join(recorder, "claude"), 0o700)
        path = os.pathsep.join((recorder, PLUGIN_BIN, "/usr/bin", "/bin"))
        result = subprocess.run(
            ["/bin/sh", "-c", resume],
            env={**os.environ, "PATH": path, "RESUME_RECORD": record},
            capture_output=True,
            text=True,
            timeout=WAIT,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(record, encoding="utf-8") as f:
            resumed = json.load(f)
        self.assertEqual(
            (resumed["argv"], os.path.realpath(resumed["cwd"])),
            (
                [
                    *("--resume", SID, "--permission-mode", "auto", "--setting-sources", "user"),
                    *("--settings", '{"enabledPlugins": {"ensemble@windroc": false}}'),
                ],
                self.workdir,
            ),
        )
        self.assertEqual({key: resumed["env"].get(key) for key in secrets}, secrets)


if __name__ == "__main__":
    unittest.main()
