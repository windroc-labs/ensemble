"""Readies a done pull-request run's PR only when G1 its updraft converged, G2 at the PR head, G3
the head's checks pass; else the run fails, PR draft. Gotcha: owner/name are the agent's, so it may
name another PR on an allowed host; an agent that can run `gh auth login` can add a host.
"""

import json
import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from urllib.parse import urlsplit

from ensemble.delegate.engine.types import Gated, GateStatus
from ensemble.delegate.event.outcome import DEFAULT_HOST, PULL, summary_max
from ensemble.delegate.event.types import Outcome, Updraft, UpdraftStatus
from ensemble.utils import SHORT, OutcomeStatus, Runner, err_text, one_line, printable, sh

REASON_MAX = 300
GATE = "ready gate: "
HOST_KEY = re.compile(r"""["']?([A-Za-z0-9.-]+)["']?:\s*""")


class _Check(StrEnum):
    PASS = "pass"
    SKIPPED = "skipped"
    FAIL = "fail"


class _GhError(Exception):
    pass


@dataclass(frozen=True)
class _View:
    state: str
    draft: bool
    head: str
    checks: list[object]


def hosts(output: Mapping, env: Mapping[str, str] = os.environ) -> frozenset[str]:
    """The hosts a pull-request url may name: the output's `host` alone when set, else github.com,
    GH_HOST and the hosts in gh's hosts.yml.
    """
    if output.get("host"):
        return frozenset({output["host"].lower()})
    found = {DEFAULT_HOST, env.get("GH_HOST", "").lower()} - {""}
    config = env.get("GH_CONFIG_DIR") or os.path.join(
        env.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config"), "gh"
    )
    try:
        with open(os.path.join(config, "hosts.yml"), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except (OSError, UnicodeDecodeError):
        lines = []
    return frozenset(found | {m[1].lower() for line in lines if (m := HOST_KEY.fullmatch(line))})


def gate(outcome: Outcome, updraft: Updraft | None, *, run: Runner = sh) -> Gated:
    """`outcome`: its url host was checked against hosts()."""
    url, done = outcome.url, outcome.status == OutcomeStatus.DONE
    if not PULL.fullmatch(urlsplit(url).path):
        return Gated(outcome, GateStatus.SKIPPED, "")
    try:
        view = _view(url, run)
    except _GhError as e:
        return _refuse(outcome, str(e), done=done)
    if not done:
        status, error = _undo(url, view, run)
        return Gated(outcome, status, _line(error))
    reason = _judge(updraft, view, url, run)
    if reason is not None:
        _, error = _undo(url, view, run)
        return _failed(outcome, f"{reason}; {error}" if error else reason)
    if view.draft and (error := _ready(url, run)):
        return _failed(outcome, error)
    return Gated(outcome, GateStatus.PASSED, "")


def _ready(url: str, run: Runner) -> str | None:
    """Readies the PR: None once it is ready (a failed call may still have landed), else the errors
    after a best-effort undo.
    """
    try:
        _gh(run, "gh pr ready", ["pr", "ready", url])
    except _GhError as e:
        errors = [str(e)]
    else:
        return None
    try:
        if not _view(url, run).draft:
            return None
    except _GhError as e:
        errors.append(str(e))
    if error := _draft(url, run):
        errors.append(error)
    return "; ".join(errors)


def _refuse(outcome: Outcome, reason: str, *, done: bool) -> Gated:
    if done:
        return _failed(outcome, reason)
    return Gated(outcome, GateStatus.KEPT, _line(reason))


def _failed(outcome: Outcome, reason: str) -> Gated:
    reason = _line(reason)[: summary_max() - len(GATE)]
    line = f"{GATE}{reason}"
    kept = outcome.summary[: max(0, summary_max() - len(line) - 1)].rstrip()
    summary = f"{kept}\n{line}" if kept else line
    return Gated(
        replace(outcome, status=OutcomeStatus.FAILED, summary=summary), GateStatus.FAILED, reason
    )


def _line(text: str) -> str:
    return printable(one_line(text))[:REASON_MAX]


def _gh(run: Runner, label: str, argv: list[str]) -> str:
    try:
        res = run(["gh", *argv], SHORT)
    except (subprocess.TimeoutExpired, OSError) as e:
        raise _GhError(f"{label}: {one_line(e)}") from None
    if res.returncode != 0:
        raise _GhError(f"{label}: {err_text(res)}")
    return res.stdout


def _view(url: str, run: Runner) -> _View:
    out = _gh(
        run,
        "gh pr view",
        ["pr", "view", url, "--json", "state,isDraft,headRefOid,statusCheckRollup"],
    )
    try:
        data = json.loads(out)
    except ValueError as e:
        raise _GhError(f"gh pr view: {e}") from None
    if not isinstance(data, dict):
        raise _GhError("gh pr view: not an object")
    state, draft, head = data.get("state"), data.get("isDraft"), data.get("headRefOid")
    checks = data.get("statusCheckRollup")
    if (
        not isinstance(state, str)
        or not isinstance(draft, bool)
        or not isinstance(head, str)
        or not isinstance(checks, list | None)
    ):
        raise _GhError("gh pr view: unexpected state, isDraft, headRefOid or statusCheckRollup")
    return _View(state, draft, head, checks or [])


def _undo(url: str, view: _View, run: Runner) -> tuple[GateStatus, str]:
    """Drafts an open ready PR: (KEPT, "") nothing to do, (DRAFTED, "") drafted, (KEPT, error)."""
    if view.state != "OPEN" or view.draft:
        return GateStatus.KEPT, ""
    error = _draft(url, run)
    return (GateStatus.KEPT, error) if error else (GateStatus.DRAFTED, "")


def _draft(url: str, run: Runner) -> str:
    """`gh pr ready --undo`: "" or its error."""
    try:
        _gh(run, "gh pr ready --undo", ["pr", "ready", url, "--undo"])
    except _GhError as e:
        return str(e)
    return ""


def _judge(updraft: Updraft | None, view: _View, url: str, run: Runner) -> str | None:
    """The first of G1–G3 that fails, as a reason; None if all pass."""
    if updraft is None:
        return "G1: no updraft result"
    if updraft.status != UpdraftStatus.CONVERGED:
        return f"G1: updraft {updraft.status}"
    if view.state != "OPEN":
        return f"G2: PR is {view.state}"
    if updraft.head != view.head:
        return f"G2: updraft head {updraft.head[:12]} is not PR head {view.head[:12]}"
    checks = [_check(item, url, view.head, run) for item in view.checks]
    for name, kind, said in checks:
        if kind == _Check.FAIL:
            return f"G3: check {name} {said}"
    if checks and all(kind == _Check.SKIPPED for _, kind, _ in checks):
        return f"G3: CI did not run (CI 未运行): all {len(checks)} checks skipped"
    return None


def _check(item: object, url: str, head: str, run: Runner) -> tuple[str, _Check, str]:
    """(name, kind, GitHub's word) of a statusCheckRollup entry; unknown fails."""
    if not isinstance(item, dict):
        return "?", _Check.FAIL, "unreadable"
    name = str(item.get("name") or item.get("context") or "?")
    kind = item.get("__typename")
    if kind == "CheckRun":
        status, conclusion = str(item.get("status")), str(item.get("conclusion"))
        if status != "COMPLETED":
            return name, _Check.FAIL, status
        if conclusion == "CANCELLED" and _unstarted(item, url, head, run):
            return name, _Check.SKIPPED, conclusion
        found = {"SUCCESS": _Check.PASS, "NEUTRAL": _Check.PASS, "SKIPPED": _Check.SKIPPED}
        return name, found.get(conclusion, _Check.FAIL), conclusion
    if kind == "StatusContext":
        state = str(item.get("state"))
        return name, _Check.PASS if state == "SUCCESS" else _Check.FAIL, state
    return name, _Check.FAIL, f"of unknown type {kind}"


def _unstarted(item: dict, url: str, head: str, run: Runner) -> bool:
    """Confirm terminal cancellation without a runner or steps; missing evidence fails closed."""
    link = item.get("detailsUrl")
    if not isinstance(link, str):
        return False
    try:
        pr, job = urlsplit(url), urlsplit(link)
        repo = "/".join(pr.path.split("/")[1:3])
        path = re.fullmatch(rf"/{re.escape(repo)}/actions/runs/([1-9]\d*)/job/([1-9]\d*)", job.path)
        if job.scheme != "https" or job.netloc.lower() != pr.netloc.lower() or path is None:
            return False
        data = json.loads(
            _gh(
                run,
                "gh api job",
                ["api", "--hostname", pr.netloc, f"repos/{repo}/actions/jobs/{path[2]}"],
            )
        )
    except (_GhError, ValueError):
        return False
    # GitHub can populate started_at while queued; use execution evidence instead.
    return (
        isinstance(data, dict)
        and type(data.get("id")) is int
        and data["id"] == int(path[2])
        and type(data.get("run_id")) is int
        and data["run_id"] == int(path[1])
        and data.get("head_sha") == head
        and data.get("status") == "completed"
        and data.get("conclusion") == "cancelled"
        and all(key in data for key in ("runner_id", "runner_name", "steps"))
        and (data["runner_id"] is None or type(data["runner_id"]) is int and data["runner_id"] == 0)
        and data["runner_name"] in (None, "")
        and data["steps"] == []
    )
