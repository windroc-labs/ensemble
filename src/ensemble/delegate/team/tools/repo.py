#!/usr/bin/env python3
"""Target repos for agent runs: one checkout per repo at DIR/<owner>/<name>-<SLUG>, on the run's
branch.

    repo.py worktree --dir DIR --branch B [--name SLUG] [--base BASE] REPO
    repo.py status --dir DIR --branch B [--name SLUG] [--base BASE] REPO

worktree prints {"repo", "host", "default", "branch", "commit", "worktree", "permalink_base",
"push"}; no push permission is no error. status, after worktree with the same SLUG, prints {"pr",
"base", "plan_docs", "since", "user", "others"}. `pr` is B's PR {"number", "url", "state",
"baseRefName", "isDraft"} or null; `base` is the PR's baseRefName, else BASE, else the default
branch. `user` and `others` are PR comments and reviews by the user (core config's `users`, else the
gh login) and by anyone else since B's latest own commit (no merge; not on origin/<base>, as it was
before a merged PR's merge), all when none. `plan_docs` are B's updraft plan docs in
<worktree>/.updraft/ {"path", "phase"}, phase `S<n>` or `done`, newest (last modified) last.

ENSEMBLE_USERS, a JSON array of strings, replaces `users`. ENSEMBLE_PR_AUTHOR (a Docker session's
App bot, e.g. windroc-bot[bot]) replaces the gh login as B's PR author; `user` then holds only
`users` other than it and any [bot], never the gh login; and `push` is whether a dry-run push of B
reaches origin with git's credentials, permissions unread (an App token gets them all false).

SLUG: 1-100 of [A-Za-z0-9._-], default B with / → -. REPO is `owner/name`, `host/owner/name`,
`https://host/owner/name` or a local clone's path (/ or ~), named by its origin and outside the temp
dirs. A local clone gets a git worktree after a fetch that moves only `origin/*`; any other REPO a
blobless clone. Symlinks: the core config's trusted_dirs comment. B: the local B, else tracking
origin/B, else new from origin/BASE (origin/<default> when BASE is omitted). Each option may be
given once, so a command pre-approved by its `--dir` prefix can't be redirected elsewhere by a
second `--dir`. Exits 2 when REPO, B or SLUG is invalid or unusable (B the default branch or checked
out elsewhere), or BASE syntax is invalid. Exits 1 on other failures, including a missing
origin/BASE. Errors mask URL userinfo.
"""

import argparse
import glob
import json
import os
import re
import subprocess
import sys
from datetime import datetime

from ensemble.delegate.config import CONFIG, trusted_dirs
from ensemble.delegate.engine.config import read_config
from ensemble.utils import (
    LOCAL,
    LONG,
    SHORT,
    GitError,
    Invalid,
    Once,
    Repo,
    Runner,
    check_slug,
    checkout,
    err_text,
    origin,
    redact,
    resolve,
    sh,
    temp_dirs,
    under,
)
from ensemble.utils import run_git as git

SHA = re.compile(r"[0-9a-f]{40}")
FETCH = ("fetch", "--refmap=", "origin", "+refs/heads/*:refs/remotes/origin/*")
DENIED = re.compile(
    r"remote: Permission to \S+ denied to |remote: Write access to repository not granted"
    r"|returned error: 403$|fatal: could not read Username for |fatal: Authentication failed for ",
    re.M,
)
BRANCH = re.compile(r"(?!-)(?!.*\.\.)(?!.*//)[A-Za-z0-9._/-]{1,100}(?<![./])")


class RepoCommandError(RuntimeError):
    pass


def gh_json(run: Runner, argv: list[str]):
    res = run(["gh", *argv], SHORT)
    if res.returncode != 0:
        raise RepoCommandError(f"gh {' '.join(argv)[:100]}: {err_text(res)}")
    try:
        return json.loads(res.stdout)
    except ValueError as e:
        raise RepoCommandError(f"gh {' '.join(argv)[:100]}: {e}") from None


