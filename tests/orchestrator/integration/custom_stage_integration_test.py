"""A custom role as a board stage: a team dir's writer, a workflow whose writer stage hands off
to pm, and writer's Linear app; its issue goes from Todo to In Review, then promote.py makes pm's
PRD issue.
"""

import json
import os
import unittest
from unittest import mock

from ensemble.orchestrator.engine import config
from tests.orchestrator.case import REPO
from tests.orchestrator.integration import fake_linear, segments
from tests.orchestrator.integration.flow_fixtures import BRANCH, DOCS, claude_account
from tests.orchestrator.integration.segments import INSTRUCTIONS, RESEARCH, Segment
from tests.orchestrator.unit import run_fixtures
from tests.orchestrator.unit.board_ids import PACKAGED_FLOW, role, role_id
from tests.utils.case import local_section
from tests.utils.sandbox import owned_directory

ROLE, TOKEN, DIR = "writer", "linear-app-writer-token", "Notes/"

WRITER = role_id(ROLE)

TEAM = {
    f"roles/{ROLE}.md": (
        "# Writer\n\nYou write notes.\n\n## Tasks\n\n"
        "Pick the task below that fits the input; unsure → `note` (`<tasks>/note.md`).\n\n"
        "- `note` (`<tasks>/note.md`): a short note; always; light.\n"
    ),
    "tasks/note.md": (
        "# Note\n\n## Steps\n\n1. **Report progress:**\n   [ensemble-progress:start] the topic\n"
        "2. **Write.** Write it.\n\n## Resume\n\nRedo step 2.\n"
    ),
}

# writer, then the packaged pm and engineer stages
WORKFLOW = "\n[[stage]]\n".join(
    [
        f'role = "{ROLE}"\ninput = "research"\nnext = "pm"\n',
        *(p for p in PACKAGED_FLOW.split("\n[[stage]]\n")[1:] if 'role = "researcher"' not in p),
    ]
).join(("[[stage]]\n", ""))


def make_team(case):
    """A team dir of TEAM under the repo's gitignored work/ (a temp dir is one a run can write)."""
    team = owned_directory(case, os.path.join(REPO, "work"), prefix="team-")
    for rel, text in TEAM.items():
        os.makedirs(os.path.join(team, os.path.dirname(rel)), exist_ok=True)
        with open(os.path.join(team, rel), "w", encoding="utf-8") as f:
            f.write(text)
    return team


class CustomStage(Segment):
    def setUp(self):
        super().setUp()
        team = make_team(self)
        flow = os.path.join(self.root, "workflow.toml")
        with open(flow, "w", encoding="utf-8") as f:
            f.write(WORKFLOW)
        board = (
            f'workflow = "{flow}"\n'
            + run_fixtures.local(os.path.join(self.root, "state"))
            + role(ROLE)
        )
        output = f'type = "github"\nrepo = "{DOCS}"\nbranch = "{BRANCH}"\ndir = "{DIR}"\n'
        with open(os.path.join(self.ensemble, "config.local.toml"), "w") as f:
            f.write(
                local_section(board, "orchestrator")
                + f"\n[delegate]\nteam_dirs = {json.dumps([team])}\n"
                + run_fixtures.CORE_LOCAL
                + f"\n[delegate.roles.{ROLE}.output]\n{output}"
                + claude_account(self.root)
            )
        self.fake.user("Writer", TOKEN, uid=WRITER)
        with open(self.seam, encoding="utf-8") as f:  # its keys: the users when it was written
            url = json.load(f)["url"]
        self.seam = fake_linear.seam(self, self.fake, url)
        for table, entry in (
            (segments.ROLE_OF, {WRITER: ROLE}),
            (segments.DIRS, {ROLE: DIR}),
        ):
            patch = mock.patch.dict(table, entry)
            patch.start()
            self.addCleanup(patch.stop)
        config.TASKS.load(flow)  # this process's readers (Segment's expectations) see it too

    def test_custom_role_ready_review_promote(self):
        ident = self.seed_backlog(ROLE, *RESEARCH)
        self.backlog_not_claimed(ident)
        self.human_move(ident, "todo", "human: to todo")
        self.claim(ident, ROLE, self.agent_script(ROLE, "done", ident))
        self.agent_run()
        self.assertEqual(self.state(ident), "in_review")
        # the stage's default leads
        said = [b for a, b in self.comments(ident) if a == WRITER]
        self.assertEqual(
            [b.split(" ", 2)[:2] for b in said], [[ROLE, "started:"], [ROLE, "done:"]], said
        )
        self.handoff(ident, INSTRUCTIONS)
        child = self.promote(ident, ROLE, "pm")
        self.assert_pm_start(child)
        self.assertEqual(self.issue(child)["title"], f"PRD: {RESEARCH[0]}")
        self.assertNotIn("researcher", config.TASKS)


if __name__ == "__main__":
    unittest.main()
