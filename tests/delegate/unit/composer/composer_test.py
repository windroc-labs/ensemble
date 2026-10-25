"""Composer owns its configuration snapshot and exposes the team boundary."""

import io
from dataclasses import replace
from unittest import mock

from ensemble.delegate.composer import Composer, ConfigError, RunParams, Team
from tests.delegate.case import Case

ROOT = "/corpus/team"
TEXTS = {
    f"{ROOT}/roles/writer.md": (
        "# Writer\n\nWrite well.\n\n## Tasks\n\n"
        "Pick what fits; unsure → `note` (`<tasks>/note.md`).\n\n"
        "- `note` (`<tasks>/note.md`): a note; always; light.\n"
    ),
    f"{ROOT}/tasks/note.md": "# Note\n\n## Steps\n\nWrite.\n\n## Resume\n\nContinue.\n",
    f"{ROOT}/guide.md": "# Guide\n\nYou are {{role}}. Task: {{task|pick one}}.\n",
    f"{ROOT}/principles.md": "# Principles\n\nUse {{language}}.\n",
    f"{ROOT}/output/output.md": "# Output\n\nReturn it.\n",
    f"{ROOT}/output/destinations/local.md": "## Destination\n\nSave {{deliverable}}.\n",
}
CONFIG = {
    "tier": 2,
    "effort": "high",
    "idle_timeout": 60,
    "show": "",
    "client": "client-a",
    "language": "English",
    "output": {"type": "local"},
    "roles": {"writer": {"tier": 1, "read": ["/operator/read"]}},
    "trusted_dirs": ["/operator/trusted"],
}
TEAM = Team(
    ROOT,
    files={
        (p.split("/")[-2], p.split("/")[-1][:-3]): (p,)
        for p in TEXTS
        if p.split("/")[-2] in ("roles", "tasks")
    },
)
PARAMS = RunParams(input="  # Literal\n\n```\n{{role}}\n```  ", out="/work/out.md", workdir="/work")


class OwnedComposer(Case):
    def setUp(self):
        self.enterContext(
            mock.patch("builtins.open", side_effect=lambda path: io.StringIO(TEXTS[path]))
        )
        self.enterContext(mock.patch("os.path.isfile", side_effect=lambda path: path in TEXTS))
        self.composer = Composer((), CONFIG, team=TEAM)

    def test_snapshot_is_deep_and_instances_and_results_are_independent(self):
        original = {
            "tier": 2,
            "effort": "high",
            "idle_timeout": 60,
            "show": "",
            "client": "client-a",
            "roles": {"writer": {"read": ["/read/a"]}},
            "output": {"type": "local"},
        }
        first = Composer((), original, team=TEAM)
        original["roles"]["writer"]["read"].append("/read/b")
        original["output"]["type"] = "orchestrator"
        second = Composer((), original, team=TEAM)
        self.assertEqual(first.resolve("writer").read, ["/read/a"])
        self.assertEqual(first.resolve("writer").output, {"type": "local"})
        self.assertEqual(second.resolve("writer").read, ["/read/a", "/read/b"])
        returned = first.resolve("writer")
        returned.read.append("/read/c")
        returned.output["type"] = "github"
        self.assertEqual(first.resolve("writer").read, ["/read/a"])
        self.assertEqual(first.resolve("writer").output, {"type": "local"})

    def test_overlays_replace_by_layer_without_accumulating(self):
        first = self.composer.resolve(
            "writer",
            "note",
            overlays=(
                {"roles": {"writer": {"tier": 3, "read": ["/first"]}}},
                {"tier": 4, "read": ["/last"]},
            ),
        )
        second = self.composer.resolve("writer")
        self.assertEqual((first.tier, first.read, first.task), (4, ["/last"], "note"))
        self.assertEqual((second.tier, second.read, second.task), (1, ["/operator/read"], ""))
        with self.assertRaisesRegex(ConfigError, "unknown key 'trusted_dirs'"):
            self.composer.resolve("writer", overlays=({"trusted_dirs": ["/extra"]},))

    def test_index_returns_exact_order_and_independent_maps(self):
        expected = {"writer": {"note": "a note; always; light."}}
        self.assertEqual(self.composer.index(), expected)
        result = self.composer.index()
        result["writer"]["note"] = "changed"
        result["invented"] = {}
        self.assertEqual(self.composer.index(), expected)
        self.assertEqual(self.composer.index("writer"), expected["writer"])
        self.assertEqual(self.composer.trusted_dirs(), frozenset({"/operator/trusted"}))

    def test_render_keeps_placement_resume_and_literal_input(self):
        prompt = self.composer.render(self.composer.resolve("writer", "note"), PARAMS)
        self.assertTrue(
            prompt.startswith("# Guide\n\nYou are Writer. Task: `note` (`<tasks>/note.md`).")
        )
        self.assertIn("Save <Workdir>/out.md.", prompt)
        self.assertTrue(prompt.endswith("\n# Input\n\n# Literal\n\n```\n{{role}}\n```\n"))
        run = self.composer.resolve("writer")
        resumed = self.composer.render(run, replace(PARAMS, resume=True, out="/external/out.md"))
        self.assertTrue(resumed.startswith("Resumed agent run after an interruption."))
        self.assertIn("Save <Workdir>/tmp/deliverable.md.", resumed)
        no_language = self.composer.render(replace(run, language=""), PARAMS)
        self.assertNotIn("Use English.", no_language)

    def test_unknown_role_task_and_bad_unused_custom_text_stay_selective(self):
        with self.assertRaisesRegex(ConfigError, "unknown role 'missing'"):
            self.composer.resolve("missing")
        with self.assertRaisesRegex(ConfigError, "task 'missing' is not one of writer's tasks"):
            self.composer.resolve("writer", "missing")
        found = replace(
            TEAM,
            dirs=("/custom",),
            files=dict(TEAM.files) | {("roles", "broken"): ("/custom/roles/broken.md",)},
        )
        composer = Composer((), CONFIG, team=found)
        self.assertEqual(composer.resolve("writer").role, "writer")