def info(repo: Repo, *, run: Runner, app: bool = False) -> tuple[bool | None, str]:
    res = run(["gh", "api", "--hostname", repo.host, f"repos/{repo.owner}/{repo.name}"], SHORT)
    if res.returncode != 0:
        code = re.search(r"HTTP (\d{3})", res.stderr or "")
        if code and code.group(1) in ("403", "404"):
            raise Invalid(f"{repo.slug}: not found or no access (HTTP {code.group(1)})")
        raise RepoCommandError(f"gh api: {err_text(res)}")
    try:
        data = json.loads(res.stdout)
        push = None if app else data["permissions"]["push"]
        default = data["default_branch"]
    except (ValueError, KeyError, TypeError) as e:
        raise RepoCommandError(f"gh api repos/{repo.owner}/{repo.name}: {e!r}") from None
    if not (app or isinstance(push, bool)) or not isinstance(default, str):
        raise RepoCommandError(f"gh api repos/{repo.owner}/{repo.name}: invalid repository fields")
    return push, default


def place(repo: Repo, base: str, clone: str | None, slug: str, *, run: Runner) -> tuple[str, bool]:
    wt = checkout(base, repo.owner, repo.name, slug)
    if not os.path.lexists(wt):
        return wt, False
    dotgit = os.path.join(wt, ".git")
    if not (os.path.isdir(dotgit) or os.path.isfile(dotgit)):
        raise Invalid(f"{wt} exists and is not a checkout")
    if os.path.isfile(dotgit):
        res = (
            run(["git", "-C", wt, "rev-parse", "--path-format=absolute", "--git-common-dir"], SHORT)
            if clone
            else None
        )
        if (
            clone is None
            or res is None
            or res.returncode != 0
            or os.path.realpath(res.stdout.strip()) != os.path.join(clone, ".git")
        ):
            raise Invalid(f"{wt} is not a checkout of {repo.slug}")
    res = run(["git", "-C", wt, "remote", "get-url", "origin"], SHORT)
    if res.returncode != 0 or origin(res.stdout) != repo.slug.lower():
        raise Invalid(f"{wt} is not a checkout of {repo.slug}")
    return wt, True


def clone_repo(repo: Repo, wt: str, *, run: Runner) -> None:
    os.makedirs(os.path.dirname(wt), exist_ok=True)
    # The repo is untrusted: no symlinks that could point outside the checkout.
    res = run(
        [
            "gh",
            "repo",
            "clone",
            repo.slug,
            wt,
            "--",
            "-c",
            "core.symlinks=false",
            "--filter=blob:none",
        ],
        LONG,
    )
    if res.returncode != 0:
        raise RepoCommandError(f"gh repo clone: {err_text(res)}")


def check_branch(branch: str) -> None:
    if not BRANCH.fullmatch(branch):
        raise Invalid(f"unsafe branch name {branch[:80]!r}")


def app_push(wt: str, branch: str, *, run: Runner) -> bool:
    """Whether a dry-run push connects; GitHub refuses it without the App installed."""
    refspec = f"HEAD:refs/heads/{branch}"
    res = run(
        ["git", "-C", wt, "push", "--dry-run", "--no-verify", "--porcelain", "origin", refspec],
        SHORT,
    )
    connected = any(s.startswith("To ") for s in (res.stdout or "").splitlines())
    if res.returncode == 0 or connected:
        return True
    if DENIED.search(res.stderr or ""):
        return False
    raise RepoCommandError(f"git push --dry-run: {err_text(res)}")


def registered(run: Runner, at: str) -> dict[str, str | None]:
    found, path = {}, None
    for line in git(at, "worktree", "list", "--porcelain", run=run).splitlines():
        if line.startswith("worktree "):
            path = os.path.realpath(line[len("worktree ") :])
            found[path] = None
        elif line.startswith("branch refs/heads/") and path:
            found[path] = line[len("branch refs/heads/") :]
    return found


