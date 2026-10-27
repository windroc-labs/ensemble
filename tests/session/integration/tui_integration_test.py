"""HostedSession's tui mode end to end on a private tmux server (tests/session/live_tmux.py),
os.environ that server's env: the fake client's Tui (tests/session/fake_client/) started with no
pane, its hook steps as events and events-file lines, send, the idle timer and its Blocked pause,
attach from another process, stop, a client's own exit, close by name, list while tmux's state is
unknown, an old session once its name is another session's, two starts of one name at once, and a
start while another process closes the name.
"""

import contextlib
import itertools
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from collections.abc import Iterator
from unittest import mock

from ensemble.session import Session
from ensemble.session.engine.records import RunLog
from ensemble.session.engine.tmux_cli import TmuxCli
from ensemble.session.engine.types import (
    Access,
    Activity,
    Blocked,
    Exited,
    IdleTimeout,
    Mode,
    SessionError,
    SessionStatus,
    Spec,
    TurnEnd,
)
from ensemble.session.modes import tui
from tests.session import fake_client, live_tmux
from tests.session.case import PY, SPAWNS
from tests.session.tui_support import DIALOG, IDLE, MANAGER, SID, WAIT, Live, Stream

# Another process: attaches by name, sends text, prints [sid, state, reply, its first event but
# IdleTimeout, reply].
ATTACH = """import importlib.util, json, sys
fake, clients, name, text = sys.argv[1:]
spec = importlib.util.spec_from_file_location("ensemble.session.clients.fake", fake)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
from ensemble.session import Host, IdleTimeout, Session
hosts = Host.parse_hosts({"default": "tmux", "tmux": {"implementation": "ensemble.session.hosts.tmux:TmuxHost"}})
Session.configure(clients=json.loads(clients), hosts=hosts)
handle = Session.attach(name)
before = [handle.sid, handle.alive(), handle.reply]
handle.send(text)
event = next(e for e in handle.events() if not isinstance(e, IdleTimeout))
print(json.dumps([*before, repr(event), handle.reply]))
"""
CLOSE = "import sys; from ensemble.session import Host, Session; Session.configure(clients={}, hosts=Host.parse_hosts({'default': 'tmux', 'tmux': {'implementation': 'ensemble.session.hosts.tmux:TmuxHost'}})); Session.retire(sys.argv[1])"


