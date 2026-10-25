"""Fake team files (roles, tasks) and a run config the composer and plan tests share."""

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
repo = "o/docs"
branch = "main"
"""
