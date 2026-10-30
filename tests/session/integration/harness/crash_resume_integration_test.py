"""Guard lifecycle, conversation recovery and worker panes shown again."""

import os
import re
import signal
import subprocess
import sys
import uuid
from pathlib import Path
from unittest import mock

from ensemble.session.engine.types import Access, HookCallback, Liveness, Spec
from ensemble.session.harness.channel import Channel
from ensemble.session.harness.view import OwnedSession
from ensemble.session.hosts.base import Host
from ensemble.session.hosts.tmux import managed
from tests.session import harnesses, live_tmux
from tests.session.case import HOSTS, REPO
from tests.session.cli_support import COMMAND, MANAGER, TIMEOUT, WorkerCase, logged

FLAGS = ("--model", "m-test")
REAPED = 10  # FR-13's bound for guard and client to end after kill -9 of the harness
RECALL = [{"kind": "recall"}]
HANDLER = "tests.session.integration.harness.recording_handler"
GATE_BUSY = "busy: lock w1 is held by previous worker generation (live or unknown)"
CLOSE_BUSY = "busy: lock w1 is held by worker generation (live or unknown)"


def ps(pid: int, field: str) -> str:
    return subprocess.run(
        ["ps", "-o", f"{field}=", "-p", str(pid)],
        capture_output=True,
        text=True,
        env={"PATH": os.defpath, "LC_ALL": "C"},
        timeout=TIMEOUT,
    ).stdout.strip()


def running(pid: int) -> bool:
    """pid runs and is no zombie."""
    found = ps(pid, "stat")
    return bool(found) and not found.startswith("Z")


def has_pair(argv: list[str], flag: str, value: str) -> bool:
    return any(argv[i : i + 2] == [flag, value] for i in range(len(argv)))


def holders(link: str) -> set[int]:
    """Every pid with an open descriptor on `link` (a /proc fd link such as pipe:[N])."""
    found = set()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            fds = os.listdir(f"/proc/{entry}/fd")
        except OSError:
            continue
        for fd in fds:
            try:
                if os.readlink(f"/proc/{entry}/fd/{fd}") == link:
                    found.add(int(entry))
            except OSError:
                continue
    return found


class CrashCase(WorkerCase):
    def launched(self, name: str = "w1", *args: str, **kw) -> dict:
        """The first done turn's worker identities."""
        record = self.record(self.start(name, *args, **kw))
        self.wait_event(name, "done")
        channel = self.owner(name)
        status = channel.status()
        call = self.calls(name)[-1]
        return {
            "record": record,
            "channel": channel,
            "harness": status["pid"],
            "generation": status["generation"],
            "guard": int(ps(call["pid"], "ppid")),
            "call": call,
        }

    def resume(
        self, name: str = "w1", steps=None, inside: str | None = None, **kw
    ) -> subprocess.CompletedProcess:
        return self.session(
            "resume", name, inside=inside, cwd=self.root, **self.scene(name, steps), **kw
        )

    def reaped(self, *pids: int) -> None:
        live_tmux.wait(lambda: not any(map(running, pids)), timeout=REAPED, what=f"{pids} to end")

    def liveness(self, name: str, generation: str) -> Liveness:
        return managed.liveness("tmux", name, generation)

    def unshown(self, name: str) -> bool:
        self.poke()
        return not self.server.pane_of(name)

    def shown_again(self, name: str, before: str) -> None:
        """A new live worker pane of `name`, not `before`, runs its attach."""
        pane = self.server.pane_of(name)
        self.assertTrue(pane and pane != before, (pane, before))
        self.assertIn("--attach", ps(int(self.pane(name, "#{pane_pid}")), "args"))
        self.assertEqual(self.pane(name, "#{pane_dead}"), "0")

    def running_harnesses(self) -> list[Channel]:
        root = os.path.join(self.ensemble, "harness")
        channels = [Channel(os.path.join(root, name)) for name in os.listdir(root)]
        return [c for c in channels if c.alive() and c.status().get("state") == "running"]


