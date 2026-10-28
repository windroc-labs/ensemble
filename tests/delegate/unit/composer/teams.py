"""Fake team files (roles, tasks), a run config and FakeTeam, their temp team root, which the
composer and plan tests share; run_config, a RunConfig of given keys.
"""

import os

from ensemble.delegate.composer import RunConfig
from tests.delegate.case import Case
from tests.utils.case import local_section
from tests.utils.sandbox import scratch

INTRO = "Pick the task below that fits the input; unsure → `{0}` (`<tasks>/{0}.md`).\n\n"


WRITER = (
    "# Writer\n\nWrite well.\n\n## Tasks\n\n"
    + INTRO.format("short-note")
    + "- `short-note` (`<tasks>/short-note.md`): a note; most times; light.\n"
    "- `long-note` (`<tasks>/long-note.md`): a long note; when asked; heavy.\n\n"
    "## Style\n\n- `draft`: not a task.\n"
)


EDITOR = (
    "# Editor\n\nEdit well.\n\n## Tasks\n\n"
    + INTRO.format("long-note")
    + "- `long-note` (`<tasks>/long-note.md`): a long note; always; heavy.\n"
)


FILES = {
    "roles/writer.md": WRITER,
    "roles/editor.md": EDITOR,
    "tasks/short-note.md": "# Short Note\n\nWrite a note.\n",
    "tasks/long-note.md": "# Long Note\n\nWrite a long note.\n",
    "templates/note.md": "# Note: [Title]\n",
    "methods/deep.md": "# Deep\n",
}


POET = (
    "# Poet\n\nRhyme.\n\n## Tasks\n\n"
    + INTRO.format("verse")
    + "- `verse` (`<tasks>/verse.md`): a verse; always; light.\n"
)


STEPS = "\n## Steps\n\n1. **Report progress**:\n   [ensemble-progress:start] the topic.\n\n## Resume\n\nGo on.\n"


VERSE = "# Verse\n" + STEPS


RUN_CONFIG = """\
tier = 2
effort = "high"
client = "a"
idle_timeout = 60
show = ""

[output]
type = "local"

[roles.writer]
effort = "medium"
templates = ["note"]
read = ["repo"]

[roles.editor]
tier = 1

[roles.editor.output]
type = "github"
repo = "o/notes"
branch = "main"
"""


def run_config(**changes) -> RunConfig:
    """A writer's RunConfig: tier 2, effort high, client `a`, local output; `changes` replace."""
    return RunConfig(
        **{
            "role": "writer",
            "tier": 2,
            "effort": "high",
            "client": "a",
            "output": {"type": "local"},
            "idle_timeout": 60,
            "show": "",
            **changes,
        }
    )


class FakeTeam(Case):
    """A team root of `files`, config of `config_text` and local file in a temp dir; a HOME of the
    test's own for team dirs.
    """

    files = FILES
    config_text = RUN_CONFIG

    def setUp(self):
        self.tmp = scratch(self, real=False)
        self.root = os.path.join(self.tmp, "team")
        self.config = os.path.join(self.tmp, "config.toml")
        self.local_file = os.path.join(self.tmp, "config.local.toml")
        self.write(self.files)
        self.configure(self.config_text)

    def write(self, files, top=None):
        for rel, text in files.items():
            path = os.path.join(top or self.root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(text)

    def configure(self, text):
        with open(self.config, "w") as f:
            f.write(text)

    def local(self, text):
        with open(self.local_file, "w") as f:
            f.write(local_section(text, "delegate"))
