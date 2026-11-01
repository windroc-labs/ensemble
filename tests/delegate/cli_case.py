"""Shared CLI fixtures over the configured fake client."""

import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from ensemble.delegate import cli
from ensemble.delegate.engine.harness_delegate import HarnessDelegate
from ensemble.session import ConfigError
from tests.delegate.case import FakeClientCase, RunReads
from tests.delegate.fakes.harness_session import DrivenDelegate
from tests.delegate.replies import block, completed_steps
from tests.delegate.unit.runs import DONE, START, said
from tests.delegate.verifier import VERIFIER
from tests.session import fake_client
from tests.utils.case import local_section
from tests.utils.jsonl import read_jsonl

TUI = "researcher-11111111"
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
]


COMMANDS = [
    "run",
    "open",
    "status",
    "content",
    "outcome",
    "resume",
    "show",
    "hide",
    "close",
    "prompt",
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


class Base(RunReads, FakeClientCase):
    """A HOME of the test's own, the fake client registered, the cwd a temp dir, workdir `work`
    (not made), TMUX and the launcher's ENSEMBLE_SID and ENSEMBLE_EVENTS unset.
    """

    def setUp(self):
        super().setUp()
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.tmp)
        self.local(VERIFIER)
        self.steps(said(START, block(DONE)))
        self.enterContext(mock.patch.dict(os.environ))
        for key in ("TMUX", "TMUX_PANE", "ENSEMBLE_SID", "ENSEMBLE_EVENTS"):
            os.environ.pop(key, None)

    def steps(self, *steps):
        with open(self.scenario, "w") as f:
            json.dump(list(steps) if self._tui else completed_steps(steps), f)

    def local(self, text):
        with open(os.path.join(self.ensemble, "config.local.toml"), "w") as f:
            f.write(local_section(text, "delegate"))

    def launches(self) -> list[dict]:
        return [
            e for e in read_jsonl(self.log, missing_ok=True) if e.get("argv", [])[-1:] != ["/usage"]
        ]

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
        role="researcher",
        task="light-research",
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
