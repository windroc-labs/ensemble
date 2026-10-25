"""The Composer in a child process: PY with PYTHONPATH this tree's import root, the test's own HOME
(its ~/.ensemble/config.local.toml the only local config) and a temp cwd, printing one JSON value.
PROGRAM runs argv[1]: compose (one agent run as its caller, the Delegate, would), catalog (every
role's catalog row, rendered, with a workdir), or index.
"""

import json
import os
import subprocess
import tempfile

from delegate.tests import case
from delegate.tests.case import PY, child_env

TIMEOUT = 60
PROGRAM = """import dataclasses
import json
import os
import sys

from ensemble.delegate.composer import Composer, ConfigError, RunParams


def compose(arg):
    req = json.loads(arg)
    try:
        composition = Composer()
        run = composition.resolve(req["role"], req.get("task"), overlays=req.get("overlays", ()))
        intent = composition.access(run, req["workdir"], cwd=req["cwd"], project=False)
        writes = [("the workdir", req["workdir"]), ("the cwd", req["cwd"])]
        writes += [("a dir the run adds", d) for d in intent.dirs]
        composition.check_team_dirs(writes=writes)
        params = RunParams(
            input=req["input"], out=req["out"], workdir=req["workdir"], resume=req.get("resume", False)
        )
        print(
            json.dumps(
                {
                    "run": dataclasses.asdict(run),
                    "intent": dataclasses.asdict(intent),
                    "prompt": composition.render(run, params),
                    "trusted": sorted(composition.trusted_dirs()),
                    "files": [
                        sys.modules["ensemble.delegate.composer"].__file__,
                        sys.modules["ensemble.utils.config_file"].__file__,
                        sys.modules["ensemble.utils.paths"].__file__,
                    ],
                    "repo_imported": any(
                        os.path.dirname(getattr(m, "__file__", None) or "")
                        == os.path.join(composition.team.root, "tools")
                        for m in list(sys.modules.values())
                    ),
                }
            )
        )
    except ConfigError as e:
        print(json.dumps({"error": str(e)}))


def catalog(workdir):
    composition = Composer()
    team = composition.team
    composition.check_team_dirs(writes=[])
    composition.check_all()
    params = RunParams(input="", out=os.path.join(workdir, "out.md"), workdir=workdir)
    rows = {}
    for role, tasks in composition.index().items():
        run = composition.resolve(role)
        composition.render(run, params)
        rows[role] = [
            run.output["type"],
            team.source(composition.path_for("roles", role)),
            {t: [what, team.source(composition.path_for("tasks", t))] for t, what in tasks.items()},
        ]
    print(json.dumps(rows))


def index():
    print(json.dumps(Composer().index()))


{"compose": compose, "catalog": catalog, "index": index}[sys.argv[1]](*sys.argv[2:])
"""


class Case(case.Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.work, self.proj = os.path.join(self.root, "work"), os.path.join(self.root, "proj")
        for d in (self.work, self.proj):
            os.mkdir(d)
        self.out = os.path.join(self.work, "out.md")

    def local(self, text: str, mode: str = "w") -> None:
        with open(os.path.join(self.ensemble, "config.local.toml"), mode, encoding="utf-8") as f:
            f.write(text)

    def child(self, *args: str):
        res = subprocess.run(
            [*PY, "-c", PROGRAM, *args],
            cwd=self.root,
            env=child_env({**os.environ, "HOME": self.home}),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)

    def compose(self, role: str, task: str | None = None, **req) -> dict:
        req = {
            "role": role,
            "task": task,
            "input": "Hello.",
            "out": self.out,
            "workdir": self.work,
            "cwd": self.work,
            **req,
        }
        return self.child("compose", json.dumps(req))
