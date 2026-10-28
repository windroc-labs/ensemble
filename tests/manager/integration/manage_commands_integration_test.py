"""skills/manage/SKILL.md's layer command examples against the real bin/ commands:
each subcommand's --help exits 0 and lists every flag the example writes; a misspelt one does not.
"""

import os
import re
import subprocess

from tests.manager.case import Case, child_env
from tests.manager.skills.commands import BIN, body, examples, flags, subcommand


class Examples(Case):
    def help(self, command: str, sub: tuple[str, ...]) -> str | None:
        result = subprocess.run(
            (os.path.join(BIN, command), *sub, "--help"),
            cwd=self.home,
            env=child_env(),
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        return result.stdout if result.returncode == 0 else None

    def refused(self, source: str) -> list[tuple[str, ...]]:
        """Each (command, subcommand[, flag]) of source's examples its --help does not take."""
        helps, found = {}, []
        for command in examples(source):
            key = command[0], *subcommand(command)
            if key not in helps:
                helps[key] = self.help(key[0], key[1:])
            if helps[key] is None:
                found.append(key)
                continue
            found.extend(
                (*key, flag)
                for flag in flags(command)
                if not re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", helps[key])
            )
        return found

    def test_every_example_names_a_subcommand_and_flags_its_command_takes(self):
        self.assertEqual(self.refused(body("manage")), [])

    def test_a_misspelt_subcommand_or_flag_is_refused(self):
        source = "`ensemble-crew close-all --dryrun` and `ensemble-session sendkeys w`"
        self.assertEqual(
            self.refused(source),
            [("ensemble-crew", "close-all", "--dryrun"), ("ensemble-session", "sendkeys")],
        )
