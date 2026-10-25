"""Configured fake run fixtures, shared by LinearRun and CLI tests."""

import json
from dataclasses import replace

from ensemble.orchestrator.engine import config, runs
from ensemble.orchestrator.engine.linear_run import LinearRun
from ensemble.session import Usage
from tests.delegate.fakes.delegate import FakeDelegate
from tests.orchestrator.case import Case
from tests.orchestrator.config_fixtures import PIPELINE, ConfigFile

SID = "1234abcd-0000-4000-8000-000000000000"


class ReadyDelegate(FakeDelegate):
    ROLES = {
        **FakeDelegate.ROLES,
        "researcher": {"deep-research": "research", "light-research": "research"},
        "pm": {"product-design": "design"},
        "engineer": {"build": "build", "light-build": "build"},
    }
    usage_result = Usage(None, None)
    usage_calls = []

    @classmethod
    def usage(cls, role, *, overlays=()):
        cls.usage_calls.append(role)
        return super().usage(role, overlays=overlays)

    @classmethod
    def resolve(cls, role, task=None, *, overlays=()):
        resolved = super().resolve(role, task, overlays=overlays)
        return (
            resolved
            if role == "dummy"
            else replace(
                resolved,
                output={
                    "type": "github",
                    "repo": "acme/notes",
                    "branch": "main",
                    "dir": role + "/",
                },
            )
        )


class RunCase(ConfigFile, Case):
    def setUp(self):
        super().setUp()
        ReadyDelegate.usage_calls = []
        self.configure()

    def configure(self, default="fake"):
        pipeline = "\n".join(
            line for line in PIPELINE.splitlines() if not line.startswith("delegates = ")
        )
        self.local(
            f"work_dir = {json.dumps(self.dir + '/state')}\n"
            + pipeline
            + f'\n[delegates]\ndefault = "{default}"\n'
            + '[delegates.fake]\nimplementation = "tests.orchestrator.linear_run_case:ReadyDelegate"\n'
            + '[delegates.harness]\nimplementation = "tests.orchestrator.linear_run_case:ReadyDelegate"\n'
            + '[delegates.other]\nimplementation = "tests.orchestrator.linear_run_case:ReadyDelegate"\n'
        )
        self.cfg = config.load()

    def start(self, issue="TASK-1", *, sid=SID, delegate_cls=None, display=None):
        owner = LinearRun(
            issue,
            "Echo input.",
            self.cfg.issue_workdir(issue),
            cfg=self.cfg,
            role="researcher",
            sid=sid,
            task="light-research",
            client="fake",
            display=display,
            delegate_cls=delegate_cls,
        )
        runs.note(
            self.cfg.runs_log,
            "start",
            issue=issue,
            sid=sid,
            role="researcher",
            client="fake",
            delegate_kind="fake",
        )
        return owner, owner.detach()
