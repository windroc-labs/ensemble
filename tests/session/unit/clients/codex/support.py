"""Isolated Codex unit fixtures with recovery records and native session files."""

import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest import mock

from ensemble.session.clients import registry
from ensemble.session.clients.codex import CodexClient
from ensemble.session.clients.codex.common import SessionStore
from ensemble.session.engine.config import read_config
from ensemble.session.engine.types import LOCAL_CONTEXT, Access, Launch, Permission, Spec
from tests.session.case import REPO, Case

PLUGIN_OFF = ("-c", "plugins.ensemble@windroc.enabled=false")
TOOLS_OFF = (
    *(
        ("-c", f"plugins.{name}@openai-bundled.enabled=false")
        for name in (
            "browser",
            "chrome",
            "chrome-dev",
            "chrome-internal",
            "computer-use",
            "unified-computer-use",
        )
    ),
    ("--disable", "apps"),
    ("--disable", "tool_suggest"),
)
UNIFIED = (
    ("-s", "danger-full-access"),
    ("-c", 'approval_policy="on-request"'),
    ("-c", 'approvals_reviewer="auto_review"'),
)
AUTO = tuple(word for flag in UNIFIED for word in flag)
BYPASS = "--dangerously-bypass-approvals-and-sandbox"
MODES = {"auto": list(AUTO), "yolo": [BYPASS]}
NATIVE = "11111111-2222-3333-4444-555555555555"
CHILD = "aaaaaaaa-2222-3333-4444-555555555555"


def packaged(local: str = os.devnull) -> CodexClient:
    """The client the packaged session config.toml, under `local`, builds."""
    table = dict(registry.settings(read_config(local=local))["clients"]["codex"])
    table.pop("implementation")
    table.pop("max_runs")
    return CodexClient(table)


def holds(argv, words) -> bool:
    return any(tuple(argv[i : i + len(words)]) == tuple(words) for i in range(len(argv)))


def committed_headless(
    client: CodexClient, spec: Spec, permission: Permission = Permission.AUTO
) -> Launch:
    run = client.headless()
    run._context = replace(LOCAL_CONTEXT, permission=permission)
    launch = run.launch(spec)
    run.commit()
    return launch


def committed_tui(
    client: CodexClient, spec: Spec, hook, permission: Permission = Permission.AUTO
) -> Launch:
    run = client.tui()
    run._context = replace(LOCAL_CONTEXT, permission=permission)
    launch = run.launch(spec, hook)
    run.commit()
    return launch


class Base(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("ENSEMBLE_TEST_TMP", REPO))
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "home"
        self.work = self.root / "work"
        self.cwd = self.work / "project"
        self.extra = self.root / "extra"
        self.codex_home = self.home / ".codex"
        for path in (self.home, self.cwd, self.extra, self.codex_home):
            path.mkdir(parents=True, exist_ok=True)
        self.enterContext(
            mock.patch.dict(
                os.environ, {"HOME": str(self.home), "CODEX_HOME": str(self.codex_home)}
            )
        )
        lexists = os.path.lexists
        self.enterContext(
            mock.patch(
                "os.path.lexists",
                side_effect=lambda path: (
                    os.path.commonpath((os.path.abspath(path), str(self.root))) == str(self.root)
                    and lexists(path)
                ),
            )
        )
        self.client = CodexClient({"tiers": {"1": "gpt-test"}, "efforts": {"low": "low"}})
        self.common = self.client.common
        self.spec = Spec(
            workdir=str(self.work),
            sid="caller-id",
            tier=1,
            effort="low",
            access=Access(cwd=str(self.cwd), dirs=(str(self.extra),), commands=("git status",)),
        )

    def prepare(self):
        return self.common.save(self.common.prepare(self.spec))

    def rollout(self, native=NATIVE, *, cwd=None, parent=None, stamp=100):
        path = self.codex_home / "sessions" / "2026" / "10" / "11" / f"rollout-test-{native}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"id": native, "cwd": cwd or str(self.cwd), "source": "exec"}
        if parent:
            payload["parent_thread_id"] = parent
            payload["source"] = {
                "subagent": {"thread_spawn": {"parent_thread_id": parent, "depth": 1}}
            }
        path.write_text(
            json.dumps({"type": "session_meta", "payload": payload}) + "\n", encoding="utf-8"
        )
        os.utime(path, (stamp, stamp))
        return path

    def record(self):
        return next(Path(SessionStore().root).glob("*.json"))

    def edit_record(self, **fields):
        path = self.record()
        value = json.loads(path.read_text())
        value.update(fields)
        path.write_text(json.dumps(value), encoding="utf-8")