def worktree(
    spec: str,
    branch: str,
    base: str,
    *,
    slug: str | None = None,
    run: Runner = sh,
    temp=None,
    links=frozenset(),
    base_branch: str | None = None,
    author: str | None = None,
) -> dict:
    # A local clone in a temp dir is refused: prune treats clones there as untrusted.
    check_branch(branch)
    slug = check_slug(slug, branch)
    repo, clone = resolve(spec, run=run)
    if clone and (d := under(clone, temp_dirs() if temp is None else temp)):
        raise Invalid(f"{clone} is under the temp dir {d}: keep local clones outside temp dirs")
    symlinks = "true" if clone is not None and clone in links else "false"
    wt, exists = place(repo, base, clone, slug, run=run)
    if exists and clone and os.path.isfile(os.path.join(wt, ".git")):
        index = git(
            wt, "rev-parse", "--path-format=absolute", "--git-path", "index", run=run
        ).strip()
        if not os.path.isfile(index):  # its checkout never finished: it holds nothing
            git(clone, "worktree", "remove", "--force", wt, run=run, timeout=LONG)
            exists = False
        else:
            found = run(
                ["git", "-C", wt, "config", "--worktree", "--get", "core.symlinks"], SHORT
            ).stdout.strip()
            if found != symlinks:
                raise Invalid(
                    f"{wt} has core.symlinks={found or 'unset'}, trusted_dirs gives {symlinks}: "
                    "remove it and run again"
                )
    push, default = info(repo, run=run, app=author is not None)
    if not isinstance(default, str) or not BRANCH.fullmatch(default):
        raise Invalid(f"{repo.slug}: unsafe default branch name")
    if branch == default:
        raise Invalid(f"{branch} is the default branch; build on another")
    onto = base_branch if base_branch is not None else default
    if not isinstance(onto, str) or not BRANCH.fullmatch(onto):
        raise Invalid(f"unsafe base branch name {str(onto)[:80]!r}")
    add = clone is not None and not exists
    # An explicit refspec without the configured map: a mirror-style remote.origin.fetch can't move
    # local branches.
    if exists:
        git(wt, *FETCH, run=run, timeout=LONG)
    elif clone is not None:
        git(clone, *FETCH, run=run, timeout=LONG)
    else:
        clone_repo(repo, wt, run=run)
    at = clone if clone is not None and not exists else wt
    if add or git(wt, "branch", "--show-current", run=run).strip() != branch:
        trees, mine = registered(run, at), os.path.realpath(wt)
        busy = [p for p, b in trees.items() if b == branch and p != mine]
        if busy:
            raise Invalid(f"{branch} is checked out in {busy[0]}")
        if git(at, "branch", "--list", branch, run=run).strip():
            opts, start = [], branch
        elif git(at, "ls-remote", "--heads", "origin", f"refs/heads/{branch}", run=run).strip():
            opts, start = ["--track", "-b", branch], f"origin/{branch}"
        else:
            opts, start = ["--no-track", "-b", branch], f"origin/{onto}"
        if clone is not None and not exists:
            os.makedirs(os.path.dirname(wt), exist_ok=True)
            # core.symlinks per worktree, so the clone's own checkout keeps its config. --force re-adds
            # our own path when it was deleted by hand but is still registered.
            if (
                run(
                    ["git", "-C", clone, "config", "--get", "extensions.worktreeConfig"], SHORT
                ).stdout.strip()
                != "true"
            ):
                git(clone, "config", "extensions.worktreeConfig", "true", run=run)
            force = ["--force"] if mine in trees else []
            git(
                clone,
                "worktree",
                "add",
                "--no-checkout",
                *force,
                *opts,
                wt,
                start,
                run=run,
                timeout=LONG,
            )
            try:
                git(wt, "config", "--worktree", "core.symlinks", symlinks, run=run)
                git(wt, "reset", "-q", "--hard", run=run, timeout=LONG)
            except (GitError, subprocess.TimeoutExpired):
                run(["git", "-C", clone, "worktree", "remove", "--force", wt], LONG)
                raise
        else:
            git(wt, "checkout", *opts, start, run=run, timeout=LONG)
    commit = git(wt, "rev-parse", "HEAD", run=run).strip()
    if not SHA.fullmatch(commit):
        raise RepoCommandError(f"git rev-parse HEAD: {commit[:80]!r}")
    if push is None:
        push = app_push(wt, branch, run=run)
    return {
        "repo": f"{repo.owner}/{repo.name}",
        "host": repo.host,
        "default": default,
        "branch": branch,
        "commit": commit,
        "worktree": wt,
        "permalink_base": f"https://{repo.slug}/blob/{commit}/",
        "push": push,
    }


