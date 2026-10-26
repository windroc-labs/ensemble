"""A promote run, prune included, against a counting fake Linear at a real host's scale: 297 work
dirs (30 with agent files), 3 role apps; requests counted by phase and query constant."""

import importlib
import io
import os
import pkgutil
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import timedelta

from ensemble.orchestrator import cli
from ensemble.orchestrator.engine import promote
from ensemble.orchestrator.engine.types import TrackerKind
from ensemble.orchestrator.trackers import linear as linear_pkg
from tests.orchestrator.prune_fixtures import NOW, PRUNE_QUERIES, PruneScenario, gql_for, write
from tests.orchestrator.unit import promote_fixtures as tp
from tests.orchestrator.unit.board_ids import role_id
from tests.orchestrator.unit.run_fixtures import CORE_LOCAL

SETUP, PROMOTE, PRUNE = "setup", "promote", "prune"
ENGINEER_ID = role_id("engineer")


def _names():
    """Query text: "module.CONSTANT" of the Linear tracker package, linear_tracker's and ops' first."""
    found, mods = {}, sorted(m.name for m in pkgutil.iter_modules(linear_pkg.__path__))
    for mod in dict.fromkeys(["linear_tracker", "ops", *mods]):
        module = importlib.import_module(f"{linear_pkg.__name__}.{mod}")
        for attr, value in vars(module).items():
            if (
                attr.isupper()
                and isinstance(value, str)
                and value.startswith(("query", "mutation"))
            ):
                found.setdefault(value, f"{mod}.{attr}")
    return found


NAMES = _names()


class Counted:
    """One gql: prune's queries to gql_for, the rest to FakeLinear; counts {phase: Counter(name)}, a
    query no constant holds as "unnamed: <text>"."""

    def __init__(self, promote_fake, prune_fake):
        self.promote_fake, self.prune_fake = promote_fake, prune_fake
        self.phase, self.counts = SETUP, {}

    def __call__(self, query, *, identity, **v):
        name = NAMES.get(query, f"unnamed: {query[:80]}")
        self.counts.setdefault(self.phase, Counter())[name] += 1
        if query in PRUNE_QUERIES:
            return self.prune_fake(query, **v)
        return self.promote_fake(query, identity=identity, **v)


