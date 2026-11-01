"""The upgrade doc's cleanup steps, run in a temporary home with a fake security command."""

import json
import os
import re
import subprocess
import tempfile
import textwrap
from pathlib import Path

from tests.case import REPO, Case

DOC = Path(REPO, "docs", "upgrading.md")
KEYCHAIN = (
    "security delete-generic-password -s ensemble-broker-claude -a default"
    " ~/Library/Keychains/login.keychain-db"
)


def block(marker: str) -> str:
    """The one shell block of the upgrade doc holding marker."""
    blocks = re.findall(r"```sh\n(.*?)```", DOC.read_text(), re.S)
    (found,) = [textwrap.dedent(b) for b in blocks if marker in b]
    return found


def cleanup() -> str:
    """The shell block that deletes ~/.ensemble/usage."""
    return block("~/.ensemble/usage")


class UsageCleanupTest(Case):
    def run_in(self, home):
        subprocess.run(
            ["sh", "-c", cleanup()],
            env={"HOME": home, "PATH": "/usr/bin:/bin"},
            capture_output=True,
            check=False,
        )

    def test_only_old_readings_holds_and_their_locks_go(self):
        usage = ("claude.json", "claude.json.lock", "claude-holds.json", "claude-holds.json.lock")
        kept = (
            ".ensemble/usage/notes.txt",
            ".ensemble/config.local.toml",
            ".claude-work/.credentials.json",
        )
        for extra in (kept, ()):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as home:
                for rel in (*(f".ensemble/usage/{name}" for name in usage), *extra):
                    os.makedirs(os.path.dirname(os.path.join(home, rel)), exist_ok=True)
                    Path(home, rel).write_text("x")
                self.run_in(home)
                for name in usage:
                    self.assertFalse(Path(home, ".ensemble/usage", name).exists())
                for rel in extra:
                    self.assertTrue(Path(home, rel).exists())
                self.assertEqual(Path(home, ".ensemble/usage").exists(), bool(extra))


class BrokerCleanupTest(Case):
    """The named-accounts upgrade's broker steps, in the doc's order, on fake state only."""

    def test_steps_are_in_upgrade_order(self):
        text = DOC.read_text()
        section = text.split("## Broker protocol 38", 1)[1].split("\n## ", 1)[0]
        order = [
            "finish or close every running worker",
            "[session.clients.claude.accounts.work]",
            "Install the matching plugin and broker",
            "ensemble-broker login claude work",
            KEYCHAIN,
            "static-secrets.json",
            "Resume scheduling",
        ]
        found = [section.find(step) for step in order]
        self.assertNotIn(-1, found)
        self.assertEqual(found, sorted(found))
        self.assertIn("max_usage = 80", section)

    def test_old_default_credential_and_record_go_and_named_state_stays(self):
        with tempfile.TemporaryDirectory() as home:
            bin_dir = Path(home, "fake-bin")
            bin_dir.mkdir()
            calls = Path(home, "security-calls")
            fake = bin_dir / "security"
            fake.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> {calls}\n')
            fake.chmod(0o755)
            store = Path(home, ".ensemble/docker/static-secrets.json")
            store.parent.mkdir(parents=True)
            named = {
                "ensemble-broker-claude/work": {
                    "version": 2,
                    "expires_at": "2027-10-01T00:00:00Z",
                    "provenance": "explicit",
                },
                "ensemble-broker-env/db": {"version": 1, "provenance": "unknown"},
            }
            old = {"version": 3, "expires_at": "2026-11-15T00:00:00Z", "provenance": "assumed"}
            secrets = {"ensemble-broker-claude/default": {**old, "notified": ["30d"]}, **named}
            store.write_text(json.dumps({"secrets": secrets}))
            store.chmod(0o600)
            usage = Path(home, ".ensemble/usage")
            usage.mkdir()
            for name in ("claude.json", "claude.json.lock", "claude-holds.json"):
                Path(usage, name).write_text("{}")
            kept = (".claude-work/.credentials.json", ".codex/auth.json")
            for rel in kept:
                Path(home, rel).parent.mkdir(parents=True, exist_ok=True)
                Path(home, rel).write_text("x")
            env = {"HOME": home, "PATH": f"{bin_dir}:/usr/bin:/bin"}
            for marker in (KEYCHAIN, "static-secrets.json", "~/.ensemble/usage"):
                result = subprocess.run(
                    ["sh", "-c", block(marker)], env=env, capture_output=True, check=False
                )
                if marker != "~/.ensemble/usage":
                    self.assertEqual(result.returncode, 0, result.stderr)
            keychain = f"{home}/Library/Keychains/login.keychain-db"
            self.assertEqual(
                calls.read_text().splitlines(),
                [f"delete-generic-password -s ensemble-broker-claude -a default {keychain}"],
            )
            self.assertEqual(json.loads(store.read_text()), {"secrets": named})
            self.assertEqual(store.stat().st_mode & 0o777, 0o600)
            self.assertFalse(usage.exists())
            for rel in kept:
                self.assertTrue(Path(home, rel).exists(), rel)

    def test_the_record_cleanup_is_idempotent_and_leaves_no_store_behind(self):
        with tempfile.TemporaryDirectory() as home:
            env = {"HOME": home, "PATH": "/usr/bin:/bin"}
            run = ["sh", "-c", block("static-secrets.json")]
            self.assertEqual(subprocess.run(run, env=env, check=False).returncode, 0)
            self.assertFalse(Path(home, ".ensemble").exists())
            store = Path(home, ".ensemble/docker/static-secrets.json")
            store.parent.mkdir(parents=True)
            text = json.dumps({"secrets": {"ensemble-broker-env/db": {"version": 1}}})
            store.write_text(text)
            for _ in range(2):
                self.assertEqual(subprocess.run(run, env=env, check=False).returncode, 0)
                self.assertEqual(store.read_text(), text)