def plan_docs(wt: str, branch: str) -> list[dict]:
    found = []
    for p in glob.glob(os.path.join(wt, ".updraft", "*plan.md")):
        try:
            with open(p, errors="replace") as fh:
                m = re.search(r"RESUME: phase=(S\d|done)([^\n]*)", fh.read())
            touched = os.path.getmtime(p)
        except OSError:
            continue
        if m and f"branch={branch}" in m.group(2).split():
            found.append((touched, p, m.group(1)))
    return [{"path": p, "phase": phase} for _, p, phase in sorted(found)]


def _author(row: dict, key: str) -> str | None:
    who = row.get(key)
    return who.get("login") if isinstance(who, dict) else None


def _oldest_first(entries: list[dict]) -> list[dict]:
    return sorted(entries, key=lambda e: datetime.fromisoformat(e["at"]))


def status(
    spec: str,
    branch: str,
    base: str,
    *,
    slug: str | None = None,
    base_branch: str | None = None,
    run: Runner = sh,
    users: list[str] | None = None,
    author: str | None = None,
) -> dict:
    slug = check_slug(slug, branch)
    repo, clone = resolve(spec, run=run)
    wt, exists = place(repo, base, clone, slug, run=run)
    if not exists:
        raise Invalid(f"{wt} missing: run worktree first")
    docs = plan_docs(wt, branch)
    if author is None:
        account = gh_json(run, ["api", "--hostname", repo.host, "user"])
        if (
            not isinstance(account, dict)
            or not isinstance(account.get("login"), str)
            or not account["login"]
        ):
            raise RepoCommandError("gh api user: expected a login")
        login = account["login"]
    else:
        login = author
    # gh names a bot author app/<slug>; the REST API, as ENSEMBLE_PR_AUTHOR does, <slug>[bot]
    by = f"app/{login.removesuffix('[bot]')}" if login.endswith("[bot]") else login
    rows = gh_json(
        run,
        [
            "pr",
            "list",
            "--repo",
            repo.slug,
            "--head",
            branch,
            "--state",
            "all",
            "--json",
            "number,url,state,baseRefName,isDraft,isCrossRepository,author,mergeCommit",
            "--limit",
            "100",
        ],
    )
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise RepoCommandError("gh pr list: expected pull request objects")
    mine = [r for r in rows if r.get("isCrossRepository") is False and _author(r, "author") == by]
    if author is not None:
        named = {u.lower() for u in users or ()}
        by_user = {u for u in named if not u.endswith("[bot]")} - {login.lower()}
    else:
        by_user = {u.lower() for u in users} if users else {login.lower()}
    pr = (
        {k: mine[0].get(k) for k in ("number", "url", "state", "baseRefName", "isDraft")}
        if mine
        else None
    )
    onto = (
        pr["baseRefName"]
        if pr
        else base_branch
        if base_branch is not None
        else info(repo, run=run, app=author is not None)[1]
    )
    if not isinstance(onto, str) or not BRANCH.fullmatch(onto):
        raise Invalid(f"unsafe base branch name {str(onto)[:80]!r}")
    user, others, since = [], [], ""
    if pr:
        merged = mine[0].get("mergeCommit")
        oid = merged.get("oid") if isinstance(merged, dict) else None
        # merging puts B's commits on origin/<base>: exclude <base> only as it was before the merge
        off = f"{oid}^1" if isinstance(oid, str) and SHA.fullmatch(oid) else f"origin/{onto}"
        since = git(
            wt,
            "log",
            "-1",
            "--first-parent",
            "--no-merges",
            "--format=%cI",
            f"{off}..HEAD",
            run=run,
        ).strip()
        api = f"repos/{repo.owner}/{repo.name}"
        for path, key, kind in (
            (f"{api}/issues/{pr['number']}/comments", "created_at", "comment"),
            (f"{api}/pulls/{pr['number']}/reviews", "submitted_at", "review"),
            (f"{api}/pulls/{pr['number']}/comments", "created_at", "review_comment"),
        ):
            pages = gh_json(run, ["api", "--hostname", repo.host, "--paginate", "--slurp", path])
            if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
                raise RepoCommandError("gh api: expected comment or review pages")
            for page in pages:
                for c in page:
                    if not isinstance(c, dict):
                        raise RepoCommandError("gh api: expected comment or review objects")
                    at = c.get(key)
                    if at is None:
                        continue
                    if not isinstance(at, str):
                        raise RepoCommandError("gh api: invalid comment or review timestamp")
                    try:
                        parsed = datetime.fromisoformat(at)
                    except ValueError:
                        raise RepoCommandError(
                            "gh api: invalid comment or review timestamp"
                        ) from None
                    if parsed.utcoffset() is None:
                        raise RepoCommandError("gh api: timestamp has no timezone")
                    if since and parsed <= datetime.fromisoformat(since):
                        continue
                    e = {
                        "at": at,
                        "kind": kind,
                        "author": _author(c, "user"),
                        "body": c.get("body") or "",
                    }
                    if kind == "review":
                        e["state"] = c.get("state")
                    if kind == "review_comment":
                        e["path"], e["line"] = (
                            c.get("path"),
                            c.get("line") or c.get("original_line"),
                        )
                    (user if (e["author"] or "").lower() in by_user else others).append(e)
    return {
        "pr": pr,
        "base": onto,
        "plan_docs": docs,
        "since": since or None,
        "user": _oldest_first(user),
        "others": _oldest_first(others),
    }


