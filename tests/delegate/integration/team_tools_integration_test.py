"""A team dir's own tool, run as its role's prompt writes the approved command: its path and the
workdir hold a space and an apostrophe, and each role reaches the tool of the team dir holding it.
"""

import json
import os
import re
import shlex
import subprocess
import sys

from ensemble.delegate.composer import Composer, RunParams
from tests.delegate.case import Case

TOOL = "import json, sys\nprint(json.dumps([{team!r}, sys.argv[1:]]))\n"
STEPS = "\n## Steps\n\n1. **Report progress:**\n   [ensemble-progress:start] the topic.\n\n## Resume\n\nGo on.\n"
COMMAND = f"{sys.executable} -P {{{{role_team}}}}/tools/echo.py --dir {{{{workdir}}}}/src *"
RUN = re.compile(r"Run `([^`]+)`\.")


def charter(role: str, task: str) -> str:
    return (
        f"# {role.title()}\n\nWork.\n\n## Tasks\n\n"
        f"Pick the task below that fits the input; unsure → `{task}` (`<tasks>/{task}.md`).\n\n"
        f"- `{task}` (`<tasks>/{task}.md`): it; always; light.\n\n"
        f"Run `{sys.executable} -P <role team>/tools/echo.py --dir <Workdir>/src <x>`.\n"
    )


class TeamTools(Case):
    def test_each_role_runs_its_own_team_dirs_tool_from_its_prompt(self):
        teams = {
            "poet": os.path.realpath(os.path.join(self.ensemble, "te am's")),
            "bard": os.path.realpath(os.path.join(self.ensemble, "other team")),
        }
        for (role, top), task in zip(teams.items(), ("verse", "ballad"), strict=True):
            for rel, text in (
                (f"roles/{role}.md", charter(role, task)),
                (f"tasks/{task}.md", f"# {task.title()}\n" + STEPS),
                ("tools/echo.py", TOOL.format(team=role)),
            ):
                os.makedirs(os.path.dirname(path := os.path.join(top, rel)), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)
        composition = Composer(team_dirs=tuple(teams.values()))
        work = os.path.join(self.home, "w o'rk")
        for role in teams:
            with self.subTest(role=role):
                run = composition.resolve(
                    role, overlays=[{"roles": {role: {"commands": [COMMAND]}}}]
                )
                prompt = composition.render(
                    run, RunParams(input="", out=os.path.join(work, "out.md"), workdir=work)
                )
                [approved] = composition.access(run, work, cwd=work, project=False).commands
                [command] = RUN.findall(prompt)
                self.assertEqual(command, approved.removesuffix(" *") + " <x>")
                result = subprocess.run(
                    shlex.split(command.replace("<x>", shlex.quote("it's here"))),
                    cwd=self.home,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    encoding="utf-8",
                    timeout=60,
                    check=True,
                )
                self.assertEqual(
                    json.loads(result.stdout), [role, ["--dir", f"{work}/src", "it's here"]]
                )
