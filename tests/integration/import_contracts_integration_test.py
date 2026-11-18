"""The nested-unit contracts hold on the real import graph and report edges with their locations."""

import grimp

from tests.case import REPO, Case
from tests.import_contracts import broken, contracts


class Units(Case):
    def setUp(self):
        self.policies = [c for c in contracts(REPO) if c["type"] == "units"]

    def test_contract_reports_real_graph_edges_and_line_numbers(self):
        source, target = "ensemble.session.clients.codex", "ensemble.session.engine.tmux_cli"
        graph = grimp.ImportGraph()
        for module in (source, target):
            graph.add_module(module)
        graph.add_import(
            importer=source, imported=target, line_number=7, line_contents="import " + target
        )
        policy = next(c for c in self.policies if c["container"] == "ensemble.session")
        self.assertEqual(
            [(e["importer"], e["imported"], e["line_number"]) for e in broken(graph, policy)],
            [(source, target, 7)],
        )

    def test_the_real_graph_keeps_every_unit_contract(self):
        graph = grimp.build_graph(
            "ensemble", "tests", include_external_packages=True, cache_dir=None
        )
        self.assertEqual(len(self.policies), 5)
        for policy in self.policies:
            with self.subTest(contract=policy["name"]):
                self.assertEqual(
                    [
                        f"{e['importer']}:{e['line_number']}: imports {e['imported']}"
                        for e in broken(graph, policy)
                    ],
                    [],
                )
