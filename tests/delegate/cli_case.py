"""Shared CLI fixtures over the configured fake client."""

import io
import json
import os
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from ensemble.delegate import cli
from ensemble.delegate.engine.harness_delegate import HarnessDelegate
from ensemble.delegate.engine.types import Layout
from ensemble.session import ConfigError
from tests.delegate.case import Case
from tests.delegate.fakes.harness_session import DrivenDelegate
from tests.delegate.replies import block, completed_steps
from tests.delegate.unit.runs import DONE, START, said
from tests.delegate.verifier import VERIFIER
from tests.session import fake_client
from tests.session.modules import client_loader
from tests.utils.case import local_section
from tests.utils.jsonl import read_jsonl

SID = "11111111-2222-3333-4444-555555555555"
TUI = "dummy-tester-11111111"
HARNESS = f"{TUI}-drive"

KEYS = {
    "client",
    "environment",
    "mode",
    "sid",
    "workdir",
    "cwd",
    "project",
    "dirs",
    "reads",
    "commands",
    "tier",
    "effort",
    "prompt",
}
PLACEMENT = {"sessions", "hosts", "callback"}


HEADS = [
    "researcher  local  built-in",
    "pm  local  built-in",
    "engineer  pull-request  built-in",
    "verifier  github  built-in",
    "dummy-tester  local  built-in",
]


class RefusingClient(fake_client.InProcessFakeClient):
    def in_process(self):
        return RefusingRun(self)


class RefusingRun(fake_client.FakeInProcess):
    def __init__(self, client):
        super().__init__(client)
        self.refusal = ConfigError("refused by check")

    def check(self, spec):
        raise self.refusal


class Base(Case):
    """A HOME of the test's own, the fake client registered, the cwd a temp dir, workdir `work`
    (not made), TMUX and the launcher's ENSEMBLE_SID and ENSEMBLE_EVENTS unset.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.work, self.scenario, self.log = (
            os.path.join(self.tmp, n) for n in ("work", "scenario.json", "fake.log")
        )
        self.out = os.path.join(self.work, "out.md")
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.tmp)
        self.session_dependencies = {
            "class_loader": client_loader({"ensemble.session.clients.fake": fake_client})
        }
        self.configure_fake()
        self.local(VERIFIER)
        self.steps(said(START, block(DONE)))
        self.enterContext(mock.patch.dict(os.environ))
        for key in ("TMUX", "TMUX_PANE", "ENSEMBLE_SID", "ENSEMBLE_EVENTS"):
            os.environ.pop(key, None)

    def configure_fake(self, *, tui=False):
        self._tui = tui
        self.configure_session(
            {
                "fake": {
                    "implementation": "ensemble.session.clients.fake:FakeClient",
                    "scenario": self.scenario,
                    "log": self.log,
                    "tui": tui,
                }
            }
        )

    def steps(self, *steps):
        with open(self.scenario, "w") as f:
            json.dump(list(steps) if self._tui else completed_steps(steps), f)

    def local(self, text):
        with open(os.path.join(self.ensemble, "config.local.toml"), "w") as f:
            f.write(local_section(text, "delegate"))

    def launches(self) -> list[dict]:
        return read_jsonl(self.log, missing_ok=True)

    def records(self) -> list[dict]:
        return read_jsonl(os.path.join(Layout(self.work).run, "run.jsonl"))

    ran = None

    def delegate_cls(self) -> type[HarnessDelegate]:
        """HarnessDelegate on this test's driver; its run returns self.ran when set. Its Session
        class is configured apart from the shared one and loads no class by name (the driver holds
        the fake client).
        """
        case, driver = self, self.driver
        session_cls = type(
            "CliSession",
            (driver.session_cls,),
            {"class_roots": classmethod(lambda _cls, _client: ())},
        )

        class Driven(DrivenDelegate):
            def __init__(self, role, spec, **placement):
                super().__init__(role, spec, driver=driver, session_cls=session_cls, **placement)

            def run(self, on_event=None):
                return super().run(on_event) if case.ran is None else case.ran

            @classmethod
            def preview(cls, role, spec, **placement):
                return super().preview(
                    role, spec, driver=driver, session_cls=session_cls, **placement
                )

        return Driven

    def cli(self, *argv, stdin=""):
        """(exit code, stdout, stderr) of cli.main(argv), an argparse exit's code included."""
        out, err = io.StringIO(), io.StringIO()
        with (
            redirect_stdout(out),
            redirect_stderr(err),
            mock.patch("sys.stdin", io.StringIO(stdin)),
        ):
            try:
                code = cli.main(list(argv), delegate_cls=self.delegate_cls())
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def runs(
        self,
        *extra,
        role="dummy-tester",
        task="echo",
        input="Hello.",
        client="fake",
        stdin="",
        out=None,
    ):
        return self.cli(
            "run",
            "--role",
            role,
            *(("--task", task) if task else ()),
            "--input",
            input,
            "--out",
            out or self.out,
            "--workdir",
            self.work,
            *(("--client", client) if client else ()),
            *extra,
            stdin=stdin,
        )