def main(
    argv: list[str],
    *,
    run: Runner = sh,
    out=sys.stdout,
    err=sys.stderr,
    config: str = CONFIG,
    local: str = LOCAL,
    temp=None,
) -> int:
    ap = argparse.ArgumentParser(prog="repo.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for cmd in ("worktree", "status"):
        p = sub.add_parser(cmd)
        p.add_argument("--dir", required=True, action=Once)
        p.add_argument("--branch", required=True, action=Once)
        p.add_argument("--name", action=Once)
        p.add_argument("--base", action=Once)
        p.add_argument("repo")
    a = ap.parse_args(argv)
    try:
        cfg = read_config(path=config, local=local)
        users = cfg.get("users", [])
        if not isinstance(users, list) or any(not isinstance(user, str) for user in users):
            raise ValueError("users: want a list of strings")
        if (given := os.environ.get("ENSEMBLE_USERS")) is not None:
            try:
                users = json.loads(given)
            except ValueError:
                users = None
            if not isinstance(users, list) or any(not isinstance(user, str) for user in users):
                raise ValueError("ENSEMBLE_USERS: want a JSON array of strings")
        author = os.environ.get("ENSEMBLE_PR_AUTHOR") or None
        links = trusted_dirs(cfg) if a.cmd == "worktree" else frozenset()
    except (ValueError, OSError) as e:
        err.write(f"repo.py: {redact(str(e))}\n")
        return 1
    try:
        if a.cmd == "worktree":
            r = worktree(
                a.repo,
                a.branch,
                a.dir,
                slug=a.name,
                base_branch=a.base,
                run=run,
                temp=temp,
                links=links,
                author=author,
            )
        else:
            r = status(
                a.repo,
                a.branch,
                a.dir,
                slug=a.name,
                base_branch=a.base,
                run=run,
                users=users,
                author=author,
            )
    except Invalid as e:
        err.write(f"repo.py: {redact(str(e))}\n")
        return 2
    except (RepoCommandError, GitError, OSError, subprocess.TimeoutExpired) as e:
        err.write(f"repo.py: {redact(str(e))}\n")
        return 1
    json.dump(r, out)
    out.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
