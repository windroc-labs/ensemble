"""Fixed-path text fixtures for the pre-Composer prompt byte oracle."""

import io
import os
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from ensemble.delegate.composer import RunParams, Team
from ensemble.delegate.composer.base import TEAM
from ensemble.delegate.config import CONFIG
from ensemble.delegate.engine.config import read_config
from tests.delegate.unit.composer.shipped import catalog

ROOT = "/corpus/team"
CUSTOM = "/corpus/custom"
WORKDIR = "/corpus/work"
ORACLE = Path(__file__).with_name("composer_prompts.json")
INPUT = (
    "  # Input\n\n```markdown\n# Guide\n```\n---\n{{role}} [x](#missing) › literal\n中文\ttext\n  "
)


def corpus():
    cfg = read_config(path=CONFIG, local=os.devnull)
    cfg["roles"]["verifier"]["output"]["repo"] = "example/docs"  # as config.local.toml sets it
    files = {
        str(Path(ROOT) / path.relative_to(TEAM)): path.read_text(encoding="utf-8")
        for path in Path(TEAM).rglob("*.md")
    }
    files[f"{CUSTOM}/roles/corpus-poet.md"] = (
        "# Corpus Poet\n\nWrite a poem.\n\n## Tasks\n\n"
        "Pick the task below that fits the input; unsure → `corpus-verse` (`<tasks>/corpus-verse.md`).\n\n"
        "- `corpus-verse` (`<tasks>/corpus-verse.md`): a poem; always; light.\n"
    )
    files[f"{CUSTOM}/tasks/corpus-verse.md"] = (
        "# Corpus Verse\n\n## Steps\n\n"
        "1. **Report progress:**\n   [ensemble-progress:start] the subject\n"
        "2. Write.\n\n## Resume\n\nContinue.\n"
    )
    files[f"{CUSTOM}/templates/corpus-card.md"] = "# Card\n\n````markdown\n```\ntext\n```\n````\n"
    entries = {}
    for path in files:
        parent = Path(path).parent.name
        if parent in ("roles", "tasks", "templates"):
            entries[(parent, Path(path).stem)] = (path,)
    found = Team(ROOT, (CUSTOM,), entries)
    cases = {}
    for role, tasks in catalog().items():
        for task in (None, *tasks):
            for resume in (False, True):
                for language in ("English", ""):
                    key = f"{role}/{task or 'default'}/{'resume' if resume else 'fresh'}/{'language' if language else 'no-language'}"
                    cases[key] = (
                        role,
                        task,
                        ({"language": language},),
                        RunParams(
                            input=INPUT, out=f"{WORKDIR}/out.md", workdir=WORKDIR, resume=resume
                        ),
                    )
    for destination in ("local", "github", "pull-request"):
        output = {"type": destination}
        if destination == "github":
            output |= {
                "repo": "example/docs",
                "branch": "main",
                "dir": "Notes/",
                "host": "ghe.example.test",
            }
        cases[f"destination/{destination}"] = (
            "researcher",
            "research",
            ({"output": output},),
            RunParams(input=INPUT, out="/corpus/external/out.md", workdir=WORKDIR),
        )
    for out in (
        "",
        f"{WORKDIR}/nested/out.md",
        "/corpus/external/out.md",
        f"{WORKDIR}/escape/out.md",
    ):
        cases[f"deliverable/{out or 'absent'}"] = (
            "corpus-poet",
            "corpus-verse",
            (),
            RunParams(input=INPUT, out=out, workdir=WORKDIR),
        )
    for task in (None, "corpus-verse"):
        cases[f"custom/{task or 'default'}"] = (
            "corpus-poet",
            task,
            ({"templates": ["corpus-card"]},),
            RunParams(input=INPUT, out=f"{WORKDIR}/out.md", workdir=WORKDIR, resume=True),
        )
    return cfg, found, files, cases


@contextmanager
def supplied_text(files):
    original = open
    realpath = os.path.realpath

    def read(path, *args, **kwargs):
        if os.fspath(path) in files:
            return io.StringIO(files[os.fspath(path)])
        return original(path, *args, **kwargs)

    with (
        mock.patch("builtins.open", side_effect=read),
        mock.patch("os.path.isfile", side_effect=lambda path: os.fspath(path) in files),
        mock.patch(
            "os.path.realpath",
            side_effect=lambda path: realpath(
                os.fspath(path).replace(f"{WORKDIR}/escape", "/corpus/external")
            ),
        ),
    ):
        yield
