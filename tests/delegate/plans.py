"""Base, the plan tests' case, which the detach tests share."""

import io
import os
import re
import tempfile
from contextlib import redirect_stderr
from dataclasses import replace

from ensemble.delegate import ConfigError, Spec
from ensemble.delegate.composer import Composer
from ensemble.delegate.composer.base import TEAM
from ensemble.delegate.engine.plan import plan
from tests.delegate.case import Case, committed
from tests.delegate.unit.composer.teams import POET, VERSE
from tests.session import fake_client
from tests.session.modules import client_loader
from tests.utils.case import local_section

SID = "11111111-2222-3333-4444-555555555555"


TASKS = os.path.join(TEAM, "tasks")


TASKS_PARAM = re.compile(r"\n- `<tasks>`: `([^`]+)`\n")


TEAM_FILES = {"roles/poet.md": POET, "tasks/verse.md": VERSE}


def write_team(top, files=TEAM_FILES):
    for rel, text in files.items():
        path = os.path.join(top, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.chmod(os.path.dirname(path), 0o755)
        with open(path, "w") as f:
            f.write(text)
        os.chmod(path, 0o644)


class Base(Case):
    """A real temp dir for the workdir `work` (not made), a HOME of the test's own (self.ensemble)
    and fresh session settings: the core config's clients and the fake client.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.work = os.path.join(self.tmp, "work")
        self.out = os.path.join(self.work, "out.md")
        self.session_dependencies = {
            "class_loader": client_loader({"ensemble.session.clients.fake": fake_client})
        }
        self.local("")

    def local(self, text):
        """
        The local config `text`, the session configured with its clients and the fake client.
        """
        local = os.path.join(self.ensemble, "config.local.toml")
        with open(local, "w") as f:
            f.write(
                local_section(text, "delegate")
                .replace("[delegate.hosts", "[session.hosts")
                .replace("[delegate.clients", "[session.clients")
            )
        fake = {"implementation": "ensemble.session.clients.fake:FakeClient"}
        self.configure_session(committed(local) | {"fake": fake})

    def planned(self, role="researcher", task="research", *, keys=None, team=None, run=None, **kw):
        kw = {"client": "fake", "mode": "headless", "out": self.out, "input": "Research X.", **kw}
        workdir = kw.pop("workdir", self.work)
        environment = kw.pop("environment", None)
        composition = Composer(team=team)
        run = run or replace(composition.resolve(role), **(keys or {}))
        err = io.StringIO()
        with redirect_stderr(err):
            got = plan(
                run,
                Spec(kw.pop("input"), workdir, task=task, team=team, **kw),
                driver=self.driver,
                composition=composition,
                **({} if environment is None else {"environment": environment}),
            )
        return got, err.getvalue()

    def refused(self, *args, **kw) -> str:
        with self.assertRaises(ConfigError) as cm:
            self.planned(*args, **kw)
        return str(cm.exception)
