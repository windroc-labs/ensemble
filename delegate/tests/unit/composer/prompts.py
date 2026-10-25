"""Real-team prompt helpers the prompt and real-team tests share: RUN's prompt of a role (composed),
its sections and reference sources.
"""

import os
import re
from dataclasses import replace

from ensemble.delegate.composer import Composer, RunParams
from ensemble.delegate.composer.base import METHOD, TEAM, outline
from ensemble.delegate.composer.teams import index

RUN = RunParams(
    input="Research X.", out="/runs/W-1/out.md", workdir="/runs/W-1"
)  # a workdir no other text holds


def parameters(prompt):
    """{name: value} of the prompt's # Parameters lines after RULE, in order; a line of another
    shape fails.
    """
    section_ = re.search(r"^# Parameters\n\n(.*?)\n(?=# )", prompt, re.M | re.S).group(1)
    lines = section_.split("\n\n", 1)[1].splitlines()
    return dict(re.fullmatch(r"- `([^`]+)`: (.+)", line).groups() for line in lines)


def own_text(prompt):
    """`prompt` up to and including its # Input heading line: the input text, always last, is
    dropped.
    """
    return prompt.split("\n# Input\n\n", 1)[0] + "\n# Input\n"


def heading_anchors(prompt):
    """The anchor of each heading of the prompt's own text, in order."""
    return [a for _, _, a, heading in outline(own_text(prompt)) if heading]


def composed(role, task=None, overlays=(), **changes):
    """The real core's prompt for `role` (and `task`) with RUN, its run changed by `changes`, and
    that run.
    """
    composition = Composer()
    run = replace(composition.resolve(role, task, overlays=overlays), **changes)
    return composition.render(run, RUN), run


def task_text(task):
    with open(os.path.join(TEAM, "tasks", f"{task}.md")) as f:
        return f.read()


def method(name):
    with open(os.path.join(TEAM, "methods", f"{name}.md")) as f:
        return f.read()


def role_sources(role, task=None):
    """What `reference_errors` takes for `role` (and `task`): the real prompt's own text, the role's
    task files and the method files these name.
    """
    own = own_text(composed(role, task)[0])
    tasks = {t: task_text(t) for t in index(role)}
    named = sorted(set(METHOD.findall(own + "".join(tasks.values()))))
    return own, tasks, {f"methods/{m}.md": method(m) for m in named}


def rule(text, name):
    return [line for line in text.splitlines() if line.startswith(f"- **{name}**:")]


def section(text, name):
    m = re.search(rf"^## {name}\n(.*?)(?=^#)", text, re.M | re.S)
    return m.group(1) if m else ""


def item(text, name):
    """The top-level bold item `- **name**` of `text`, up to the next top-level item."""
    m = re.search(rf"^- \*\*{re.escape(name)}\*\*(.*?)(?=^- |\Z)", text, re.M | re.S)
    return m.group(1) if m else ""
