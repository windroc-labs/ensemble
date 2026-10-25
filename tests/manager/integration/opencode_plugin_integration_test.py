"""The OpenCode plugin (.opencode-plugin/ensemble.js) under Node, driven as OpenCode calls it by
opencode_host.mjs; its re-arm runs the real rearm.py under this test process, its stand-in hold.
"""

import json
import os
import shutil
import subprocess
import sys
import unittest

from tests.manager.case import REPO, Case

NODE = shutil.which("node")
HOST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "opencode_host.mjs")
PLUGIN = os.path.join(REPO, ".opencode-plugin", "ensemble.js")
BIN = os.path.join(REPO, "bin")
MANAGER = {"ENSEMBLE_MANAGER": "mgr", "ENSEMBLE_HOLDER_ID": "holder-1"}
SKILLS = {
    "ensemble:act-as": "skills/act-as",
    "ensemble:board": "skills/board",
    "ensemble:manage": "skills/manage",
}


def created(session, parent=None):
    info = {"id": session, **({"parentID": parent} if parent else {})}
    return {"event": {"type": "session.created", "properties": {"info": info}}}


def event(kind, session):
    return {"event": {"type": kind, "properties": {"sessionID": session}}}


def system(session):
    return {"system": session}


@unittest.skipIf(NODE is None, "node is not on PATH: the OpenCode plugin tests run it under it")
class OpenCodePluginTest(Case):
    def setUp(self):
        # The plugin's python3 is this interpreter.
        self.bin = os.path.join(self.home, "bin")
        os.mkdir(self.bin)
        os.symlink(sys.executable, os.path.join(self.bin, "python3"))
        self.path = f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}"

    def play(self, steps, plugin=PLUGIN, **extra):
        env = {"PATH": self.path, "HOME": self.home, **extra}
        done = subprocess.run(
            (NODE, HOST, plugin),
            input=json.dumps(steps).encode(),
            capture_output=True,
            env=env,
            timeout=30,
            check=False,
        )
        self.assertEqual((done.returncode, done.stderr), (0, b""))
        return json.loads(done.stdout)

    def manager(self, steps):
        return self.play(steps, **MANAGER, ENSEMBLE_HOLDER_PID=str(os.getpid()))

    def test_a_session_ensemble_starts_gets_no_hooks(self):
        hooks = ["config", "event", "experimental.chat.system.transform", "shell.env"]
        self.assertEqual(self.manager([{"hooks": True}]), [hooks])
        for value in ("", "1"):
            with self.subTest(ENSEMBLE_AGENT_SESSION=value):
                played = self.play([{"hooks": True}], **MANAGER, ENSEMBLE_AGENT_SESSION=value)
                self.assertEqual(played, [[]])

    def test_each_skill_is_an_ensemble_command_beside_the_users_own(self):
        mine = {"template": "mine"}
        (config,) = self.play([{"config": {"command": {"mine": mine}}}])
        self.assertEqual(config["command"].pop("mine"), mine)
        self.assertEqual(sorted(config["command"]), sorted(SKILLS))
        for name, rel in SKILLS.items():
            with self.subTest(name):
                with open(os.path.join(REPO, rel, "SKILL.md"), encoding="utf-8") as f:
                    text = f.read()
                head, body = text.removeprefix("---\n").split("\n---\n", 1)
                command = config["command"][name]
                description = next(
                    line.removeprefix("description: ")
                    for line in head.splitlines()
                    if line.startswith("description: ")
                )
                self.assertEqual(command["description"], json.loads(description))
                skill_dir = os.path.join(REPO, rel)
                expected = body.lstrip("\n").replace("${CLAUDE_SKILL_DIR}", skill_dir)
                self.assertEqual(command["template"], expected)
        manage = config["command"]["ensemble:manage"]["template"]
        self.assertIn(os.path.join(REPO, "skills/manage/scripts/ctmux"), manage)
        self.assertIn("$ARGUMENTS", config["command"]["ensemble:act-as"]["template"])

    def test_the_shell_finds_the_plugins_bin_first(self):
        self.assertEqual(
            self.play([{"env": {"PATH": "/x"}}, {"env": {"OTHER": "1"}}]),
            [
                {"PATH": f"{BIN}{os.pathsep}/x"},
                {"OTHER": "1", "PATH": f"{BIN}{os.pathsep}{self.path}"},
            ],
        )

    def test_a_plugin_path_with_a_colon_goes_on_path_through_a_link(self):
        """OpenCode caches the plugin under .../ensemble@git+https:/...; no PATH entry can hold ":"."""
        root = os.path.join(self.home, "packages", "ensemble@git+https:", "ensemble")
        os.makedirs(os.path.join(root, ".opencode-plugin"))
        shutil.copy(PLUGIN, os.path.join(root, ".opencode-plugin"))
        for name in ("bin", "manager", "delegate", "orchestrator", "package.json"):
            os.symlink(os.path.join(REPO, name), os.path.join(root, name))
        plugin = os.path.join(root, ".opencode-plugin", "ensemble.js")
        for _ in range(2):
            [env] = self.play([{"env": {"PATH": "/x"}}], plugin=plugin)
            first, rest = env["PATH"].split(os.pathsep)
            self.assertEqual(rest, "/x")
            self.assertEqual(
                os.path.dirname(os.path.dirname(first)),
                os.path.join(self.home, ".ensemble", "links"),
            )
            self.assertEqual(os.readlink(os.path.dirname(first)), root)

    def test_a_new_root_sessions_first_turn_carries_the_startup_rearm_until_idle(self):
        played = self.manager(
            [created("ses_1"), system("ses_1"), system("ses_1"), event("session.idle", "ses_1")]
            + [system("ses_1")]
        )
        (first,), again, after = played[1], played[2], played[4]
        self.assertIn("SessionStart `startup` in manager `mgr`", first)
        self.assertIn("Manual monitoring; no idle notifications", first)
        self.assertNotIn("Monitor may be a deferred tool", first)
        self.assertEqual((again, after), ([first], []))

    def test_a_resumed_session_gets_resume_and_a_compaction_gets_compact(self):
        played = self.manager(
            [system("ses_old"), event("session.idle", "ses_old"), system("ses_old")]
            + [event("session.compacted", "ses_old"), system("ses_old")]
            + [event("session.idle", "ses_old"), system("ses_old")]
        )
        self.assertIn("SessionStart `resume`", played[0][0])
        self.assertEqual(played[2], [])
        self.assertIn("SessionStart `compact`", played[4][0])
        self.assertEqual(played[6], [])

    def test_an_idle_before_the_turn_got_it_keeps_the_rearm(self):
        played = self.manager([created("ses_1"), event("session.idle", "ses_1"), system("ses_1")])
        self.assertIn("SessionStart `startup`", played[2][0])

    def test_child_sessions_and_non_commanders_get_no_rearm(self):
        child = [created("ses_1"), created("ses_c", parent="ses_1"), system("ses_c")]
        child.append(event("session.compacted", "ses_c"))
        child.append(system("ses_c"))
        played = self.manager(child)
        self.assertEqual((played[2], played[4]), ([], []))
        self.assertEqual(self.play([created("ses_1"), system("ses_1")])[1], [])
        resumed_child = [{"saved": {"ses_c": {"id": "ses_c", "parentID": "ses_1"}}}]
        resumed_child += [system("ses_c"), event("session.compacted", "ses_c"), system("ses_c")]
        played = self.manager(resumed_child)
        self.assertEqual((played[1], played[3]), ([], []))
        self.assertIn("SessionStart `resume`", self.manager([system("ses_root")])[0][0])
        held_elsewhere = {**MANAGER, "ENSEMBLE_HOLDER_PID": "1"}
        self.assertEqual(self.play([created("ses_1"), system("ses_1")], **held_elsewhere)[1], [])