class HostScale(PruneScenario):
    """counted_run(at, changed) runs promote.main once; changed: idents updated a minute before at,
    which the change feed lists by its updatedAt filter, as Linear does."""

    def setUp(self):
        super().setUp()
        write(
            os.path.join(self.ensemble, "config.local.toml"),
            f'[orchestrator]\nwork_dir = "{self.root}"\n' + CORE_LOCAL,
        )
        self.config_path = os.path.join(self.root, "config.toml")
        write(self.config_path, tp.PM_NEXT)  # the packaged workflow: 3 linear.roles
        self.counted = Counted(self._handoffs(), self._board())

    def _handoffs(self):
        fake = tp.FakeLinear()
        for ident, role, minutes in (
            ("TASK-1001", "researcher", 30),
            ("TASK-1002", "researcher", 45),
            ("TASK-1003", "pm", 1),  # waits out MATURE
        ):
            fake.add(ident, role=role)
            fake.moved(ident, minutes + 60, "In Review")
            fake.said(ident, minutes + 15)
            fake.moved(ident, minutes, "Handoff", frm=tp.STATES["In Review"])
        return fake

    def _board(self):
        issues, owners, children, archived = {}, {}, {}, set()

        def add(n, state, moves, owner=ENGINEER_ID, *, gone=False):
            ident = f"TASK-{n}"
            issues[ident], owners[ident] = (state, moves), owner
            if gone:
                archived.add(ident)
            return ident

        done = [(50, "In Progress"), (3, "Done")]
        agent = [add(n, "Done", done) for n in range(1, 5)]  # due: closed, cleaned, archived
        agent.append(add(5, "Canceled", [(50, "Todo"), (3, "Canceled")], owner=None))
        agent.append(add(6, "Done", done, owner=role_id("researcher")))
        agent += [add(n, "Done", [(50, "In Progress"), (1, "Done")]) for n in (7, 8)]  # quarantine
        agent += [add(n, "In Review", [(20, "In Review")]) for n in range(9, 19)]
        agent += [add(n, "In Progress", [(5, "In Progress")]) for n in range(19, 24)]
        agent += [add(n, "Todo", [(1, "Todo")]) for n in range(24, 28)]
        agent += [add(n, "In Review", [(200, "In Review")]) for n in range(28, 31)]  # stalled
        old = [(200, "In Progress"), (72, "Done")]
        bare = [
            add(n, "Done", [(200, "In Progress"), (48 + n % 72, "Done")], gone=True)
            for n in range(31, 231)
        ]
        bare += [
            add(n, "Canceled", [(200, "Todo"), (60, "Canceled")], owner=None, gone=True)
            for n in range(231, 251)
        ]
        bare += [add(n, "Done", old, owner=role_id("researcher")) for n in range(251, 257)]
        bare += [add(n, "Done", old) for n in range(257, 264)]
        children["TASK-257"] = {"TASK-901": "started"}  # refused
        children["TASK-258"] = {"TASK-902": "completed"}
        children["TASK-902"] = {"TASK-903": "unstarted"}  # refused, a level down
        children["TASK-259"] = {f"TASK-{2000 + i}": "completed" for i in range(59)}
        children["TASK-259"]["TASK-2059"] = "started"  # refused, on the second page
        children["TASK-263"] = {"TASK-904": "completed", "TASK-905": "canceled"}
        children["TASK-904"] = {"TASK-906": "completed"}
        bare += [add(n, "In Review", [(30, "In Review")]) for n in range(264, 276)]
        bare += [add(n, "Todo", [(10, "Todo")]) for n in range(276, 286)]
        bare += [add(n, "Backlog", [(300, "Backlog")]) for n in range(286, 293)]
        bare += [add(n, "Triage", [(30, "Triage")]) for n in range(293, 296)]
        deleted = ["TASK-296", "TASK-297"]  # Linear no longer has them
        for ident in agent:
            self.record(ident, f"00000000-0000-4000-8000-{int(ident[5:]):012d}", [ident.lower()])
        for ident in bare + deleted:
            os.makedirs(self.cfg.issue_workdir(ident))
        gql = gql_for(issues, owners, children=children, changed="since")
        gql.gone |= archived
        return gql

    def _trackers(self):
        wiring = cli.TRACKERS[TrackerKind.LINEAR]

        def board(gql, cfg):
            made = wiring.board(gql, cfg)
            self.counted.phase = PROMOTE
            return made

        return {**cli.TRACKERS, TrackerKind.LINEAR: replace(wiring, board=board)}

    def pruner(self, *args, **kwargs):
        self.counted.phase = PRUNE
        return super().pruner(*args, **kwargs)

    def counted_run(self, at=NOW, changed=()):
        """{phase: {constant: count}}."""
        board = self.counted.prune_fake.issues
        for ident in changed:  # a move into its own state: updatedAt a minute before at
            state, moves = board[ident]
            board[ident] = (state, [*moves, ((NOW - at).total_seconds() / 3600 + 1 / 60, state)])
        self.counted.phase, self.counted.counts = SETUP, {}
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = promote.main(
                [],
                trackers=self._trackers(),
                gql=self.counted,
                now=at,
                config_path=self.config_path,
                pruner=self.pruner,
            )
        self.assertEqual(code, 0, err.getvalue())
        return {p: dict(c) for p, c in self.counted.counts.items()}

    def runs(self):
        """The four runs RequestCountTest pins: cold; 5 minutes later, warm with 5 issues changed;
        3 minutes later, nothing changed (TASK-1003 still waits); a day later, past prune's
        backstop, with 3 changed."""
        warm = {"TASK-1", "TASK-9", "TASK-10", "TASK-264", "TASK-276"}
        return {
            "cold": self.counted_run(),
            "warm": self.counted_run(NOW + timedelta(minutes=5), warm),
            "quiet": self.counted_run(NOW + timedelta(minutes=8)),
            "next_day": self.counted_run(
                NOW + timedelta(hours=25), {"TASK-11", "TASK-265", "TASK-277"}
            ),
        }
