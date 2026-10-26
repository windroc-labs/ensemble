"""A new client is a config value to the Composer: a local `client` and its [clients.<name>]
table, with no package for it, resolve, compose and list the catalog in a child process
(child.Case); no client text in the prompt.
"""

import os
import unittest

from ensemble.delegate.composer.prompt import RETURN
from tests.delegate.case import ROOTS
from tests.delegate.integration.composer import child

BANNER = "Extension handover."
LOCAL = (
    '[delegate]\nclient = "extension"\n\n[session.clients.extension]\nimplementation = "extension_client:ExtensionClient"\n'
    f'tier = 4\nbanner = "{BANNER}"\n'
)


class ExtensionClient(child.Case):
    def test_its_client_resolves_composes_and_the_catalog_lists_every_role(self):
        self.local(LOCAL)
        got = self.compose("dummy-tester", "echo")
        self.assertFalse(got["repo_imported"])
        for path in got["files"]:
            self.assertTrue(path.startswith(tuple(r + os.sep for r in ROOTS)), path)
        self.assertEqual(
            (got["run"]["client"], got["run"]["tier"], got["run"]["effort"]),
            ("extension", 3, "low"),
        )
        self.assertNotIn(BANNER, got["prompt"])
        self.assertIn(f"\n{RETURN}\n", got["prompt"])
        self.assertIn("# Input\n\nHello.", got["prompt"])
        tiered = self.compose("dummy-tester", "echo", overlays=[{"tier": 4}])["run"]
        self.assertEqual((tiered["client"], tiered["tier"]), ("extension", 4))
        catalog = self.child("index")
        self.assertEqual(
            list(catalog), ["researcher", "pm", "engineer", "verifier", "dummy-tester"]
        )
        self.assertIn("echo", catalog["dummy-tester"])


if __name__ == "__main__":
    unittest.main()
