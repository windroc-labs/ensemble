"""Repository-wide source policy tests."""

import glob
import os
import re
import tempfile
from pathlib import Path

from ensemble.delegate import composer
from ensemble.delegate.composer import Composer
from ensemble.delegate.config import CONFIG
from ensemble.delegate.event.outcome import SCHEMA
from tests.case import REPO, Case
from tests.delegate.unit.composer.prompts import composed
from tests.delegate.unit.composer.shipped import generic_names, runs, team_texts, words
from tests.delegate.verifier import VERIFIER
from tests.utils.case import local_section

TICKET = re.compile(r"TASK-\d+")

TICKET_EXAMPLES = {"principles.md": ["(e.g. `TASK-142`)"]}

WORDS = (
    "router.py",
    "research.py",
    "usage.py",
    "eng.py",
    "Linear",
    "In Review",
    "Todo",
    "Handoff",
    "issue",
)


def tickets(root):
    """
    {path relative to `root`: its ticket ids beyond TICKET_EXAMPLES} of each file under `root`.
    """
    found = {}
    for top, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in (n for n in files if not n.endswith(".pyc")):
            rel = os.path.relpath(os.path.join(top, name), root)
            with open(os.path.join(top, name), errors="replace") as f:
                text = f.read()
            for example in TICKET_EXAMPLES.get(rel, ()):
                text = text.replace(example, "", 1)
            if ids := TICKET.findall(text):
                found[rel] = ids
    return found


class DelegatePolicy(Case):
    def setUp(self):
        with open(os.path.join(self.ensemble, "config.local.toml"), "w") as f:
            f.write(local_section(VERIFIER, "delegate"))

    def test_tickets_finds_an_id_beyond_the_examples(self):
        with tempfile.TemporaryDirectory() as root:
            for rel, text in {
                "roles/x.md": "when asked, e.g. a wording tweak like TASK-300; light.",
                "principles.md": "the id (e.g. `TASK-142`); twice (e.g. `TASK-142`)",
                "output/output.md": "the id (e.g. `TASK-142`)",
            }.items():
                os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
                with open(os.path.join(root, rel), "w") as f:
                    f.write(text)
            self.assertEqual(
                tickets(root),
                {
                    "roles/x.md": ["TASK-300"],
                    "principles.md": ["TASK-142"],
                    "output/output.md": ["TASK-142"],
                },
            )

    def test_no_ticket_id_but_the_format_examples(self):
        team = Composer().team.root
        self.assertEqual(tickets(team), {})
        for rel, examples in TICKET_EXAMPLES.items():
            with open(os.path.join(team, rel)) as f:
                text = f.read()
            for example in examples:
                self.assertEqual(text.count(example), 1, (rel, example))
        with open(CONFIG) as f:
            self.assertEqual(TICKET.findall(f.read()), [])
        for role, task in runs():
            prompt = words(composed(role, task)[0])
            self.assertEqual(
                TICKET.findall(prompt.replace("(e.g. `TASK-142`)", "", 1)), [], (role, task)
            )

    def test_no_orchestration_or_tracker_words(self):
        texts = team_texts() | {
            f"{role} {task}": words(composed(role, task)[0]) for role, task in runs()
        }
        composer_py = glob.glob(os.path.join(os.path.dirname(composer.__file__), "*.py"))
        for path in [CONFIG, SCHEMA, *composer_py]:
            with open(path) as f:
                texts[path] = f.read()
        for label, text in texts.items():
            for word in WORDS:
                self.assertNotIn(word, text, f"{label}: {word}")

    def test_a_plugin_skill_without_its_slash_or_the_x_skill_is_caught_and_slash_commands_pass(
        self,
    ):
        for text, want in (
            (
                "run updraft:build\nthen `ensemble:manage`\nor x/ensemble:act-as",
                ["run updraft:build", "then `ensemble:manage`"],
            ),
            (
                "the act-as skill\nthe manage skill's workers\nthe skill",
                ["the act-as skill", "the manage skill's workers"],
            ),
            ("/updraft:build, `/ensemble:manage`'s workers, /deep-research", []),
        ):
            with self.subTest(text=text):
                self.assertEqual(generic_names(text), want)


class SessionPolicy(Case):
    def test_clients_contain_no_role_policy_names(self):
        root = Path(REPO) / "src" / "ensemble" / "session" / "clients"
        forbidden = ("role_policy", "permission-mode", "approval_policy", "approvals_reviewer")
        for path in root.rglob("*.py"):
            for name in forbidden:
                with self.subTest(path=path, name=name):
                    self.assertNotIn(name, path.read_text())

    def test_recovery_helpers_use_driver_selection_without_literal_builders(self):
        root = Path(REPO) / "src" / "ensemble" / "session" / "clients"
        for client in ("claude", "codex"):
            common = root / client / "common.py"
            paths = [common] if common.is_file() else (root / client / "common").rglob("*.py")
            source = "\n".join(path.read_text() for path in paths)
            with self.subTest(client=client):
                self.assertNotIn("--resume", source)
                self.assertNotIn('"resume"', source)
