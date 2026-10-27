"""Shared fixtures for unit and integration coverage."""

import io
import json
import os
import shutil
import tempfile
from contextlib import redirect_stderr
from dataclasses import replace
from unittest import mock

from ensemble.delegate import RunConfig, Spec
from ensemble.delegate.composer import Composer
from ensemble.delegate.engine.plan import plan
from ensemble.delegate.engine.runner import execute
from ensemble.delegate.engine.types import Layout, Result
from tests.delegate.case import Case
from tests.delegate.replies import completed_steps
from tests.delegate.unit.runs import DONE, NAME, turn
from tests.session import fake_client
from tests.session.modules import client_loader

SID = "11111111-2222-3333-4444-555555555555"


NEEDS = {**DONE, "status": "needs_input", "questions": ["Which repo?"], "deliverable": ""}


FAILED = {**DONE, "status": "failed", "deliverable": ""}


INVALID = {**DONE, "status": "maybe"}


INVALID_ERROR = "status 'maybe' is not one of done, needs_input, failed"


GH = {"type": "github", "repo": "o/docs", "branch": "main", "dir": "R/"}


PR = {"type": "pull-request"}


def ended(text) -> dict:
    """A step printing a result record (TurnEnd) of `text`."""
    return {"out": {"type": "result", "text": text}}


PENDING = turn("Waiting for the build.", pending=1)


def executed(
    config: RunConfig,
    spec: Spec,
    *,
    driver,
    on_event=None,
    composition=None,
    docker=False,
    session_name=None,
    environment=None,
) -> Result:
    """plan() then execute(), a session run's agent and run dirs made between."""
    composition = Composer(team=spec.team) if composition is None else composition
    accepted = plan(config, spec, driver=driver, composition=composition)
    accepted = replace(accepted, docker=docker or accepted.docker)
    if session_name is not None:
        accepted = replace(accepted, session=replace(accepted.session, name=session_name))
    if environment is not None:
        accepted = replace(accepted, session=replace(accepted.session, environment=environment))
    if driver.session_based:
        for made in (Layout(spec.workdir).agent, accepted.session.workdir):
            os.makedirs(made, exist_ok=True)
    return execute(accepted, driver=driver, on_event=on_event)


class Base(Case):
    """dummy-tester's echo over the fake client in workdir `work` (not made), out `out`, the cwd
    its agent dir; `channel` its run.jsonl.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.work = os.path.join(self.tmp, "work")
        self.out = os.path.join(self.work, "out.md")
        self.channel = os.path.join(Layout(self.work).run, "run.jsonl")
        self.scenario, self.log = (
            os.path.join(self.tmp, name) for name in ("scenario.json", "client.log")
        )
        self.session_dependencies = {
            "class_loader": client_loader({"ensemble.session.clients.fake": fake_client})
        }
        self.configure_fake()

    def configure_fake(self, *, tui=False):
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

    def go(
        self,
        *steps,
        fixes=None,
        output=None,
        on_event=None,
        docker=False,
        session_name=None,
        environment=None,
        **kw,
    ) -> Result:
        """executed() over the fake with `steps` (`fixes`: its steps on --resume); self.events gets
        each callback, self.err the stderr.
        """
        complete = list if kw.get("mode") == "tui" else completed_steps
        with open(self.scenario, "w") as f:
            json.dump(
                {
                    "steps": complete(steps),
                    **({"resume": complete(fixes)} if fixes is not None else {}),
                },
                f,
            )
        composition = Composer()
        config = composition.resolve("dummy-tester")
        config = replace(config, output=output) if output else config
        self.events, err = [], io.StringIO()

        def on(event):
            self.events.append(event)
            if on_event:
                on_event(event)

        kw = {
            "client": "fake",
            "mode": "headless",
            "out": self.out,
            "input": "Echo me.",
            **kw,
        }
        try:
            with redirect_stderr(err):
                return executed(
                    config,
                    Spec(kw.pop("input"), self.work, task="echo", **kw),
                    on_event=on,
                    driver=self.driver,
                    composition=composition,
                    docker=docker,
                    session_name=session_name,
                    environment=environment,
                )
        finally:
            self.err = err.getvalue()

    def records(self) -> list[dict]:
        with open(self.channel) as f:
            return [json.loads(line) for line in f]

    def kinds(self) -> list[str]:
        return [e["kind"] for e in self.records()]

    def last(self, kind) -> dict:
        return [e for e in self.records() if e["kind"] == kind][-1]

    def launches(self) -> list[dict]:
        with open(self.log) as f:
            return [json.loads(line) for line in f]

    def read(self, path) -> str:
        with open(path) as f:
            return f.read()

    def write(self, path, text) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def fresh(self) -> None:
        shutil.rmtree(self.work, ignore_errors=True)

    def spy_save(self):
        replace = os.replace
        spy = mock.Mock(wraps=replace)

        def output(source, destination):
            if os.fspath(destination) == self.out:
                return spy(source, destination)
            return replace(source, destination)

        patch = mock.patch("os.replace", side_effect=output)
        self.addCleanup(patch.stop)
        patch.start()
        return spy


LEFT_OPEN = f"session {NAME} left open: tmux attach -t '={NAME}'\n"


class UnstreamedHeadless(fake_client.FakeHeadless):
    def events(self, lines):
        raise ValueError("no stream")


class UnstreamedClient(fake_client.FakeClient):
    def headless(self):
        return UnstreamedHeadless(self)
