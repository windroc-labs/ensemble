"""LiveCrew with FakeLinear, the orchestrator's config, fake_gh and a python3 shim running bin/'s
commands under the fake transport (fake_python.py). ID: an engineer's Todo issue.
"""

import json
import os
import shlex
import sys

from ensemble.manager import State
from ensemble.utils import Record
from tests.manager.case import REPO
from tests.manager.live_crew import MANAGER, LiveCrew
from tests.orchestrator.integration import fake_gh, fake_linear, fake_python, fake_transport
from tests.orchestrator.integration.flow_fixtures import (
    ACCOUNTS,
    GH,
    HARNESS,
    HARNESS_EMAIL,
    HUMAN,
    KEYS,
    LIGHT_RESEARCH,
    PROJECT,
    ROLES,
    TASK_LABELS,
)
from tests.orchestrator.integration.flow_fixtures import REPO as PROJECT_REPO
from tests.orchestrator.unit import board_ids, run_fixtures
from tests.utils.case import local_section

ID = "TASK-7"
ROUTER = ("ensemble-orchestrator", "router", "--issue", ID, "--tui")
CTL, RESUME = ("ensemble-orchestrator",), ("ensemble-orchestrator", "resume", ID)
TESTS = os.path.join(REPO, "tests")
FAKE_PYTHON = os.path.abspath(fake_python.__file__)
PR = f"https://github.com/{PROJECT_REPO}/pull/1"


class LivePipeline(LiveCrew):
    def setUp(self):
        super().setUp()
        state = os.path.join(self.root, "state")
        self.workdirs = os.path.join(state, "work")
        with open(os.path.join(self.ensemble, "config.local.toml"), "w", encoding="utf-8") as f:
            f.write(
                local_section(run_fixtures.local(state) + TASK_LABELS, "orchestrator")
                + "\n"
                + run_fixtures.CORE_LOCAL
            )
        self.fake = fake_linear.FakeLinear(harness=HARNESS)
        self.fake.user(HARNESS_EMAIL, "Harness", HARNESS)
        ids = {r: self.fake.user(ACCOUNTS[r], r.title(), KEYS[r]) for r in ROLES}
        self.fake.user(HUMAN, "Me")
        self.fake.label_group(board_ids.TASK_GROUP, "Tasks", {LIGHT_RESEARCH: "Light Research"})
        self.seam = fake_linear.seam(self, self.fake, fake_linear.serve(self, self.fake))
        self.fake.issue(
            "ENG: Session registry",
            identifier=ID,
            assignee=ids["engineer"],
            project=PROJECT,
            description="Add a session registry.",
        )
        repos = os.path.join(self.root, "repos")
        fake_gh.install(self.server.bin)
        fake_gh.bare(repos, *PROJECT_REPO.split("/"))
        fake_gh.gitconfig(self.home, repos)
        fake_gh.git_shim(self.server.bin, self.home)
        self.gh = os.path.join(self.root, "gh.json")
        with open(self.gh, "w", encoding="utf-8") as f:
            json.dump(GH, f)
        os.makedirs(self.spies, exist_ok=True)
        with open(shim := os.path.join(self.spies, "python3"), "w", encoding="utf-8") as f:
            f.write(
                f'#!/bin/sh\nPYTHONPATH="$PYTHONPATH:{TESTS}:{REPO}" exec '
                f'{shlex.quote(sys.executable)} -P -s {shlex.quote(FAKE_PYTHON)} "$@"\n'
            )
        os.chmod(shim, 0o755)

    def env(self, *, inside: str | None = MANAGER, **extra: str | None) -> dict[str, str]:
        # ENSEMBLE_PATH leads the router's PATH: no real claude
        fakes = {
            fake_transport.ENV: self.seam,
            fake_gh.ENV: self.gh,
            "ENSEMBLE_PATH": os.pathsep.join((self.spies, self.server.bin)),
        }
        return super().env(inside=inside, **{**fakes, **extra})

    def pipeline(self, name: str, steps) -> Record:
        """crew run `name` -- ROUTER; its Record and entry, checked."""
        record = self.record(self.crew("run", name, "--", *ROUTER, **self.scene(name, steps)))
        tui = f"engineer-{ID}-{record.sid[:8]}"
        self.assertEqual(
            (record.kind, record.name, record.sessions, record.workdir, record.ctl, record.resume),
            ("pipeline", ID, (f"{tui}-drive", tui), os.path.join(self.workdirs, ID), CTL, RESUME),
        )
        entry = self.roster()[name]
        self.assertEqual((entry.state, entry.record, entry.sid), (State.LIVE, record, record.sid))
        return record

    def state(self, ident: str) -> str:
        with self.fake.lock:
            return fake_linear.NAMES[self.fake.find(ident)["state"]]
