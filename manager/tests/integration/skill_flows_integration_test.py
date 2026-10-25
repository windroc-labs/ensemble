"""The commands /ensemble:manage writes, filled in as a commander would, run on
the real commands (manager/tests/live_crew.py): a worker's launch, reply, note, status,
gone-then-resume and close; a role run that ends waiting, answered in
its own session; manage's Codex issue worker launch.
"""

import json
import os

from ensemble.manager import State

from delegate.tests.integration.cli import ASKING, DONE
from delegate.tests.replies import block
from manager.tests.live_crew import ROLE, LiveCrew, has_pair, logged
from manager.tests.skills.commands import example, fill
from session.tests.integration.clients.codex import fake_tui


class SkillFlows(LiveCrew):
    def written(self, skill, prefix, *having, keep=(), env=None, **values):
        """skill's one command starting with prefix (and holding having), filled with values, run
        as the commander runs it.
        """
        argv = fill(example(skill, prefix, *having), *keep, **values)
        crew = self.crew_env() if argv[0] == "ensemble-crew" else {}
        return self.call(*argv, **crew, **(env or {}))

    def test_a_worker_launched_followed_up_resumed_and_closed_as_written(self):
        record = self.record(
            self.written(
                "manage",
                "ensemble-crew worker",
                "--client <client>",
                keep=("--prompt",),
                env=self.scene("w1"),
                name="w1",
                client="claude",
                text="hi",
            )
        )
        self.assertEqual(self.roster()["w1"].state, State.LIVE)
        self.wait_event("w1", "done")
        reply = self.ok(self.written("manage", "ensemble-session reply", name="w1"))
        self.assertIn("Hi from w1.", reply.stdout)
        self.ok(self.written("manage", "ensemble-crew note", name="w1", text="TASK-1 it's"))
        self.assertEqual(self.roster()["w1"].note, "TASK-1 it's")
        status = self.ok(
            self.written("manage", "ensemble-crew status [", keep=("<name>",), name="w1")
        )
        self.assertTrue(json.loads(status.stdout)["alive"])

        self.kill("w1")
        attach = self.ok(self.written("manage", "ensemble-crew attach"))
        self.assertRegex(attach.stdout, r"(?m)^resume: ensemble-crew resume .*\bw1$")
        resumed = self.record(
            self.written("manage", "ensemble-crew resume <name>…", env=self.scene("w1"), name="w1")
        )
        self.assertEqual(resumed.sid, record.sid)
        self.assertTrue(self.live("w1"))

        self.ok(self.written("manage", "ensemble-crew close <name>", name="w1"))
        self.assertNotIn("w1", self.roster())
        self.assertTrue(self.gone("w1"))

    def test_a_waiting_role_run_is_answered_in_its_session_as_written(self):
        work = self.workdir("r")
        launch = fill(
            example("manage", "ensemble-crew role"),
            "--workdir",
            "--name",
            "--mode",
            name="r",
            role=ROLE,
            workdir=work,
            input="Hello.",
        )
        argv = launch
        record = self.record(
            self.call(*argv, **self.crew_env(), **self.scene("r", [block(ASKING)]))
        )
        (harness,) = record.sessions
        self.wait_event(harness, "exited")
        self.ok(self.crew("attach"))
        self.assertEqual(self.roster()["r"].state, State.WAITING)

        answered = self.record(
            self.written(
                "manage",
                "ensemble-crew resume <name> --input",
                env=self.scene("r", [block(DONE)]),
                name="r",
                answers="Say hello.",
            )
        )
        self.assertEqual(answered.sid, record.sid)
        self.assertIs(self.roster()["r"].state, State.LIVE)
        self.wait_event(harness, "exited", count=2)
        self.assertTrue(has_pair(logged(self.log("r"))[-1]["argv"], "--resume", record.sid))
        status = json.loads(self.ok(self.crew("status", "r")).stdout)
        self.assertEqual(status["outcome"], "done")

    def test_manages_codex_issue_worker_launches_by_name_with_its_prompt(self):
        fake_tui.install(self.server.bin)
        never, scenario = (os.path.join(self.root, n) for n in ("never", "c1.codex.json"))
        with open(scenario, "w", encoding="utf-8") as f:
            json.dump({"tui_turns": [[{"kind": "pause", "file": never}]]}, f)
        log = os.path.join(self.root, "c1.codex.jsonl")
        agent = os.path.join(self.cwd, "agent")
        os.mkdir(agent)  # made by the prompt step's --out <work dir>/agent/prompt.md
        record = self.record(
            self.written(
                "manage",
                "ensemble-crew worker",
                "--client codex",
                env={"FAKE_SCENARIO": scenario, "FAKE_LOG": log},
                name="c1",
                work_dir=self.cwd,
            )
        )
        self.assertEqual((record.kind, record.name, record.detail), ("worker", "c1", "codex"))
        [begun] = [start for start in logged(log) if start.get("mode") == "tui"]
        self.assertEqual(os.path.realpath(begun["cwd"]), os.path.realpath(agent))