class TuiTest(Live):
    def test_a_session_over_hooks_send_idle_attach_and_stop(self):
        handle = self.start(
            "w1",
            {"hook": "tool", "payload": {"session_id": "n1"}},
            {"hook": "stop", "payload": {"last_message": "r1"}},
            {"read": True},
            {"hook": "ask", "payload": {}},
            {"read": True},
            {"hook": "stop", "payload": {"last_message": "r2", "tasks": [1]}},
            {"read": True},
            sid=SID,
            idle_timeout=IDLE,
        )
        self.assertEqual(len(self.server.panes(f"={MANAGER}:")), 1)
        self.assertEqual(self.server.option("w1", "@sid"), SID)
        first = RunLog(self.workdir).read()[0]  # its harness may have bound the client since
        self.assertEqual((first["kind"], first["sid"]), ("session", SID))
        self.assertEqual(tui.TuiMode.snapshot("w1")["sid"], SID)
        stream = Stream(self, handle)
        # Hook processes can exceed IDLE between tool and stop on a loaded runner.
        self.assertEqual(
            [stream.next(idle=False) for _ in range(2)], [Activity(""), TurnEnd("r1", 0)]
        )
        self.assertEqual(stream.next(), IdleTimeout(IDLE))
        self.assertEqual(
            [(r["kind"], r.get("client_id")) for r in RunLog(self.workdir).read()],
            [("session", None), ("client_session", "n1")],
        )
        self.lines("w1 done")
        self.assertEqual(live_tmux.activity("w1"), "done")

        handle.send("go")
        self.assertEqual(stream.next(idle=False), Blocked(""))
        self.lines("w1 done", "w1 blocked")
        stream.quiet(3 * IDLE)

        res = subprocess.run(
            [*PY, "-c", ATTACH, fake_client.__file__, json.dumps(self.clients), "w1", "more"],
            env=self.server.child_env(),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=WAIT,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(json.loads(res.stdout), [SID, True, "r1", repr(TurnEnd("r2", 1)), "r2"])
        self.assertEqual(stream.next(), TurnEnd("r2", 1))
        self.lines("w1 done", "w1 blocked", "w1 done")
        self.assertEqual(handle.reply, "r2")

        handle.stop()
        self.assertEqual(stream.until_exited(), [Exited(-signal.SIGHUP)])
        self.assertEqual(self.session_cls.list("w1"), [])
        self.assertFalse(
            os.path.lexists(tui.TuiStore.attach("w1", host=self.session_cls.host()).dir)
        )

    def test_inspect_an_ended_tui_session_after_its_pane_and_store_are_removed(self):
        self.clients["fake"]["implementation"] = "ensemble.session.clients.fake:InspectClient"
        self.session_cls = self.fake_session()
        session = self.start("historical", {"exit": 3}, sid=SID)
        stream = Stream(self, session)
        self.assertEqual(stream.next(poke=self.poke), Exited(3))
        info = session.info()
        session.close()
        self.assertEqual(self.session_cls.list("historical"), [])
        self.assertFalse(
            os.path.lexists(tui.TuiStore.attach("historical", host=self.session_cls.host()).dir)
        )
        with contextlib.ExitStack() as stack:
            starts = [
                stack.enter_context(mock.patch(target, side_effect=AssertionError(target)))
                for target in SPAWNS
            ]
            status = self.session_cls.inspect(
                Spec(workdir=self.workdir, client="fake", sid=SID, access=Access(cwd=self.dir))
            )
        self.assertEqual(
            status, SessionStatus("yes", 1.5, f"{SID} {info.cwd} {info.transcript} {info.resume}")
        )
        for start in starts:
            start.assert_not_called()

    def test_a_client_exit_ends_with_its_code_the_session_listed_dead_until_close(self):
        handle = self.start("w2", {"hook": "stop", "payload": {"last_message": "bye"}}, {"exit": 3})
        stream = Stream(self, handle)
        self.assertEqual(stream.next(), TurnEnd("bye", 0))
        self.assertEqual(stream.next(poke=self.poke), Exited(3))
        self.lines("w2 done", "w2 dead")
        self.assertEqual(  # not alive: its worker ended, its display kept till close
            (handle.alive(), [r.state for r in self.session_cls.list("w2")]), (False, ["dead"])
        )
        tui.TuiStore.attach("w2", host=self.session_cls.host()).close()
        self.assertEqual((handle.alive(), self.session_cls.list("w2")), (False, []))
        self.assertFalse(
            os.path.lexists(tui.TuiStore.attach("w2", host=self.session_cls.host()).dir)
        )

    def test_an_unreachable_server_is_unknown_and_no_end(self):
        if os.getuid() == 0:
            self.skipTest("root connects to a mode 000 socket")
        handle = self.start("w3", {"read": True})
        stream = Stream(self, handle)
        mode = os.stat(self.server.socket).st_mode
        os.chmod(self.server.socket, 0)
        self.addCleanup(os.chmod, self.server.socket, mode)
        with self.assertRaisesRegex(SessionError, "^tmux sessions unknown: "):
            self.session_cls.list("w.*")
        stream.quiet(1.0)  # its harness, not tmux, supervises the worker
        os.chmod(self.server.socket, mode)
        self.assertEqual([r.name for r in self.session_cls.list("w3")], ["w3"])
        handle.stop()
        self.assertEqual(stream.until_exited(), [Exited(-signal.SIGHUP)])


class SplitTest(Live):
    def test_a_split_and_refocused_session_keeps_its_client_pane(self):
        self.start(
            "w5",
            {"read": True},
            {"hook": "stop", "payload": {"last_message": "r1"}},
            {"read": True},
            sid=SID,
        )
        added = self.server.tmux(
            "split-window", "-P", "-F", "#{pane_id}", "-t", "=w5:", *live_tmux.IDLE
        )
        self.assertEqual(added.returncode, 0, added.stderr)
        other = added.stdout.strip()
        self.assertEqual(self.active("w5"), other)  # its client's pane no longer active

        attached = self.session_cls.attach("w5")
        attached.send("ping")
        self.lines("w5 done")
        self.assertNotIn("ping", self.server.tmux("capture-pane", "-p", "-t", other).stdout)
        self.assertEqual((attached.alive(), live_tmux.activity("w5")), (True, "done"))

        attached.stop()
        self.assertFalse(
            os.path.lexists(tui.TuiStore.attach("w5", host=self.session_cls.host()).dir)
        )

    def active(self, session: str) -> str:
        found = self.server.tmux("display-message", "-p", "-t", f"={session}:", "#{pane_id}")
        self.assertEqual(found.returncode, 0, found.stderr)
        return found.stdout.strip()


class SameNameTest(Live):
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(fake_client.FakeTui, "dialogs", (DIALOG,))
        patch.start()
        self.addCleanup(patch.stop)

    def old(self, name: str) -> tuple[Session, Iterator]:
        """Session `name` and its events() past its turn's end."""
        handle = self.start(
            name, {"hook": "stop", "payload": {"last_message": "r1"}}, {"read": True}
        )
        events = handle.events()
        self.assertEqual(next(events), TurnEnd("r1", 0))
        self.lines(f"{name} done")
        return handle, events

    def renewed(self, name: str, old: Session, events: Iterator) -> None:
        """`name` started again showing DIALOG, with an events file of its own: old's events end,
        never scanning it; old.stop() leaves it; the new session gets Blocked and the blocked line.
        """
        mine = os.path.join(self.dir, "events-new")
        new = self.start(name, {"raw": DIALOG}, {"read": True}, events=mine)
        live_tmux.wait(
            lambda: DIALOG in new.screen(), what="the dialog on the new session's screen"
        )
        self.assertEqual(next(events), Exited(-signal.SIGHUP))
        self.assertEqual((list(events), old.alive()), ([], False))
        old.stop()
        stream = Stream(self, new)
        self.addCleanup(new.stop)
        self.assertEqual(stream.next(), Blocked(DIALOG))
        self.lines(f"{name} blocked", events=mine)
        self.lines(f"{name} done")
        self.assertEqual((new.alive(), live_tmux.activity(name)), (True, "blocked"))

    def test_a_restart_after_stop(self):
        old, events = self.old("w4")
        old.stop()
        self.renewed("w4", old, events)

    def test_two_starts_of_one_name_at_once_make_one_session_its_dir_kept(self):
        with open(self.clients["fake"]["scenario"], "w") as f:
            json.dump([{"read": True}], f)
        tmux = TmuxCli()
        real, order, second, first_done = (
            tmux.start,
            itertools.count(),
            threading.Event(),
            threading.Event(),
        )
        results, threads = [], []

        def run():
            try:
                results.append(
                    self.session_cls.start(
                        Spec(
                            workdir=self.workdir,
                            access=Access(cwd=self.workdir),
                            name="w6",
                            client="fake",
                            mode=Mode.TUI,
                            prompt="hello",
                        )
                    )
                )
            except SessionError as e:
                results.append(e)

        def start(*args, **kwargs):
            if next(order):
                second.set()
                first_done.wait(WAIT)
                return real(*args, **kwargs)
            threads.append(threading.Thread(target=run))
            threads[0].start()
            second.wait(1)  # unlocked, the second start prepares meanwhile
            try:
                return real(*args, **kwargs)
            finally:
                first_done.set()

        self.session_cls = self.fake_session(
            host=type(
                "HostProxy",
                (),
                {"__getattr__": lambda _, key: start if key == "start" else getattr(tmux, key)},
            )()
        )
        run()
        threads[0].join(WAIT)
        handle, error = sorted(results, key=lambda result: isinstance(result, Exception))
        self.addCleanup(handle.stop)
        self.assertEqual(tui.TuiMode.snapshot("w6")["sid"], handle.sid)
        self.assertEqual(str(error), "tui session w6 is running; tmux attach -t '=w6'")
        self.assertEqual(
            (
                handle.alive(),
                os.listdir(tui.TuiStore.attach("w6", host=self.session_cls.host()).dir),
            ),
            (True, ["session.json"]),
        )

    def test_a_restart_after_a_close_from_another_process(self):
        old, events = self.old("w5")
        res = subprocess.run(
            [*PY, "-c", CLOSE, "w5"],
            env=self.server.child_env(),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=WAIT,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.renewed("w5", old, events)

    def test_a_close_from_another_process_then_a_start_of_its_name_keeps_the_new_sessions_dir(self):
        self.start("w7", {"read": True})
        retiring, remove = threading.Event(), tempfile.TemporaryDirectory.cleanup

        def slow(retired):  # its owner's removal of the closed dir, on its harness thread here
            retiring.set()
            time.sleep(1)
            remove(retired)

        with mock.patch.object(tempfile.TemporaryDirectory, "cleanup", slow):
            closer = subprocess.Popen(
                [*PY, "-c", CLOSE, "w7"],
                env=self.server.child_env(),
                text=True,
                stdin=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            self.assertTrue(retiring.wait(WAIT), "the close's removal")
            new = self.start("w7", {"read": True})
            self.assertEqual((closer.communicate(timeout=WAIT)[1], closer.returncode), ("", 0))
        self.assertEqual((new.alive(), tui.TuiMode.snapshot("w7")["sid"]), (True, new.sid))


if __name__ == "__main__":
    unittest.main()
