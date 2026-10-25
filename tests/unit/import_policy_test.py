"""Nested unit contracts preserve main's allowed edges and reject future-module escapes."""

import tomllib
from pathlib import Path

from tests.case import REPO, Case
from tests.import_policy import violation


class Units(Case):
    def setUp(self):
        config = tomllib.loads(Path(REPO, "pyproject.toml").read_text())
        self.policies = [
            c for c in config["tool"]["importlinter"]["contracts"] if c["type"] == "units"
        ]

    def rejected(self, source, target):
        return any(violation(source, target, policy) for policy in self.policies)

    def test_unit_contracts_keep_allowed_edges_and_reject_the_rest(self):
        session = (
            *(
                (source, target, False)
                for source, target in (
                    ("engine.hosted_session", "clients.codex"),
                    ("engine.hosted_session", "modes.tui"),
                    ("engine.hosted_session", "hosts.tmux.host"),
                    ("engine.hosted_session", "engine.records"),
                    ("engine.hosted_session", "engine.base"),
                    ("engine.records", "engine.types"),
                    ("__init__", "engine.records"),
                    ("modes.tui", "hosts.tmux.host"),
                    ("modes.headless", "clients.base"),
                    ("modes.tui", "modes.client_adapters.tui"),
                    ("clients.claude.headless", "modes.client_adapters.headless"),
                    ("modes.client_adapters.tui", "engine.types"),
                    ("modes.base", "clients.registry"),
                    ("clients.codex", "engine.types"),
                    ("clients.registry", "hosts.base"),
                    ("engine.base", "hosts.base"),
                    ("hosts.base", "engine.types"),
                    ("hosts.tmux.host", "hosts.base"),
                    ("hosts.tmux.host", "engine.types"),
                    ("hosts.tmux.host", "engine.tmux_cli"),
                    ("hosts.docker.host", "engine.tmux_cli"),
                    ("engine.tmux_cli", "engine.status_log"),
                    ("engine.tmux_cli", "engine.types"),
                    ("__init__", "engine.base"),
                    ("extension", "clients.base"),
                    ("extension", "hosts.base"),
                    ("extension", "modes.client_adapters.tui"),
                    ("extension", "engine.types"),
                    ("engine.hosted_session", "viewer.display"),
                    ("cli", "viewer.display"),
                    ("modes.tui", "viewer.viewer"),
                    ("__init__", "viewer.viewer"),
                    ("viewer.viewer", "engine.tmux_cli"),
                    ("viewer.viewer", "hosts.base"),
                    ("viewer.display", "engine.types"),
                    ("hosts.base", "hosts.types"),
                    ("hosts.tmux.host", "hosts.types"),
                    ("viewer.viewer", "hosts.types"),
                    ("extension", "hosts.types"),
                )
            ),
            *(
                (source, target, True)
                for source, target in (
                    ("clients.codex", "engine.tmux_cli"),
                    ("hosts.base", "engine.tmux_cli"),
                    ("engine.tmux_cli", "hosts.base"),
                    ("engine.tmux_cli", "hosts.tmux.types"),
                    ("engine.hosted_session", "engine.tmux_cli"),
                    ("modes.tui", "engine.tmux_cli"),
                    ("hosts.tmux.host", "clients.registry"),
                    ("clients.base", "modes.tui"),
                    ("modes.client_adapters.tui", "modes.tui"),
                    ("modes.client_adapters.headless", "clients.base"),
                    ("modes.client_adapters.in_process", "hosts.base"),
                    ("modes.tui", "engine.hosted_session"),
                    ("modes.tui", "engine.base"),
                    ("modes.tui", "engine.records"),
                    ("engine.base", "engine.hosted_session"),
                    ("engine.base", "clients.registry"),
                    ("__init__", "engine.hosted_session"),
                    ("__init__", "engine.tmux_cli"),
                    ("engine.types", "clients.base"),
                    ("__init__", "extension"),
                    ("clients.base", "extension"),
                    ("engine.hosted_session", "extension"),
                    ("extension", "modes.base"),
                    ("extension", "engine.hosted_session"),
                    ("extension", "hosts.tmux.host"),
                    ("hosts.base", "viewer.viewer"),
                    ("hosts.tmux.host", "viewer.grid"),
                    ("hosts.docker.host", "viewer.viewer"),
                    ("viewer.viewer", "engine.hosted_session"),
                    ("viewer.display", "modes.tui"),
                    ("viewer.viewer", "hosts.tmux.host"),
                    ("engine.worker", "viewer.display"),
                    ("extension", "viewer.viewer"),
                    ("hosts.types", "engine.types"),
                    ("hosts.types", "hosts.base"),
                    ("engine.hosted_session", "hosts.types"),
                )
            ),
        )
        shared = ("config", "engine.config", "event.reply", "team.tools.repo")
        delegate = (
            *((source, "engine.future", True) for source in shared),
            ("event.reply", "engine.config", False),
            ("team.tools.repo", "engine.config", False),
            ("engine.config", "event", True),
            ("config", "event", True),
            ("engine.config", "config", False),
            ("engine.plan", "team.tools.repo", True),
            *(
                (source, "team.tools.helper", False)
                for source in ("team.tools", "team.tools.repo", "team.tools.nested.caller")
            ),
        )
        manager = (
            ("engine.crew", "engine.roster", False),
            ("engine.holder", "engine.roster", False),
            ("engine.roster", "engine.events", False),
            ("engine.crew", "engine.types", False),
            ("cli", "engine.crew", False),
            ("engine.roster", "engine.crew", True),
            ("engine.roster", "engine.holder", True),
            ("cli", "engine.roster", True),
        )
        cases = (
            *((f"ensemble.session.{s}", f"ensemble.session.{t}", r) for s, t, r in session),
            *((f"ensemble.delegate.{s}", f"ensemble.delegate.{t}", r) for s, t, r in delegate),
            *((f"ensemble.delegate.{source}", "ensemble.utils", False) for source in shared),
            ("ensemble.delegate.config", "ensemble.session", True),
            (
                "tests.delegate.unit.team.tools.repo_test",
                "ensemble.delegate.team.tools.repo",
                False,
            ),
            *((f"ensemble.manager.{s}", f"ensemble.manager.{t}", r) for s, t, r in manager),
            ("ensemble.manager.engine.holder", "ensemble.session", True),
            ("ensemble.manager.engine.crew", "ensemble.session", True),
            ("ensemble.manager.engine.roster", "ensemble.delegate", True),
        )
        for source, target, rejected in cases:
            with self.subTest(source=source, target=target):
                self.assertEqual(self.rejected(source, target), rejected)


class TestOwnership(Case):
    def test_each_layer_forbids_every_shared_test_module(self):
        contracts = tomllib.loads(Path(REPO, "pyproject.toml").read_text())["tool"]["importlinter"][
            "contracts"
        ]
        layers = next(c["layers"] for c in contracts if c["type"] == "layers")
        layer_packages = {f"tests.{layer.rsplit('.', 1)[-1]}" for layer in layers}
        root = Path(REPO, "tests")
        modules = {
            f"tests.{path.stem}" for path in root.glob("*.py") if path.stem != "__init__"
        } | {f"tests.{path.name}" for path in root.iterdir() if (path / "__init__.py").is_file()}
        shared_modules = modules - layer_packages
        for layer in sorted(layer_packages):
            with self.subTest(layer=layer):
                ownership = [
                    c
                    for c in contracts
                    if c["type"] == "forbidden" and c.get("source_modules") == [layer]
                ]
                self.assertEqual(len(ownership), 1)
                forbidden = {
                    name for name in ownership[0]["forbidden_modules"] if name.startswith("tests.")
                }
                self.assertEqual(forbidden - layer_packages, shared_modules)
