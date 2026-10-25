"""A separately packaged Delegate runs and recovers through configuration in fresh processes."""

import json
import os
import subprocess
from pathlib import Path

from ensemble.utils import Record, Status
from tests.delegate.fakes import delegate as fake_delegate
from tests.orchestrator.case import PY, Case, child_env
from tests.orchestrator.config_fixtures import PIPELINE, ConfigFile
from tests.orchestrator.linear_run_case import SID

EXTENSION = """
from ensemble.session import Usage, Window

class ExtensionDelegate(FakeDelegate):
    ROLES = {
        "researcher": {"deep-research": "research"},
        "pm": {"product-design": "design"},
        "engineer": {"build": "build"},
    }

    @classmethod
    def usage(cls, role, *, overlays=()):
        if cls.__name__ != "ExtensionDelegate":
            raise AssertionError("saved usage provider was replaced by new default")
        if overlays[-1]["roles"][role]["client"] != "fake":
            raise AssertionError("saved client was replaced by role default")
        return Usage(Window(10, None), None)

    @classmethod
    def resolve(cls, role, task=None, *, overlays=()):
        cls._known(role)
        return RunConfig(
            role=role, task=task or "", tier=2, effort="high", client="fake",
            output={"type": "github", "repo": "acme/notes", "branch": "main", "dir": role + "/"},
            idle_timeout=60,
            show="",
        )

class NewDefault(ExtensionDelegate):
    @classmethod
    def attach(cls, record_or_workdir, *, sid=None):
        raise AssertionError("saved provider was replaced by new default")
"""


START = """import sys
from ensemble.orchestrator import LinearRun
from ensemble.orchestrator.engine import config, runs
issue, sid = sys.argv[1:]
cfg = config.load()
owner = LinearRun(issue, "Echo.", cfg.issue_workdir(issue), cfg=cfg, role="engineer", sid=sid, display="opaque display")
runs.note(cfg.runs_log, "start", issue=issue, sid=sid, role="engineer", client="fake", delegate_kind=cfg.delegate_kind)
print(owner.detach().to_line())
"""


CLI = (*PY, "-m", "ensemble.orchestrator.cli")


class Extension(ConfigFile, Case):
    def setUp(self):
        super().setUp()
        package = Path(self.dir) / "external_delegate"
        package.mkdir()
        source = Path(fake_delegate.__file__).read_text(encoding="utf-8")
        (package / "__init__.py").write_text(source + EXTENSION, encoding="utf-8")
        self.env = child_env(os.environ, self.dir)
        self.configure()

    def configure(self, default="extension"):
        pipeline = "\n".join(
            line for line in PIPELINE.splitlines() if not line.startswith("delegates = ")
        )
        self.local(
            f"work_dir = {json.dumps(self.dir + '/state')}\n"
            + pipeline
            + f'\n[delegates]\ndefault = "{default}"\n'
            + '[delegates.extension]\nimplementation = "external_delegate:ExtensionDelegate"\n'
            + '[delegates.new]\nimplementation = "external_delegate:NewDefault"\n'
        )

    def child(self, argv, *, stdin="", stderr=""):
        result = subprocess.run(
            argv,
            input=stdin,
            capture_output=True,
            text=True,
            env=self.env,
            cwd=self.dir,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, stderr)
        return result.stdout

    def start(self, issue):
        return Record.parse(self.child([*PY, "-c", START, issue, SID]))

    def test_separate_provider_creates_and_resumes_saved_kind_after_default_changes(self):
        record = self.start("TASK-1")
        self.assertEqual(
            (record.kind, record.name, record.display), ("pipeline", "TASK-1", "opaque display")
        )
        self.configure(default="new")
        output = self.child([*CLI, "resume", "TASK-1"])
        self.assertEqual(Record.parse(output), record)
        self.assertEqual(fake_delegate.FakeDelegate.starts(record.workdir), 1)
        ended = (
            "ensemble-orchestrator: TASK-1: run ended (done); nothing resumed; its conversation:"
            " ensemble-crew reopen TASK-1 (docs/sessions.md › Recall a closed run)\n"
        )
        for transition, starts, stderr in (("finish", 1, ended), ("end", 2, "")):
            getattr(fake_delegate.FakeDelegate, transition)(record.workdir)
            output = self.child([*CLI, "resume", "TASK-1"], stderr=stderr)
            self.assertEqual(Record.parse(output), record)
            self.assertEqual(fake_delegate.FakeDelegate.starts(record.workdir), starts)

    def test_cli_batch_status_and_resume_run_in_another_process(self):
        first, second = self.start("TASK-1"), self.start("TASK-2")
        for args, stdin in (
            (["status", "TASK-2", "TASK-1"], ""),
            (["status"], second.to_line() + "\n" + first.to_line() + "\n"),
        ):
            output = self.child([*CLI, *args], stdin=stdin)
            statuses = [Status.parse(line) for line in output.splitlines()]
            self.assertEqual([item.name for item in statuses], ["TASK-2", "TASK-1"])
            self.assertTrue(all(item.alive for item in statuses))
        output = self.child([*CLI, "resume", "TASK-1"])
        self.assertEqual(Record.parse(output), first)
