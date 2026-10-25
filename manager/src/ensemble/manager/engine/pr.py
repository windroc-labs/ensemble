"""A pull request named by an entry's text, and the report cells `gh pr view` JSON gives it."""

import os
import re
from collections.abc import Mapping
from urllib.parse import urlsplit

PR_URL = re.compile(r"https://[^\s/]+/[^\s/]+/[^\s/]+/pull/[0-9]+")
VIEW_FIELDS = "number,mergeable,reviewDecision,statusCheckRollup"
DEFAULT_HOST = "github.com"
FAILED = {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR"}
PENDING = {"PENDING", "EXPECTED"}
NO_PR = ("-", "-", "-", "-")


def pr_url(*texts: str | None, env: Mapping[str, str] = os.environ) -> str | None:
    """The first PR URL in texts on github.com or GH_HOST, without userinfo or port: any other host
    could receive gh's token.
    """
    hosts = {DEFAULT_HOST, env.get("GH_HOST", "").lower()} - {""}
    for text in texts:
        for match in PR_URL.finditer(text or ""):
            try:
                parts = urlsplit(match[0])
                host = parts.hostname
            except ValueError:
                continue
            if parts.netloc.lower() == host and host in hosts:
                return match[0]
    return None


def unknown(url: str) -> tuple[str, str, str, str]:
    """The cells of a PR whose `gh pr view` failed: its number from url, the rest `?`."""
    return f"#{url.rpartition('/')[2]}", "?", "?", "?"


def _bucket(check) -> str:
    if not isinstance(check, dict):
        raise ValueError("statusCheckRollup: want objects")
    if "state" in check:
        state = check["state"]
        return "fail" if state in FAILED else "pending" if state in PENDING else "pass"
    if check.get("status") != "COMPLETED":
        return "pending"
    return "fail" if check.get("conclusion") in FAILED else "pass"


def checks(rollup: list) -> str:
    buckets = [_bucket(check) for check in rollup]
    for word in ("fail", "pending"):
        if word in buckets:
            return f"{word} {buckets.count(word)}/{len(buckets)}"
    return "pass" if buckets else "none"


def cells(view) -> tuple[str, str, str, str]:
    """`#<number>`, checks, mergeable and review from `gh pr view --json VIEW_FIELDS`."""
    if not isinstance(view, dict):
        raise ValueError("gh pr view: want a JSON object")
    number, mergeable = view.get("number"), view.get("mergeable")
    review, rollup = view.get("reviewDecision"), view.get("statusCheckRollup")
    if (
        type(number) is not int
        or not isinstance(mergeable, str)
        or not isinstance(review, str | None)
        or not isinstance(rollup, list | None)
    ):
        raise ValueError(f"gh pr view: want {VIEW_FIELDS}")
    return f"#{number}", checks(rollup or []), mergeable.lower(), review.lower() if review else "-"
