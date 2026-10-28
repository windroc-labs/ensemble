"""Installer JSONC edits, without launching tools or touching credentials."""

import io
import json
import os
import shutil
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from tests.case import Case
from tests.unit.install_script import program


class OpenCodeConfigTest(Case):
    def setUp(self):
        self.conf = Path(self.home, "config")
        self.conf.mkdir()
        self.state = Path(self.home, "state")
        self.state.mkdir()
        self.path = self.conf / "opencode.jsonc"

    def edit(self, *args):
        output = io.StringIO()
        with mock.patch.object(sys, "argv", ["-c", *map(str, args)]), redirect_stdout(output):
            exec(program("opencode_config_edit"), {})
        return output.getvalue().strip()

    def plan(self):
        return self.edit("plan", self.conf, self.state, "lan", "http://localhost/v1", "m", "65536")

    def add(self):
        target = self.plan()
        self.assertEqual(target, str(self.path))
        shutil.copyfile(self.state / "opencode-provider.plan", target)
        return json.loads(self.path.read_text())["provider"]["lan"]

    def remove(self):
        self.edit("remove-provider", self.path, self.state, "lan")

    def test_new_provider_and_budget(self):
        value = self.add()
        self.assertEqual(value["npm"], "@ai-sdk/openai-compatible")
        self.assertEqual(value["options"], {"baseURL": "http://localhost/v1"})
        self.assertEqual(value["models"]["m"]["name"], "m")
        self.assertEqual(
            value["models"]["m"]["limit"], {"context": 65536, "input": 32768, "output": 16384}
        )
        before = self.path.read_bytes()
        self.assertEqual(self.plan(), "")
        self.assertEqual(self.path.read_bytes(), before)

    def test_jsonc_comments_strings_trailing_commas_and_other_provider_survive(self):
        original = '{\n// keep\n"theme": "a,} // \\"", "provider": {"mine": {},},\n}\n'
        self.path.write_text(original)
        self.plan()
        shutil.copyfile(self.state / "opencode-provider.plan", self.path)
        after = self.path.read_text()
        self.assertIn('// keep\n"theme": "a,} // \\""', after)
        self.assertIn('"mine": {}', after)
        self.remove()
        self.assertIn('"mine": {}', self.path.read_text())
        self.assertNotIn('"lan"', self.path.read_text())

    def test_existing_provider_in_either_file_is_untouched(self):
        for filename in ("opencode.json", "opencode.jsonc"):
            with self.subTest(filename=filename):
                path = self.conf / filename
                path.write_text('{"provider": {"lan": {"name": "mine"}}}')
                self.assertEqual(self.plan(), "")
                self.assertEqual(path.read_text(), '{"provider": {"lan": {"name": "mine"}}}')
                self.assertFalse((self.state / "opencode-provider-lan.json").exists())
                path.unlink()

    def test_existing_provider_with_both_global_files_is_not_owned(self):
        for selected in ("opencode.json", "opencode.jsonc"):
            with self.subTest(selected=selected):
                before = {}
                for filename in ("opencode.json", "opencode.jsonc"):
                    name = "lan" if filename == selected else "other"
                    text = json.dumps({"provider": {name: {"name": "mine"}}})
                    path = self.conf / filename
                    path.write_text(text)
                    before[path] = path.read_bytes()
                self.assertEqual(self.plan(), "")
                self.assertEqual({p: p.read_bytes() for p in before}, before)
                self.assertFalse((self.state / "opencode-provider-lan.json").exists())

    def test_plan_with_private_user_config_is_private_even_with_permissive_umask(self):
        self.path.write_text('{"provider":{"mine":{"options":{"apiKey":"private-fixture"}}}}')
        self.path.chmod(0o600)
        before = self.path.read_bytes()
        previous = os.umask(0o022)
        try:
            self.plan()
            plan = self.state / "opencode-provider.plan"
            self.assertEqual(plan.stat().st_mode & 0o777, 0o600)
            plan.chmod(0o644)
            self.plan()
            self.assertEqual(plan.stat().st_mode & 0o777, 0o600)
        finally:
            os.umask(previous)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_bom_and_duplicate_keys_preserve_text_and_use_last_provider(self):
        original = (
            '\ufeff{// keep\n"theme":"first","theme":"last",'
            '"provider":{"lan":{"name":"shadowed"}},"provider":{"mine":{}},}\n'
        )
        self.path.write_text(original)
        self.plan()
        shutil.copyfile(self.state / "opencode-provider.plan", self.path)
        text = self.path.read_text()
        self.assertTrue(text.startswith('\ufeff{// keep\n"theme":"first","theme":"last",'))
        self.assertIn('"lan": {', text)
        self.remove()
        self.assertIn('"provider":{"lan":{"name":"shadowed"}}', self.path.read_text())
        self.assertIn('"provider":{"mine":{}', self.path.read_text())
        self.assertNotIn('"lan": {', self.path.read_text())
        self.assertTrue(self.path.read_text().startswith("\ufeff{// keep\n"))

    def test_duplicate_provider_uses_last_value_for_existing_name(self):
        original = '{"provider":{},"provider":{"lan":{"name":"mine"}}}'
        self.path.write_text(original)
        self.assertEqual(self.plan(), "")
        self.assertEqual(self.path.read_text(), original)

    def test_provider_cleanup_preserves_config_symlink_and_target(self):
        for filename in ("opencode.json", "opencode.jsonc"):
            with self.subTest(filename=filename):
                self.path = self.conf / filename
                target = Path(self.home, filename)
                target.write_text('{"theme":"mine"}')
                self.path.symlink_to(target)
                self.add()
                self.assertTrue(self.path.is_symlink())
                self.assertIn("lan", json.loads(target.read_text())["provider"])
                self.remove()
                self.assertTrue(self.path.is_symlink())
                self.assertEqual(self.path.resolve(), target)
                self.assertEqual(json.loads(target.read_text()), {"theme": "mine"})
                self.path.unlink()

    def test_invalid_config_fails_without_writing_or_echoing_values(self):
        for text in (
            '{"private": "sensitive",',
            "[]",
            '{"provider": []}',
            '{"enabled_providers": ["other"]}',
            '{"disabled_providers": ["lan"]}',
        ):
            with self.subTest(text=text):
                self.path.write_text(text)
                with self.assertRaises(SystemExit) as caught:
                    self.plan()
                self.assertNotIn("sensitive", str(caught.exception))
                self.assertEqual(self.path.read_text(), text)
                self.assertFalse((self.state / "opencode-provider.plan").exists())

    def test_cleanup_preserves_other_settings_and_user_modified_provider(self):
        value = self.add()
        self.path.write_text(json.dumps({"provider": {"lan": value, "mine": {}}, "theme": "mine"}))
        self.remove()
        self.assertEqual(
            json.loads(self.path.read_text()), {"provider": {"mine": {}}, "theme": "mine"}
        )
        self.path.unlink()
        value = self.add()
        value["options"]["baseURL"] = "http://mine/v1"
        self.path.write_text(json.dumps({"provider": {"lan": value}}))
        before = self.path.read_bytes()
        self.remove()
        self.assertEqual(self.path.read_bytes(), before)

    def test_cleanup_removes_only_new_container(self):
        self.add()
        self.remove()
        self.assertFalse(self.path.exists())
        self.path.write_text('{"provider": {}}')
        self.plan()
        shutil.copyfile(self.state / "opencode-provider.plan", self.path)
        self.remove()
        self.assertEqual(json.loads(self.path.read_text()), {"provider": {}})

    def test_cleanup_keeps_added_comments_after_removing_new_provider(self):
        self.add()
        text = self.path.read_text().replace('"npm":', '/* provider note */ "npm":')
        self.path.write_text("// user note\n" + text)
        self.remove()
        self.assertTrue(self.path.exists())
        self.assertIn("// user note", self.path.read_text())
        self.assertIn("/* provider note */", self.path.read_text())
        self.assertNotIn('"lan"', self.path.read_text())

    def test_cleanup_write_error_hides_details(self):
        self.path.write_text('{"theme":"mine"}')
        self.add()
        before = self.path.read_bytes()
        with (
            mock.patch("os.open", side_effect=OSError("private detail")),
            self.assertRaises(SystemExit) as caught,
        ):
            self.remove()
        self.assertNotIn("private detail", str(caught.exception))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.conf.iterdir()), [self.path])

    def test_installer_and_cleanup_share_editor(self):
        self.assertEqual(
            program("opencode_config_edit"),
            program("opencode_config_edit", "install-cleanup.sh"),
        )
