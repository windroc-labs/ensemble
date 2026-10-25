"""Tui cleanup retires hook paths and closes arbitrary Host processes."""

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ensemble.session.engine.tmux_cli import TmuxCli
from ensemble.session.engine.types import Access, Spec
from ensemble.session.hosts.tmux import TmuxHost
from ensemble.session.modes import tui
from ensemble.session.modes.base import POINTER
from tests.session import fake_client
from tests.session.case import Case
from tests.session.fakes.terminal import ScreenHost
from tests.session.live_tmux import wait
from tests.session.tui_support import SID, Live

NAME = "w1"


class CloseTest(Case):
    def test_close_prevents_a_late_hook_from_recreating_files_during_removal(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root, host = os.path.join(self.ensemble, "tui"), ScreenHost()
        tui.TuiMode.start(
            Spec(
                workdir=tmp.name,
                access=Access(cwd=tmp.name),
                name=NAME,
                client="fake",
                sid=SID,
                events=os.path.join(self.home, "events"),
            ),
            fake_client.FakeClient({"tui": True, "scenario": "", "log": ""}),
            registry=self.session_cls._config().registry,
            record=lambda _info: None,
            host=host,
        )
        remove, hooks = os.rmdir, []

        def remove_after_hook(path, *args, **kwargs):
            hooks.append(
                subprocess.run(
                    [*tui.TuiStore.attach(NAME, host=host).hook_argv(), "stop"],
                    input=b"{}",
                    capture_output=True,
                    check=True,
                    timeout=5,
                )
            )
            return remove(path, *args, **kwargs)

        with mock.patch.object(os, "rmdir", remove_after_hook):
            tui.TuiStore.attach(NAME, host=host).close()
        self.assertEqual(len(hooks), 1)
        self.assertIn(b"session.json", hooks[0].stderr)
        self.assertFalse(os.path.lexists(os.path.join(root, NAME)))
        self.assertEqual(os.listdir(root), [])


class RawSessionCloseTest(Live):
    def test_legacy_close_removes_an_untagged_session_and_its_mode_state(self):
        name = "untagged"
        created = self.server.tmux("new-session", "-d", "-s", name, "cat", "-")
        self.assertEqual(created.returncode, 0, created.stderr)
        self.assertEqual(self.server.option(name, "@kind"), "")
        folder = Path(self.ensemble, "tui", name)
        folder.mkdir(parents=True)
        (folder / "session.json").write_text("{}")
        tui.TuiStore.attach(name, host=self.session_cls.host()).close()
        self.assertNotEqual(self.server.tmux("has-session", "-t", f"={name}").returncode, 0)
        self.assertFalse(folder.exists())
        tui.TuiStore.attach(name, host=self.session_cls.host()).close()


class PollCostTest(Live):
    def test_a_quiet_poll_of_a_live_terminal_runs_five_tmux_commands(self):
        commands = []

        def counted(argv, **kwargs):
            commands.append(argv[1])
            return subprocess.run(argv, **kwargs)

        host = TmuxHost("tmux", tmux=TmuxCli(runner=counted))
        scenario = self.clients["fake"]["scenario"]
        os.makedirs(self.workdir)
        with open(scenario, "w") as f:
            json.dump([{"read": True}], f)
        mode = tui.TuiMode.start(
            Spec(
                workdir=self.workdir,
                access=Access(cwd=self.workdir),
                name=NAME,
                client="fake",
                sid=SID,
                prompt="hello",
                events=self.events,
            ),
            self.session_cls._config().registry.client("fake"),
            registry=self.session_cls._config().registry,
            record=lambda _info: None,
            host=host,
        )
        self.addCleanup(tui.TuiStore.attach(NAME, host=host).close)
        while mode.poll(0)[0]:
            pass
        commands.clear()
        self.assertEqual(mode.poll(0), ([], False))
        self.assertEqual(
            commands,
            [
                "display-message",
                "display-message",
                "display-message",
                "capture-pane",
                "display-message",
            ],
        )


class LongPromptTest(Live):
    """A prompt over Linux's 128 KiB exec argument limit still starts its client."""

    def started_with(self, host):
        prompt = "雪 a\n" * 40_000
        os.makedirs(self.workdir)
        with open(self.clients["fake"]["scenario"], "w") as f:
            json.dump([{"read": True}], f)
        tui.TuiMode.start(
            Spec(
                workdir=self.workdir,
                access=Access(cwd=self.workdir),
                name=NAME,
                client="fake",
                sid=SID,
                prompt=prompt,
                events=self.events,
            ),
            self.session_cls._config().registry.client("fake"),
            registry=self.session_cls._config().registry,
            record=lambda _info: None,
            host=host,
        )
        self.addCleanup(tui.TuiStore.attach(NAME, host=host).close)
        log, path = self.clients["fake"]["log"], os.path.join(self.ensemble, "tui", NAME, "prompt")
        wait(lambda: os.path.exists(log) and os.path.getsize(log), what="the client's log")
        with open(log, encoding="utf-8") as f:
            argv = json.loads(f.readline())["argv"]
        path = os.path.join(path, "prompt.md")
        self.assertEqual(
            argv[argv.index("--") + 1],
            POINTER.format(size=len(prompt.encode()), path=path),
        )
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), prompt)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)

    def test_a_prompt_over_128_kib_starts_on_tmux_with_a_pointer_to_its_file(self):
        self.started_with(TmuxHost("tmux"))


if __name__ == "__main__":
    unittest.main()
