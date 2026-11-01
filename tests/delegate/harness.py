"""Shared HarnessDelegate fixtures with caller-owned configuration and Sessions."""

import json
import os

from ensemble.delegate import Spec
from ensemble.delegate.engine.state import RunState
from ensemble.delegate.engine.types import Layout
from ensemble.session import Exited, Reply
from tests.delegate.case import Case
from tests.delegate.fakes.harness_session import (
    DrivenDelegate,
    FakeControl,
    FakeViewer,
    HarnessSession,
    RecordingHost,
)
from tests.delegate.replies import block
from tests.delegate.unit.runs import DONE
from tests.delegate.verifier import VERIFIER
from tests.session.case import SID
from tests.utils.case import local_section


class ViewedHarnessDelegate(DrivenDelegate):
    """A HarnessDelegate whose views show through a FakeViewer unless given a Viewer."""

    def __init__(self, role, spec, **placement):
        super().__init__(role, spec, **{"viewer": FakeViewer(), **placement})

    @classmethod
    def attach(cls, record_or_workdir, **kw):
        return super().attach(record_or_workdir, **{"viewer": FakeViewer(), **kw})


class HarnessCase(Case):
    def setUp(self):
        FakeControl.reset()
        HarnessSession.reset()
        HarnessSession.script = (Reply(block(DONE)), Exited(0))
        self.work = os.path.join(self.home, "run")
        self.local = os.path.join(self.ensemble, "config.local.toml")
        self.settings()

    def settings(
        self,
        *,
        language="English",
        client="fake",
        default="agent",
        team_dirs=(),
        trusted_dirs=(),
        hosts=("agent", "harness", "changed"),
    ):
        with open(self.local, "w", encoding="utf-8") as file:
            file.write(
                f'[delegate]\nlanguage = "{language}"\n'
                f"team_dirs = {json.dumps(list(team_dirs))}\n"
                f"trusted_dirs = {json.dumps(list(trusted_dirs))}\n"
                f'[delegate.roles.researcher]\nclient = "{client}"\n'
                + local_section(VERIFIER, "delegate")
                + f'[session.hosts]\ndefault = "{default}"\n'
                "[session.clients.fake]\n"
                'implementation = "tests.session.fake_client:FakeClient"\n'
            )
            for host in hosts:
                file.write(
                    f"[session.hosts.{host}]\n"
                    'implementation = "tests.delegate.fakes.harness_session:RecordingHost"\n'
                )

    def make(self, *, role="researcher", spec=None, session_cls=HarnessSession, **kw):
        kw.setdefault("viewer", FakeViewer())
        spec = spec or Spec(
            "Hello.\nLiteral input.", self.work, task="light-research", sid=SID, client="fake"
        )
        return DrivenDelegate(role, spec, session_cls=session_cls, **kw)

    def state_record(self) -> str:
        return RunState(Layout(self.work)).record

    def metadata(self):
        with open(self.state_record(), encoding="utf-8") as file:
            return json.load(file)

    def stop(self, record):
        for (_host, name), process in RecordingHost.processes.items():
            if name in record.sessions:
                process.close()
        with open(RunState(Layout(record.workdir)).record, encoding="utf-8") as file:
            drive = json.load(file).get("drive")
        if drive is not None:
            FakeControl(drive).close()
