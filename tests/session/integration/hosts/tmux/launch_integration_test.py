"""TmuxHost preserves literal launch data and terminal paste bytes on a private server, and
submits input to a pane in copy-mode.
"""

import json
import os
import sys
import time
from pathlib import Path
from unittest import mock

from ensemble.session import Launch, SessionError
from ensemble.session.engine.tmux_cli import TmuxCli
from ensemble.session.engine.types import Key, KeyPress, Paste
from ensemble.session.hosts.tmux import TmuxHost
from tests.session import live_tmux
from tests.session.case import Case

WAIT = 10


class LaunchTest(Case):
    def setUp(self):
        self.server = live_tmux.Server(self, self.home)
        self.server.start("manager")
        self.enterContext(mock.patch.dict(os.environ, self.server.env(), clear=True))
        self.host = TmuxHost("tmux")
        self.handles = []
        self.addCleanup(self.close_all)

    def close_all(self):
        for process in self.handles:
            process.close()

    def poll(self, check):
        deadline = time.monotonic() + WAIT
        while not (found := check()):
            if time.monotonic() >= deadline:
                self.fail("launch observation timed out")
            time.sleep(0.02)
        return found

    def launch(self, kind, name, launch):
        process = getattr(self.host, kind)(name, launch)
        self.handles.append(process)
        return process

    def test_literal_argv_path_cwd_and_env_reach_both_kinds_terminals_keep_their_identity(self):
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
        for kind in ("detach", "terminal"):
            with self.subTest(kind=kind):
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
                process = self.launch(
                    kind,
                    kind,
                    Launch((script.name, str(out), str(gate), *data), cwd=self.home, env=env),
                )
                self.poll(lambda out=out: out.exists() and out.stat().st_size)
                got = json.loads(out.read_text())
                self.assertEqual(got["argv"], data)
                self.assertTrue(os.path.samefile(got["cwd"], self.home))
                given = {key: value for key, value in env.items() if key not in ("TMUX", "TERM")}
                self.assertEqual({key: got["env"][key] for key in given}, given)
                self.assertEqual(
                    {key: got["env"][key] == env[key] for key in ("TMUX", "TERM")},
                    {"TMUX": kind == "detach", "TERM": kind == "detach"},
                )
                self.assertTrue(process.alive())
                gate.touch()
                self.assertTrue(process.wait_exit(WAIT))

    def test_launch_hands_a_long_argv_and_env_over_in_cwd_and_views_are_not_listed(self):
        out = os.path.join(self.home, "launched.json")
        code = "import json, os, sys, time; json.dump([sys.argv[1:], os.getcwd(), os.environ['X']], open(sys.argv[1], 'w')); time.sleep(30)"
        prompt = "a b ; $(echo x)\n" * 4096
        tmux = TmuxCli()
        tmux.launch(
            "v1",
            [sys.executable, "-I", "-c", code, out, prompt],
            cwd=self.home,
            env={"X": "x y"},
            python=sys.executable,
            handover_dir=self.home,
            tags={"@view": "docker"},
        )
        self.addCleanup(tmux.kill, "v1")
        self.poll(lambda: os.path.exists(out) and os.path.getsize(out))
        with open(out) as f:
            self.assertEqual(json.load(f), [[out, prompt], os.path.realpath(self.home), "x y"])
        self.assertFalse([name for name in os.listdir(self.home) if name.startswith("launch-")])
        self.assertEqual(tmux.sessions(), ["manager"])
        self.assertEqual([row["name"] for row in tmux.listing()], ["manager"])

    def test_long_unicode_multiline_paste_is_literal_and_does_not_submit(self):
        text = ("雪 a b\n" * 3000) + "literal ; $(echo x)"
        payload = text.encode()
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
        terminal = self.launch(
            "terminal",
            "paste",
            Launch(
                (sys.executable, "-I", "-c", code, str(ready), str(output), str(len(payload))),
                cwd=self.home,
            ),
        )
        self.poll(ready.exists)
        before = self.server.tmux("list-buffers").stdout
        terminal.send(Paste(text))
        self.poll(lambda: output.exists() and output.stat().st_size == len(payload))
        self.assertEqual(output.read_bytes(), payload)
        self.assertTrue(terminal.alive())
        self.assertEqual(self.server.tmux("list-buffers").stdout, before)

    def test_a_pane_scrolled_into_copy_mode_still_gets_and_submits_pastes_and_keys(self):
        ready, lines = Path(self.home, "ready"), Path(self.home, "lines")
        code = (
            "import sys\n"
            "open(sys.argv[1], 'w').close()\n"
            "for line in sys.stdin:\n"
            "    with open(sys.argv[2], 'a') as f: f.write(line)\n"
        )
        terminal = self.launch(
            "terminal",
            "scrolled",
            Launch((sys.executable, "-I", "-c", code, str(ready), str(lines)), cwd=self.home),
        )
        self.poll(ready.exists)
        pane = terminal.identity.split(" ")[2]

        def in_mode():
            res = self.server.tmux("display-message", "-p", "-t", pane, "#{pane_in_mode}")
            self.assertEqual(res.returncode, 0, res.stderr)
            return res.stdout.strip()

        def scroll():
            res = self.server.tmux("copy-mode", "-u", "-t", pane)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertEqual(in_mode(), "1")

        scroll()
        terminal.send(Paste("nudge"))
        scroll()
        terminal.send(Key("Enter"))
        self.poll(lambda: lines.exists() and lines.read_text() == "nudge\n")
        scroll()
        terminal.press([KeyPress("o"), KeyPress("k"), KeyPress("Enter")])
        self.poll(lambda: lines.read_text() == "nudge\nok\n")
        self.assertEqual(in_mode(), "0")
        self.assertTrue(terminal.alive())

    def test_a_split_and_refocused_session_is_reached_through_its_launched_pane(self):
        ready, received = Path(self.home, "ready"), Path(self.home, "received")
        code = (
            "import os, sys, tty\n"
            "tty.setraw(0)\n"
            "open(sys.argv[1], 'w').close()\n"
            "data = b''\n"
            "while len(data) < 4: data += os.read(0, 4 - len(data))\n"
            "with open(sys.argv[2], 'wb') as f: f.write(data)\n"
            "while True: os.read(0, 1)\n"
        )
        launched = self.host.terminal(
            "split",
            Launch((sys.executable, "-I", "-c", code, str(ready), str(received)), cwd=self.home),
            metadata={"sid": "sid-1", "client": "tool", "generation": "generation-1"},
        )
        self.handles.append(launched)
        self.poll(ready.exists)
        pane = launched.identity.split(" ")[2]
        added = self.server.tmux(
            "split-window", "-P", "-F", "#{pane_id}", "-t", "=split:", *live_tmux.IDLE
        )
        self.assertEqual(added.returncode, 0, added.stderr)
        other = added.stdout.strip()
        active = self.server.tmux("display-message", "-p", "-t", "=split:", "#{pane_id}")
        self.assertEqual(active.stdout.strip(), other)

        attached = self.host.attach("split")
        self.assertEqual(attached.identity, launched.identity)
        self.assertEqual(attached.session_identity(), ("sid-1", "generation-1"))
        self.assertTrue(attached.alive())
        attached.send(Paste("ping"))
        self.poll(lambda: received.exists() and received.stat().st_size == 4)
        self.assertEqual(received.read_bytes(), b"ping")
        self.assertNotIn("ping", self.server.tmux("capture-pane", "-p", "-t", other).stdout)

        attached.close()
        panes = self.server.tmux("list-panes", "-s", "-t", "=split:", "-F", "#{pane_id}")
        self.assertEqual(panes.returncode, 0, panes.stderr)
        self.assertEqual(panes.stdout.split(), [other])
        self.assertNotEqual(pane, other)
        self.assertFalse(attached.alive())

    def test_stale_terminal_cannot_send_to_a_same_name_replacement(self):
        launch = Launch((sys.executable, "-I", "-c", "import time; time.sleep(30)"), cwd=self.home)
        old = self.launch("terminal", "same", launch)
        old.close()
        new = self.launch("terminal", "same", launch)
        with self.assertRaises(SessionError):
            old.send(Paste("old input"))
        old.kill()
        old.close()
        self.assertTrue(old.wait_exit(0))
        self.assertTrue(new.alive())
