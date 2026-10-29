import json

from tests.guard_checks import plugin
from tests.guard_samples import Sample

CLAUDE, CODEX, OPENCODE = ".claude-plugin/plugin.json", ".codex-plugin/plugin.json", "package.json"


class PluginManifests(Sample):
    SKILLS = ["./skills/"]

    def manifest(self, **fields):
        return json.dumps({"name": "ensemble", "version": "1.0.0", "skills": self.SKILLS, **fields})

    def shipped(self, files=()):
        files = {
            CLAUDE: self.manifest(),
            CODEX: self.manifest(),
            OPENCODE: self.manifest(),
            "skills/a/SKILL.md": "",
            "skills/m/SKILL.md": "",
            "skills/t/scripts/ensemble": "",
            **dict(files),
        }
        return self.tree({rel: text for rel, text in files.items() if text is not None})

    def test_both_manifests_must_ship_every_project_skill_under_one_name_and_version(self):
        for name, files, expected in (
            ("every project skill", {}, []),
            (
                "a single skills path string",
                {
                    CLAUDE: self.manifest(skills="./skills/"),
                    CODEX: self.manifest(skills="./skills/"),
                    OPENCODE: self.manifest(skills="./skills/"),
                    "skills/a/SKILL.md": "",
                    "skills/m/SKILL.md": None,
                },
                [],
            ),
            ("a missing manifest", {CODEX: None}, [f"{CODEX}: missing"]),
            ("a missing OpenCode manifest", {OPENCODE: None}, [f"{OPENCODE}: missing"]),
            (
                "an OpenCode manifest of another version",
                {OPENCODE: self.manifest(version="0.9.0")},
                [f'{OPENCODE}: version "0.9.0", not {CLAUDE}\'s "1.0.0"'],
            ),
            (
                "a missing project skill",
                {CODEX: self.manifest(skills=[])},
                [
                    f"{CODEX}: skills miss skills/a/SKILL.md",
                    f"{CODEX}: skills miss skills/m/SKILL.md",
                ],
            ),
            (
                "a skill outside the projects",
                {CODEX: self.manifest(skills=[*self.SKILLS, "./extra/"]), "extra/x/SKILL.md": ""},
                [f"{CODEX}: skills ship extra/x/SKILL.md, not a plugin skill"],
            ),
            (
                "a path not relative to the plugin root",
                {CODEX: self.manifest(skills=["skills/"])},
                [
                    f'{CODEX}: skills path "skills/" does not start with ./',
                    f"{CODEX}: skills miss skills/a/SKILL.md",
                    f"{CODEX}: skills miss skills/m/SKILL.md",
                ],
            ),
            (
                "another name or version",
                {CODEX: self.manifest(name="other", version="0.9.0")},
                [
                    f'{CODEX}: name "other", not {CLAUDE}\'s "ensemble"',
                    f'{CODEX}: version "0.9.0", not {CLAUDE}\'s "1.0.0"',
                ],
            ),
        ):
            with self.subTest(name):
                self.assertEqual(plugin.plugin_manifests(self.shipped(files)), expected)


class PluginSkills(Sample):
    def test_a_missing_extra_or_repeated_skill(self):
        root = self.tree(
            {
                "skills/manage/SKILL.md": "",
                "skills/extra/SKILL.md": "",
            }
        )
        self.assertEqual(
            plugin.plugin_skills(root),
            [
                "skill act-as: missing",
                "skill board: missing",
                "skill extra: not one of act-as, board, manage",
            ],
        )


class PluginCommandTemplates(Sample):
    def test_shell_and_positional_placeholders_in_a_body_are_found(self):
        head = '---\nname: m\ndescription: "run !`x` with $1"\n---\n'
        root = self.tree(
            {
                "skills/m/SKILL.md": head + "Run `!` first, then `ls`.\nTake $2.\n",
                "skills/a/SKILL.md": head + "Arguments: $ARGUMENTS, $HOME, 1$\n",
            }
        )
        self.assertEqual(
            plugin.plugin_command_templates(root),
            [
                "skills/m/SKILL.md: !` runs a shell command in an OpenCode command template",
                "skills/m/SKILL.md: $2 is an OpenCode command argument placeholder",
            ],
        )
