"""A new client is one importable module plus one [clients.<name>] table naming it as
module.path:Class: a temp copy of the session and utils packages, the fake client's package copied in outside
them as module extension_client (a name unrelated to the client's) and its table appended to the
copy's ensemble/session/config.toml, nothing else changed, runs a headless session through Session.start in a
child Python whose PYTHONPATH is only the copy's site directory and that module's dir. A table without its
module, or a module without its table, fails there before anything starts.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest

from tests.session import fake_client
from tests.session.case import PY, REPO, Case, child_env

TIMEOUT = 60  # seconds
SKIP = shutil.ignore_patterns("__pycache__")
PROJECTS = ("session", "utils")
MODULE = "extension_client"
# The child: configures the session from the copy's session config, starts client argv[2] headless
# in workdir argv[1] and prints its events; a ConfigError exits 1, its text on stderr.
CHILD = """import dataclasses, json, sys
from ensemble.session import Access, ConfigError, Mode, Session, Spec
from ensemble.session.engine.config import read_config
try:
    Session.configure_from(read_config())
    handle = Session.start(
        Spec(workdir=sys.argv[1], access=Access(cwd=sys.argv[1]), client=sys.argv[2],
             mode=Mode.HEADLESS, prompt="hello")
    )
except ConfigError as e:
    sys.exit(f"ConfigError: {e}")
events = [[type(e).__name__, dataclasses.asdict(e)] for e in handle.events()]
print(json.dumps({"sid": handle.sid, "events": events,
                  "files": [sys.modules[type(handle).__module__].__file__,
                            sys.modules["extension_client"].__file__]}))
"""
INSTALLED_CHILD = """import json, sys
from unittest import mock
from ensemble.session import Access, Spec
from ensemble.session.engine.config import read_config
from ensemble.session.engine.hosted_session import HostedSession
HostedSession.configure_from(read_config())
client = HostedSession._config().registry.client("codex")
spec = Spec(workdir=sys.argv[1], sid="installed-layout", access=Access(cwd=sys.argv[1], project=True))
with mock.patch("shutil.which", return_value="/protected/codex"):
    launch = client.tui().launch(spec, (sys.executable, "-I", "/protected/hook.py"))
print(json.dumps({"argv": launch.argv[1:-1], "pythonpath": launch.env.get("PYTHONPATH"),
                  "plan": json.loads(launch.argv[-1])}))
"""


class ExtensionClientIntegration(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("ENSEMBLE_TEST_TMP", REPO))
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.site = os.path.join(self.root, "site")
        self.src = os.path.join(self.site, "ensemble", "session")
        self.extensions = os.path.join(self.root, "extensions")
        self.package(MODULE)
        self.package("untabled")
        for project in PROJECTS:
            shutil.copytree(
                os.path.join(REPO, "src", "ensemble", project),
                os.path.join(self.site, "ensemble", project),
                ignore=SKIP,
                dirs_exist_ok=True,
            )
        self.srcs = os.pathsep.join((self.site, self.extensions))
        self.config = os.path.join(self.src, "config.toml")
        os.chmod(self.config, 0o600)
        self.home = os.path.join(self.root, "home")
        os.mkdir(self.home)
        self.temp = os.path.join(self.root, "tmp")
        os.mkdir(self.temp)
        self.work = os.path.join(self.root, "work")
        scenario = os.path.join(self.root, "scenario.json")
        with open(scenario, "w", encoding="utf-8") as f:
            json.dump(
                [
                    {"out": {"type": "session", "id": "native-1"}},
                    {"out": {"type": "message", "text": "hi"}},
                    {"out": {"type": "result", "text": "done"}},
                ],
                f,
            )
        self.table(
            f'[clients.fake]\nimplementation = "{MODULE}:FakeClient"\n'
            f"scenario = {json.dumps(scenario)}\nlog = {json.dumps(os.path.join(self.root, 'log.jsonl'))}\n"
        )

    def package(self, name: str) -> None:
        """The fake client's package copied in as top-level package <name>, outside session."""
        shutil.copytree(
            os.path.dirname(fake_client.__file__),
            os.path.join(self.extensions, name),
            ignore=SKIP,
        )

    def table(self, text: str) -> None:
        with open(self.config, "a", encoding="utf-8") as f:
            f.write("\n" + text)

    def child(self, client: str, *, installed: bool = False) -> subprocess.CompletedProcess:
        script = CHILD
        if installed:
            script = INSTALLED_CHILD
        return subprocess.run(
            [*PY, "-c", script, self.work, client, self.site],
            cwd=self.root,
            env={
                **child_env({**os.environ, "HOME": self.home, "TMPDIR": self.temp}),
                "PYTHONPATH": self.srcs,
            },
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )

    def refused(self, client: str, error: str) -> None:
        """The child starting `client` exits 1 on ConfigError `error`, before any run record or
        process.
        """
        child = self.child(client)
        self.assertEqual((child.returncode, child.stdout), (1, ""), child.stderr)
        self.assertIn(f"ConfigError: {error}", child.stderr)
        self.assertFalse(os.path.exists(self.work))

    def test_a_copied_client_package_and_its_table_run_a_headless_session(self):
        child = self.child("fake")
        self.assertEqual(child.returncode, 0, child.stderr)
        data = json.loads(child.stdout)
        hosted, module = data["files"]
        self.assertTrue(hosted.startswith(self.src + os.sep), hosted)
        self.assertTrue(module.startswith(os.path.join(self.extensions, MODULE) + os.sep), module)
        self.assertEqual([name for name, _ in data["events"]], ["Reply", "TurnEnd", "Exited"])
        self.assertEqual(data["events"][-1], ["Exited", {"returncode": 0}])
        with open(os.path.join(self.work, "run.jsonl"), encoding="utf-8") as f:
            written = [json.loads(line) for line in f]
        self.assertEqual(
            [(e["kind"], e["sid"]) for e in written],
            [("session", data["sid"]), ("client_session", data["sid"])],
        )

    def test_installed_codex_starts_its_module_with_its_site_as_the_only_import_root(self):
        child = self.child("codex", installed=True)
        self.assertEqual(child.returncode, 0, child.stderr)
        launched = json.loads(child.stdout)
        self.assertEqual(
            launched["argv"], ["-P", "-s", "-m", "ensemble.session.clients.codex.tui.start"]
        )
        self.assertEqual(launched["pythonpath"], self.site)
        self.assertNotIn("protect", launched["plan"])

    def test_a_table_without_its_module_fails_before_launch(self):
        self.table('[clients.absent]\nimplementation = "absent_client:AbsentClient"\n')
        self.refused(
            "absent",
            "[clients.absent].implementation: absent_client:AbsentClient: cannot import "
            "absent_client: No module named 'absent_client'",
        )

    def test_a_module_without_its_table_fails_before_launch(self):
        self.assertTrue(os.path.isdir(os.path.join(self.extensions, "untabled")))
        self.refused("untabled", "no [clients.untabled] table")


if __name__ == "__main__":
    unittest.main()
