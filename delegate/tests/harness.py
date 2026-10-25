"""Shared HarnessDelegate fixtures with caller-owned configuration and Sessions."""

import json
import os

from ensemble.delegate import Spec
from ensemble.delegate.engine.harness_delegate import HarnessDelegate
from ensemble.delegate.engine.state import RunState
from ensemble.delegate.engine.types import Layout
from ensemble.session import Exited, Reply

from delegate.tests.case import REPO, Case
from delegate.tests.fakes.harness_session import FakeViewer, HarnessSession, RecordingHost
from delegate.tests.replies import block
from delegate.tests.unit.runs import DONE
from utils.tests.sandbox import owned_directory

SID = "0f8fad5b-d9cb-469f-a165-70867728950e"


class ViewedHarnessDelegate(HarnessDelegate):
    """A HarnessDelegate whose views show through a FakeViewer unless given a Viewer."""

    def __init__(self, role, spec, **placement):
        super().__init__(role, spec, **{"viewer": FakeViewer(), **placement})

    @classmethod
    def attach(cls, record_or_workdir, **kw):
        return super().attach(record_or_workdir, **{"viewer": FakeViewer(), **kw})


class HarnessCase(Case):
    def setUp(self):
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
        harness_host=None,
    ):
        with open(self.local, "w", encoding="utf-8") as file:
            file.write(
                f'[delegate]\nlanguage = "{language}"\n'
                f"team_dirs = {json.dumps(list(team_dirs))}\n"
                f"trusted_dirs = {json.dumps(list(trusted_dirs))}\n"
                f'[delegate.roles.dummy-tester]\nclient = "{client}"\n'
                f'[session.hosts]\ndefault = "{default}"\n'
                "[session.clients.fake]\n"
                'implementation = "session.tests.fake_client:FakeClient"\n'
            )
            if harness_host is not None:
                file.write(f'[delegate.delegates]\nharness_host = "{harness_host}"\n')
            for host in hosts:
                file.write(
                    f"[session.hosts.{host}]\n"
                    'implementation = "delegate.tests.fakes.harness_session:RecordingHost"\n'
                )

    def team_dir(self):
        team = owned_directory(
            self,
            os.environ.get("ENSEMBLE_TEST_TMP", os.path.join(REPO, "work")),
            prefix="harness-team-",
        )
        os.chmod(team, 0o755)
        return os.path.realpath(team)

    def make(self, *, role="dummy-tester", spec=None, session_cls=HarnessSession, **kw):
        kw.setdefault("viewer", FakeViewer())
        spec = spec or Spec(
            "Hello.\nLiteral input.", self.work, task="echo", sid=SID, client="fake"
        )
        return HarnessDelegate(role, spec, session_cls=session_cls, **kw)

    def state_record(self) -> str:
        return RunState(Layout(self.work)).record

    def metadata(self):
        with open(self.state_record(), encoding="utf-8") as file:
            return json.load(file)

    def stop(self, record):
        for (_host, name), process in RecordingHost.processes.items():
            if name in record.sessions:
                process.close()
