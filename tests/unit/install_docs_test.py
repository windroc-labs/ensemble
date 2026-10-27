"""The documented --password-stdin install commands (S2): checks of the docs' text, run by no installer."""

import re
from pathlib import Path

from tests.case import REPO, Case


def usage(name):
    """A script's usage text as it prints: its usage() heredoc, \\$ unescaped."""
    script = Path(REPO, "scripts", name).read_text()
    body = re.search(r"\nusage\(\) \{\n  cat <<EOF\n(.*?)\nEOF\n", script, re.S)
    return body.group(1).replace("\\$", "$") if body else ""


class PasswordStdinCommandsTest(Case):
    def test_s2_password_stdin_commands_keep_the_script_out_of_argv(self):
        for name, text in (
            ("README.md", Path(REPO, "README.md").read_text()),
            ("install.sh usage", usage("install.sh")),
            ("install-cleanup.sh usage", usage("install-cleanup.sh")),
        ):
            with self.subTest(name=name):
                commands = [
                    line
                    for line in text.splitlines()
                    if "--password-stdin" in line and "curl" in line
                ]
                self.assertTrue(commands, f"{name} documents no --password-stdin install command")
                for command in commands:
                    self.assertNotRegex(command, r"bash -c \"\$\(curl")
                    self.assertRegex(command, r"\| bash \"\$f\" ")
                    # A subshell whose trap removes the download keeps the installer's exit status.
                    self.assertRegex(
                        command, r"^\s*\(f=\$\(mktemp\) && trap 'rm -f \"\$f\"' EXIT && "
                    )
