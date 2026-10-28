"""A Linear issue's run, owned and recovered through its recorded Delegate implementation; its
harness calls `ensemble-orchestrator writeback --issue ID --workdir <workdir>` on each event.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace

from ensemble.delegate import Delegate, Spec
from ensemble.orchestrator.engine import config, runs
from ensemble.orchestrator.engine.flow.types import (
    ISSUE_ID,
)
from ensemble.orchestrator.engine.types import (
    UUID_RE,
    Config,
    ConfigError,
)
from ensemble.utils import Display, Record, Status


class LinearRun:
    def __init__(
        self,
        issue: str,
        input: str,
        workdir: str,
        *,
        cfg: Config,
        role: str,
        sid: str,
        task: str | None = None,
        client: str | None = None,
        mode: str = "headless",
        resume: bool = False,
        events: str | None = None,
        display: Display = None,
        delegate_kind: str | None = None,
        delegate_cls: type | None = None,
        tier: int | None = None,
    ):
        self._identity(issue, sid)
        self.issue, self.sid = issue, sid
        self.delegate_kind = delegate_kind or cfg.delegate_kind
        provider = delegate_cls or self.kind_cls(cfg, self.delegate_kind)
        overlays = (cfg.core,)
        stage = config.TASKS.get(role)
        output = cfg.roles[role].run.output if stage else {}
        if stage and (stage.input == "build" or output["type"] == "github"):
            host = cfg.repo_host(role)
            if host != "github.com" or output.get("host"):
                overlays += ({"roles": {role: {"output": dict(output) | {"host": host}}}},)
        if tier is not None:
            overlays += ({"roles": {role: {"tier": tier}}},)
        if role == "researcher":
            overlays += (
                {"roles": {role: {"gate": "ensemble-orchestrator router --brake researcher"}}},
            )
        self.delegate: Delegate = provider.create(
            role,
            Spec(
                input,
                workdir,
                task=task,
                client=client,
                sid=sid,
                mode=mode,
                resume=resume,
                name=config.tui_name(role, issue, sid),
                events=events,
                out=cfg.docs.local_out(role, issue, sid),
                credentials=(
                    json.dumps(config.credentials(cfg, role, task))
                    if stage and not resume
                    else None
                ),
            ),
            display=display,
            callback=["ensemble-orchestrator", "writeback", "--issue", issue, "--workdir", workdir],
            overlays=overlays,
        )

    @staticmethod
    def _identity(issue: str, sid: str | None = None) -> None:
        if not re.fullmatch(ISSUE_ID, issue) or sid is not None and not UUID_RE.fullmatch(sid):
            raise ConfigError(f"bad issue or session id: {issue} {sid}")

    @staticmethod
    def kind_cls(cfg: Config, kind: str) -> type:
        if kind not in cfg.delegates:
            raise ConfigError(f"delegate kind {kind!r} is no longer configured")
        return cfg.delegates[kind]

    @classmethod
    def check_tui(cls, *, cfg: Config, display: Display, events: str) -> None:
        """ConfigError unless an attended run of the configured Delegate kind could show at
        display and write events (Delegate.check_tui).
        """
        cls.kind_cls(cfg, cfg.delegate_kind).check_tui(display=display, events=events)

    @classmethod
    def attach(
        cls,
        issue: str,
        *,
        cfg: Config,
        delegate_cls: type | None = None,
        sid: str | None = None,
    ) -> LinearRun:
        cls._identity(issue, sid)
        entries = [entry for entry in runs.parse_log(cfg.runs_log) if entry.issue == issue]
        if not entries:
            raise ConfigError(f"{issue}: no recorded run")
        latest = runs.latest(entries)[issue]
        if sid is not None and sid != latest.sid:
            raise ConfigError(f"{issue}: session {sid} is no longer the latest run")
        failed = [f.ts for f in runs.failures(cfg.runs_log) if f.issue == issue]
        if sid is None and failed and max(failed) >= latest.ts:
            raise ConfigError(f"{issue}: its latest launch failed at {max(failed).isoformat()}")
        view = cls.__new__(cls)
        view.issue, view.sid = issue, latest.sid
        view.delegate_kind = runs.delegate_of(entries, latest.sid)
        provider = delegate_cls or cls.kind_cls(cfg, view.delegate_kind)
        view.delegate = provider.attach(cfg.issue_workdir(issue), sid=latest.sid)
        return view

    def detach(self) -> Record:
        return self.delegate.detach(
            outer={
                "kind": "pipeline",
                "name": self.issue,
                "ctl": ("ensemble-orchestrator",),
                "resume": ("ensemble-orchestrator", "resume", self.issue),
            }
        )

    def content(self) -> str:
        return self.delegate.content()

    def status(self) -> Status:
        return replace(self.delegate.status(), name=self.issue)

    def resume(self, *, input: str | None = None) -> Record:
        return self.delegate.resume(input=input)