class ReclaimTest(CrashCase):
    def test_harness_sigkill_ends_guard_and_client(self):
        found = self.launched()
        self.assertIn("guard.py", ps(found["guard"], "args"))
        display = int(self.pane("w1", "#{pane_pid}"))
        self.assertIn("--attach", ps(display, "args"))
        os.kill(found["harness"], signal.SIGKILL)
        self.reaped(found["call"]["pid"], found["guard"])
        live_tmux.wait(
            lambda: self.liveness("w1", found["generation"]) == Liveness.ABSENT,
            timeout=REAPED,
            what="its generation to be absent",
        )

    def test_only_the_harness_holds_the_lifecycle_pipe(self):
        if not sys.platform.startswith("linux"):
            self.skipTest("reads /proc")
        found = self.launched()
        link = os.readlink(f"/proc/{found['guard']}/fd/0")
        self.assertTrue(link.startswith("pipe:"), link)
        self.assertEqual(holders(link), {found["guard"], found["harness"]})


class ResumeTest(CrashCase):
    def test_resume_after_harness_sigkill_answers_from_the_old_conversation(self):
        dump = self.dumping()
        fact = f"remember {uuid.uuid4().hex}"
        found = self.launched("w1", "--env", "K=v", "--", *FLAGS, steps=RECALL, prompt=fact)
        os.kill(found["harness"], signal.SIGKILL)
        self.reaped(found["call"]["pid"], found["guard"])
        res = self.resume(steps=RECALL)
        self.assertEqual(self.record(res), found["record"])
        channel = self.owner("w1")
        self.assertNotEqual(channel.directory, found["channel"].directory)
        status = channel.status()
        self.assertEqual(status["state"], "running")
        self.assertNotEqual(status["pid"], found["harness"])
        first, again = self.calls("w1")
        self.assertNotEqual(again["pid"], first["pid"])
        self.assertNotEqual(int(ps(again["pid"], "ppid")), found["guard"])
        sid = found["record"].sid
        self.assertTrue(has_pair(again["argv"], "--resume", sid), again["argv"])
        self.assertNotIn("--session-id", again["argv"])
        self.assertTrue(has_pair(again["argv"], *FLAGS), again["argv"])
        self.assertEqual((again["cwd"], again["config_dir"]), (first["cwd"], first["config_dir"]))
        envs = [entry["env"] for entry in logged(dump)]
        self.assertEqual([env.get("K") for env in envs], ["v", "v"])
        self.assertEqual(envs[0].get("CLAUDE_CONFIG_DIR"), envs[1].get("CLAUDE_CONFIG_DIR"))
        self.ok(self.session("send", "w1", "what did I ask you to remember?"))
        live_tmux.wait(
            lambda: self.ok(self.session("reply", "w1")).stdout == f"{fact}\n",
            what="the resumed client's answer from the old conversation",
        )

    def test_resume_after_guard_sigkill_is_busy_and_starts_nothing(self):
        found = self.launched()
        os.kill(found["guard"], signal.SIGKILL)
        self.settled(found["channel"])
        res = self.resume()
        self.assertEqual((res.returncode, res.stdout), (75, ""), res.stderr)
        self.assertIn(GATE_BUSY, res.stderr)
        self.assertEqual(len(self.calls("w1")), 1)
        self.assertEqual(self.owner("w1").directory, found["channel"].directory)

    def test_start_with_display_killed_keeps_the_live_worker(self):
        found = self.launched()
        self.ok(self.server.tmux("kill-pane", "-t", self.server.pane_of("w1")))
        res = self.start("w1")
        self.assertNotEqual(res.returncode, 0, res.stdout)
        self.assertIn("tui session w1 is running", res.stderr)
        self.assertTrue(running(found["call"]["pid"]))
        self.assertEqual(found["channel"].status()["state"], "running")
        self.assertEqual(self.owner("w1").directory, found["channel"].directory)

    def test_resume_without_transcript_exits_2_and_starts_nothing(self):
        found = self.launched()
        os.kill(found["harness"], signal.SIGKILL)
        self.reaped(found["call"]["pid"], found["guard"])
        call = found["call"]
        config = call["config_dir"] or os.path.join(self.home, ".claude")
        project = re.sub("[^A-Za-z0-9]", "-", os.path.realpath(call["cwd"]))
        os.unlink(os.path.join(config, "projects", project, f"{found['record'].sid}.jsonl"))
        res = self.resume()
        self.assertEqual((res.returncode, res.stdout), (2, ""), res.stderr)
        self.assertIn(
            f"session w1: conversation {found['record'].sid} has no transcript to resume",
            res.stderr,
        )
        self.assertEqual(len(self.calls("w1")), 1)

    def test_resume_shows_the_live_worker_again_unless_its_pane_was_closed_by_hand(self):
        """WDRC-300, AC14: resume asks the live harness nothing; a killed attach is shown again, a
        killed pane stays hidden; the same worker runs on.
        """
        for name, kill in (("w1", "attach"), ("w2", "pane")):
            with self.subTest(kill=kill):
                fact = f"remember {uuid.uuid4().hex}"
                # only the live client's second turn can answer with the fact
                found = self.launched(name, steps=["Noted."], turns=[RECALL], prompt=fact)
                before = self.server.pane_of(name)
                if kill == "pane":  # its close hook marks it hidden
                    self.ok(self.server.tmux("kill-pane", "-t", before))
                    live_tmux.wait(
                        lambda name=name: self.status(name)[0].hidden, what=f"{name} hidden"
                    )
                else:  # its guard.py --attach killed: the pane closes, the worker runs on
                    os.kill(int(self.pane(name, "#{pane_pid}")), signal.SIGKILL)
                live_tmux.wait(
                    lambda name=name: self.unshown(name), what=f"{name}'s worker pane to close"
                )
                resumed = self.resume(name, steps=RECALL, inside=MANAGER)
                self.assertEqual(self.record(resumed), found["record"])
                if kill == "pane":
                    self.assertEqual(self.server.pane_of(name), "")
                    self.assertTrue(self.status(name)[0].hidden)
                else:
                    self.shown_again(name, before)
                    self.assertFalse(self.status(name)[0].hidden)
                self.assertEqual(len(self.calls(name)), 1)
                self.assertTrue(running(found["call"]["pid"]))
                self.assertTrue(running(found["guard"]))
                self.assertEqual(self.owner(name).directory, found["channel"].directory)
                self.assertEqual(found["channel"].status()["state"], "running")
                self.ok(self.session("send", name, "what did I ask you to remember?"))
                live_tmux.wait(
                    lambda name=name, fact=fact: (
                        self.ok(self.session("reply", name)).stdout == f"{fact}\n"
                    ),
                    what="the same client's answer from its conversation",
                )

    def test_concurrent_resumes_start_one_client(self):
        found = self.launched()
        os.kill(found["harness"], signal.SIGKILL)
        env = self.env(inside=None, **self.scene("w1"))
        procs = []
        with self.poking():
            for _ in range(2):
                proc = subprocess.Popen(
                    [COMMAND, "resume", "w1"],
                    env=env,
                    cwd=self.root,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                self.addCleanup(proc.kill)
                procs.append(proc)
            outs = [proc.communicate(timeout=TIMEOUT) for proc in procs]
        self.assertEqual([proc.returncode for proc in procs], [0, 0], outs)
        self.assertEqual([out for out, _ in outs], [found["record"].to_line() + "\n"] * 2)
        self.assertEqual(len(self.calls("w1")), 2)
        (harness,) = self.running_harnesses()
        self.assertEqual(harness.directory, self.owner("w1").directory)
        self.assertTrue(running(self.calls("w1")[-1]["pid"]))


class CloseTest(CrashCase):
    def test_close_after_guard_sigkill_is_busy_and_keeps_the_record(self):
        found = self.launched()
        os.kill(found["guard"], signal.SIGKILL)
        self.settled(found["channel"])
        res = self.session("close", "w1")
        self.assertEqual(res.returncode, 75, res.stderr)  # every failed key refused busy
        self.assertIn(CLOSE_BUSY, res.stderr)
        self.assertEqual(self.owner("w1").directory, found["channel"].directory)

    def test_close_after_harness_sigkill_waits_out_reaping(self):
        found = self.launched()
        os.kill(found["harness"], signal.SIGKILL)
        self.ok(self.session("close", "w1"))
        self.assertFalse(os.path.lexists(os.path.join(self.ensemble, "tui", "w1")))
        self.assertFalse(self.live("w1"))
        self.assertFalse(running(found["call"]["pid"]) or running(found["guard"]))


class ApiCase(CrashCase):
    def setUp(self):
        super().setUp()
        self.out = os.path.join(self.root, "handled.jsonl")
        self.pause = os.path.join(self.root, "release")
        env = {**self.env(), **self.scene("w1"), "ENSEMBLE_EVENTS": self.events}
        self.enterContext(mock.patch.dict(os.environ, env, clear=True))
        self.cls = harnesses.Threaded.configured(
            clients={"claude": {"implementation": "ensemble.session.clients.claude:ClaudeClient"}},
            hosts=Host.parse_hosts({"tmux": HOSTS["tmux"], "default": "tmux"}),
        )
        self.addCleanup(Path(self.pause).touch)  # releases a draining harness first

    def started(self, *, resume: bool = False, **state: str) -> OwnedSession:
        spec = Spec(
            workdir=self.cwd,
            access=Access(cwd=self.cwd),
            client="claude",
            mode="tui",
            name="w1",
            sid=self.sid,
            prompt="" if resume else "hi",
            resume=resume,
            handlers=(
                HookCallback(HANDLER, {"path": self.out, "pause": self.pause, **state}, (REPO,)),
            ),
        )
        session = self.cls.start(spec)
        assert isinstance(session, OwnedSession)
        return session

    def turn_ended(self) -> None:
        live_tmux.wait(
            lambda: any(r["type"] == "TurnEnd" for r in logged(self.out)),
            timeout=30,
            what="the handler's TurnEnd",
        )

    def turn_end(self, reply: str) -> dict:
        """The handler's record of the TurnEnd with `reply`."""
        return live_tmux.wait(
            lambda: next(
                (r for r in logged(self.out) if r["type"] == "TurnEnd" and r["reply"] == reply),
                None,
            ),
            timeout=30,
            what=f"the handler's TurnEnd {reply!r}",
        )


class DisplayTest(ApiCase):
    def test_a_handlers_send_reaches_the_worker_shown_again_after_its_pane_was_killed(self):
        """WDRC-304, AC14: the Dispatch view keeps its attach while its pane is killed; a show opens
        a new one.
        """
        self.sid = str(uuid.uuid4())
        self.scene("w1", turns=[["second"], ["third"], ["fourth"], ["fifth"]])
        send = os.path.join(self.root, "send")
        view = self.started(send=send)
        self.turn_end("Hi from w1.")
        Path(send).write_text("from the handler", encoding="utf-8")
        self.ok(self.session("send", "w1", "go"))
        self.assertEqual(self.turn_end("second")["sent"], "from the handler")
        self.turn_end("third")  # its send reached the worker: the view holds that attach
        self.ok(self.session("show", "w1"))
        before = self.server.pane_of("w1")
        self.assertTrue(before)
        self.ok(self.server.tmux("kill-pane", "-t", before))
        live_tmux.wait(lambda: self.status("w1")[0].hidden, what="w1 hidden")
        self.ok(self.session("show", "w1"))
        self.shown_again("w1", before)
        self.assertFalse(self.status("w1")[0].hidden)
        Path(send).write_text("again from the handler", encoding="utf-8")
        self.ok(self.session("send", "w1", "go on"))
        self.assertEqual(self.turn_end("fourth")["sent"], "again from the handler")
        self.turn_end("fifth")
        self.assertFalse(os.path.exists(send))
        self.assertEqual(len(self.calls("w1")), 1)
        self.assertEqual(self.owner("w1").status()["generation"], view.generation)


class GenerationTest(ApiCase):
    """A view's close acts on its own generation, never on the newer one now holding the name."""

    def assert_lives(self, view: OwnedSession) -> None:
        channel = self.owner("w1")
        self.assertEqual(channel.status()["state"], "running")
        self.assertEqual(channel.status()["generation"], view.generation)
        self.assertEqual(self.liveness("w1", view.generation), Liveness.LIVE)
        self.assertTrue(self.live("w1"))
        self.assertTrue(running(self.calls("w1")[-1]["pid"]))

    def test_old_harness_close_in_drain_keeps_the_new_generation(self):
        self.sid = str(uuid.uuid4())
        old = self.started()
        self.turn_ended()
        draining = self.owner("w1")
        os.kill(self.calls("w1")[-1]["pid"], signal.SIGTERM)
        self.settled(draining)  # exited; its handler holds it in its drain
        self.assertTrue(draining.alive(), draining.status())
        new = self.started(resume=True)
        self.assertNotEqual(new.generation, old.generation)
        old.close()
        old.stop()
        self.assert_lives(new)

    def test_stale_view_close_keeps_the_new_generation(self):
        self.sid = str(uuid.uuid4())
        old = self.started()
        self.turn_ended()
        first = self.owner("w1")
        os.kill(first.status()["pid"], signal.SIGKILL)
        live_tmux.wait(lambda: not first.alive(), what="the killed harness to let go")
        new = self.started(resume=True)
        old.close()
        self.assert_lives(new)
