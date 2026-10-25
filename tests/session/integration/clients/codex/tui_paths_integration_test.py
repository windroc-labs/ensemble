"""Codex source-root and symlink policies against real filesystem paths."""

import dataclasses
import json
import shlex
import tomllib
from pathlib import Path
from unittest import mock

from ensemble.session.clients.codex import tui
from ensemble.session.clients.codex.tui import HOOKS, HookPlan
from ensemble.session.clients.codex.tui import start as tui_start
from tests.session.case import SRC
from tests.session.codex_tui import HOOK, PlanCase
from tests.session.source_roots import PACKAGE_ROOTS
from tests.session.unit.clients.codex.support import Base, committed_tui


class StartPaths(Base):
    def test_unknown_launch_fields_are_refused_without_spawning(self):
        with mock.patch("shutil.which", return_value="/protected/codex"):
            launch = committed_tui(
                self.client, dataclasses.replace(self.spec, prompt="prompt"), HOOK
            )
        data = json.loads(launch.argv[-1])
        with mock.patch("subprocess.Popen") as proc, mock.patch("os.execve") as execute:
            for changed in (data | {"unknown": []}, data | {"import_roots": list(PACKAGE_ROOTS)}):
                with (
                    self.subTest(changed=sorted(changed)),
                    self.assertRaisesRegex(ValueError, "fields"),
                ):
                    tui_start.launch(changed)
        proc.assert_not_called()
        execute.assert_not_called()


class TuiPaths(Base):
    def test_a_driver_hook_link_inside_the_cwd_launches_and_execs(self):
        link = self.cwd / "driver-link.py"
        link.symlink_to("/protected/driver_hook.py")
        with mock.patch("shutil.which", return_value="/protected/codex"):
            launch = committed_tui(
                self.client,
                dataclasses.replace(self.spec, prompt="hi"),
                (*HOOK[:2], str(link), *HOOK[3:]),
            )
        with mock.patch("os.chdir"), mock.patch("os.execve") as execute:
            tui_start.launch(
                json.loads(launch.argv[-1]), trust=mock.Mock(return_value="hooks.state={x}")
            )
        self.assertEqual(execute.call_args.args[0], "/protected/codex")


class PlanPaths(PlanCase):
    def test_hook_commands_preserve_quoting_timeouts_and_matchers(self):
        roots = []
        for package, name in (("session", "session 'sources"), ("utils", "utils sources 字")):
            root = Path(self.home) / name
            root.symlink_to(SRC[package], target_is_directory=True)
            roots.append(str(root))
        argv = (
            *self.plan.hook.argv,
            json.dumps(roots, ensure_ascii=False, separators=(",", ":")),
            "opaque trailing word",
        )
        hook = HookPlan(argv)
        self.assertEqual(hook.argv, argv)
        decoded = tui.decode(json.loads(tui.encode(dataclasses.replace(self.plan, hook=hook))))
        self.assertEqual(decoded.hook, hook)
        commands = tui.commands(hook)
        self.assertEqual(list(commands), [event for event, _, _ in HOOKS])
        flags = tui.hook_flags(hook)
        self.assertEqual(flags[::2], ("-c",) * len(HOOKS))
        for (event, _, _), encoded in zip(HOOKS, flags[1::2], strict=True):
            with self.subTest(event=event):
                self.assertEqual(shlex.split(commands[event]), [*argv, event])
                group = tomllib.loads(encoded)["hooks"][event][0]
                self.assertEqual(group.get("matcher"), tui.MATCHERS.get(event))
                row = group["hooks"][0]
                self.assertEqual(
                    row,
                    {
                        "type": "command",
                        "command": commands[event],
                        "timeout": 3 if event == "Interrupt" else 10,
                    },
                )
