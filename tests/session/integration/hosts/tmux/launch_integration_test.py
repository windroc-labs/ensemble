"""TmuxHost's workers, under their guard on a private server, get literal launch data and paste
bytes; a display scrolled into copy-mode takes no input from them; a closed worker's handle never
reaches a same-name replacement.
"""

import contextlib
import json
import os
import sys
from pathlib import Path
from unittest import mock

from ensemble.session import Launch, SessionError
from ensemble.session.engine.tmux_cli import TmuxCli
from ensemble.session.engine.types import Key, KeyPress, Liveness, Paste, Unanswered, WorkerExit
from ensemble.session.hosts.tmux import TmuxHost
from tests.session import live_tmux
from tests.session.case import Case

BRACKETS = (b"\x1b[200~", b"\x1b[201~")


class LaunchTest(Case):
    def setUp(self):
        self.server = live_tmux.Server(self, self.home)
        self.server.start("manager")
        self.enterContext(mock.patch.dict(os.environ, self.server.env(), clear=True))
        self.host = TmuxHost("tmux")

    def worker(self, name, launch, *, terminal=True):
        handle = self.host.worker(name, launch, terminal=terminal)
        self.addCleanup(self.close, handle)
        return handle

    def close(self, handle):
        with contextlib.suppress(Unanswered):
            handle.close()

    def exited(self, handle):
        messages = []

        def found():
            messages.extend(handle.read(0.1))
            return any(isinstance(message, WorkerExit) for message in messages)

        live_tmux.wait(found, what=f"worker {handle.name} exit")
        return [message for message in messages if isinstance(message, WorkerExit)]

    def test_literal_argv_path_cwd_and_env_reach_both_kinds_of_worker(self):
        bindir = Path(self.home, "bin with spaces")
        bindir.mkdir()
        script = bindir / "literal-program"
        script.write_text(
            f"#!{sys.executable} -I\n"
            "import json, os, sys, time\n"
            "out, gate, *data = sys.argv[1:]\n"
            "with open(out, 'w') as f: json.dump({'argv': data, 'cwd': os.getcwd(), 'env': dict(os.environ)}, f)\n"
            "while not os.path.exists(gate): time.sleep(0.01)\n"
        )
        script.chmod(0o700)
        data = ["a b", "雪", "two\nlines", "-leading", ";", "$(echo literal)", "--"]
        for terminal in (False, True):
            with self.subTest(terminal=terminal):
                kind = "terminal" if terminal else "headless"
                out, gate = Path(self.home, kind + ".json"), Path(self.home, kind + ".gate")
                env = {
                    "PATH": str(bindir),
                    "PYTHONPATH": "literal import roots",
                    "ENSEMBLE_EVENTS": "literal events",
                    "TMUX": "supplied-server",
                    "PWD": "supplied-pwd",
                    "TERM": "supplied-term",
                    "LC_ALL": "C",
                }
                handle = self.worker(
                    kind,
                    Launch((script.name, str(out), str(gate), *data), cwd=self.home, env=env),
                    terminal=terminal,
                )
                live_tmux.wait(lambda out=out: out.exists() and out.stat().st_size, what="launch")
                got = json.loads(out.read_text())
                self.assertEqual(got["argv"], data)
                self.assertTrue(os.path.samefile(got["cwd"], self.home))
                given = {key: value for key, value in env.items() if key != "TMUX"}
                self.assertEqual({key: got["env"].get(key) for key in given}, given)
                self.assertNotIn("TMUX", got["env"])
                self.assertEqual(handle.state(), Liveness.LIVE)
                gate.touch()
                self.assertEqual(self.exited(handle), [WorkerExit(0)])

    def test_start_hands_a_long_argv_and_env_over_in_cwd_and_views_are_not_listed(self):
        out = os.path.join(self.home, "launched.json")
        code = "import json, os, sys, time; json.dump([sys.argv[1:], os.getcwd(), os.environ['X']], open(sys.argv[1], 'w')); time.sleep(30)"
        prompt = "a b ; $(echo x)\n" * 4096
        tmux = TmuxCli()
        tmux.start(
            "v1",
            Launch(
                (sys.executable, "-I", "-c", code, out, prompt), cwd=self.home, env={"X": "x y"}
            ),
            tags={"@view": "docker"},
        )
        self.addCleanup(tmux.kill, "v1")
        live_tmux.wait(lambda: os.path.exists(out) and os.path.getsize(out), what="launch")
        with open(out) as f:
            self.assertEqual(json.load(f), [[out, prompt], os.path.realpath(self.home), "x y"])
        self.assertEqual(tmux.sessions(), ["manager"])
        self.assertEqual([row["name"] for row in tmux.listing()], ["manager"])

    def test_long_unicode_multiline_paste_is_literal_and_does_not_submit(self):
        text = ("雪 a b\n" * 3000) + "literal ; $(echo x)"
        payload = BRACKETS[0] + text.encode() + BRACKETS[1]
        ready, output = Path(self.home, "ready"), Path(self.home, "paste")
        code = (
            "import os, sys, tty\n"
            "tty.setraw(0)\n"
            "open(sys.argv[1], 'w').close()\n"
            "data = b''\n"
            "while len(data) < int(sys.argv[3]): data += os.read(0, int(sys.argv[3]) - len(data))\n"
            "with open(sys.argv[2], 'wb') as f: f.write(data)\n"
            "while True: os.read(0, 1)\n"
        )
        worker = self.worker(
            "paste",
            Launch(
                (sys.executable, "-I", "-c", code, str(ready), str(output), str(len(payload))),
                cwd=self.home,
            ),
        )
        live_tmux.wait(ready.exists, what="raw tty")
        worker.send(Paste(text))
        live_tmux.wait(
            lambda: output.exists() and output.stat().st_size == len(payload), what="paste"
        )
        self.assertEqual(output.read_bytes(), payload)
        self.assertEqual(worker.state(), Liveness.LIVE)

    def test_a_display_scrolled_into_copy_mode_does_not_hold_back_pastes_or_keys(self):
        ready, lines = Path(self.home, "ready"), Path(self.home, "lines")
        code = (
            "import sys\n"
            "open(sys.argv[1], 'w').close()\n"
            "for line in sys.stdin:\n"
            "    with open(sys.argv[2], 'a') as f: f.write(line)\n"
        )
        worker = self.worker(
            "scrolled",
            Launch((sys.executable, "-I", "-c", code, str(ready), str(lines)), cwd=self.home),
        )
        live_tmux.wait(ready.exists, what="line reader")
        shown = self.server.tmux("display-message", "-p", "-t", "=scrolled:", "#{pane_id}")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        pane = shown.stdout.strip()
        res = self.server.tmux("copy-mode", "-u", "-t", pane)
        self.assertEqual(res.returncode, 0, res.stderr)
        mode = self.server.tmux("display-message", "-p", "-t", pane, "#{pane_in_mode}")
        self.assertEqual(mode.stdout.strip(), "1", mode.stderr)
        pasted = "\x1b[200~nudge\x1b[201~\n"
        worker.send(Paste("nudge"))
        worker.send(Key("Enter"))
        live_tmux.wait(lambda: lines.exists() and lines.read_text() == pasted, what="paste")
        worker.press([KeyPress("o"), KeyPress("k"), KeyPress("Enter")])
        live_tmux.wait(lambda: lines.read_text() == pasted + "ok\n", what="keys")
        self.assertEqual(worker.state(), Liveness.LIVE)

    def test_a_closed_worker_cannot_send_to_a_same_name_replacement(self):
        launch = Launch((sys.executable, "-I", "-c", "import time; time.sleep(30)"), cwd=self.home)
        old = self.worker("same", launch)
        old.close()
        new = self.worker("same", launch)
        self.assertNotEqual(new.generation, old.generation)
        with self.assertRaises(SessionError):
            old.send(Paste("old input"))
        self.assertEqual(old.state(), Liveness.ABSENT)
        self.assertEqual(new.state(), Liveness.LIVE)
