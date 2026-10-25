import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import unquote

from tests.utils.sandbox import clean_env, git_run

ENV = "FAKE_GH_SCENARIO"
RAW = "Accept: application/vnd.github.raw+json"
API = re.compile(r"repos/([^/]+)/([^/]+)(?:/contents/([^?]+)\?ref=([^?]+))?")
THREAD = re.compile(r"repos/([^/]+)/([^/]+)/(issues/\d+/comments|pulls/\d+/(?:reviews|comments))")
HOST = "github.com"
PR_LIST = ("--repo", "--head", "--state", "--json", "--limit")
KEYS = {"repos", "contents", "login", "pulls", "threads", "views"}
CLONE = re.compile(r"(?:github\.com/)?([^/]+)/([^/]+)")
# git's ssh, variant simple: HOST "git-upload-pack '/O/N.git'"; it serves the one bare repo
SSH = '#!/bin/sh\nset -- $2\nexec git "${1#git-}" %s\n'
TAIL = "/usr/bin:/bin"
# the repo root, for tests.utils.sandbox: an agent run's PYTHONPATH holds only the src roots
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), *[os.pardir] * 3))


def install(bin_dir: str) -> str:
    path = os.path.join(bin_dir, "gh")
    with open(path, "w") as f:
        f.write(
            f"#!/bin/sh\nPYTHONPATH={shlex.quote(ROOT)} exec {shlex.quote(sys.executable)} -P -S "
            f'{shlex.quote(os.path.abspath(__file__))} "$@"\n'
        )
    os.chmod(path, 0o755)
    return path


def bare(root: str, owner: str, name: str, branches=()) -> str:
    path = os.path.join(root, owner, f"{name}.git")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with tempfile.TemporaryDirectory() as home:
        env = clean_env({"HOME": home, "PATH": TAIL})

        def git(*args: str) -> str:
            res = git_run(*args, env=env, input="", check=False)
            if res.returncode != 0:
                raise GitCommandError(args, res.stderr.strip())
            return res.stdout.strip()

        git("init", "--bare", "--quiet", "--initial-branch=main", path)
        tree = git("--git-dir", path, "mktree")
        for branch in branches:
            commit = git("--git-dir", path, "commit-tree", tree, "-m", branch)
            git("--git-dir", path, "update-ref", f"refs/heads/{branch}", commit)
    return path


def git_shim(bin_dir: str, home: str) -> None:
    executable = shutil.which("git", path=TAIL)
    if executable is None:
        raise FileNotFoundError("git")
    argv = [
        executable,
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "init.templateDir=",
        "-c",
        "protocol.allow=never",
        "-c",
        "protocol.file.allow=always",
        "-c",
        f"include.path={os.path.join(home, '.gitconfig')}",
    ]
    path = os.path.join(bin_dir, "git")
    with open(path, "w") as f:
        f.write(f'#!/bin/sh\nexec {shlex.join(argv)} "$@"\n')
    os.chmod(path, 0o700)


def gitconfig(home: str, root: str) -> None:
    # `home` is a test's temp HOME, never the real one.
    with open(os.path.join(home, ".gitconfig"), "w") as f:
        f.write(f'[url "file://{root}/"]\n\tinsteadOf = https://github.com/\n')


def nested(value, depth: int) -> bool:
    return (
        isinstance(value, str)
        if depth == 0
        else isinstance(value, dict) and all(nested(v, depth - 1) for v in value.values())
    )


def scenario() -> dict:
    path = os.environ.get(ENV)
    if not path:
        sys.exit(f"fake_gh.py: {ENV} is unset")
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        sys.exit(f"fake_gh.py: {ENV}: {e}")
    if not isinstance(data, dict) or not {"repos"} <= set(data) <= KEYS:
        sys.exit(
            'fake_gh.py: the scenario is no JSON object with the key "repos" and optionally '
            '"contents", "login", "pulls", "threads" and "views"'
        )
    if not isinstance(data["repos"], dict) or not all(
        isinstance(v, dict) for v in data["repos"].values()
    ):
        sys.exit('fake_gh.py: "repos" is no object of objects')
    if not nested(data.setdefault("contents", {}), 3):
        sys.exit('fake_gh.py: "contents" is no object of objects of objects of strings')
    if not isinstance(data.setdefault("login", ""), str):
        sys.exit('fake_gh.py: "login" is no string')
    pulls = data.setdefault("pulls", {})
    if not objects(pulls, 1) or not all(
        isinstance(p.get("headRefName"), str) for prs in pulls.values() for p in prs
    ):
        sys.exit('fake_gh.py: "pulls" is no object of lists of objects with a headRefName')
    if not objects(data.setdefault("threads", {}), 2):
        sys.exit('fake_gh.py: "threads" is no object of objects of lists of objects')
    views = data.setdefault("views", {})
    if not isinstance(views, dict) or not all(isinstance(v, dict) for v in views.values()):
        sys.exit('fake_gh.py: "views" is no object of objects')
    return data


def objects(value, depth: int) -> bool:
    """`depth` levels of objects over lists of objects."""
    return (
        isinstance(value, list) and all(isinstance(v, dict) for v in value)
        if depth == 0
        else isinstance(value, dict) and all(objects(v, depth - 1) for v in value.values())
    )


def entries(files: dict, path: str) -> list[dict] | None:
    below = [f[len(path) + 1 :] for f in files if f.startswith(path + "/")]
    if not below:
        return None
    kinds = {rest.partition("/")[0]: "dir" if "/" in rest else "file" for rest in below}
    return [{"name": n, "path": f"{path}/{n}", "type": kinds[n]} for n in sorted(kinds)]


def login() -> None:
    if not (name := scenario()["login"]):
        sys.exit("fake_gh.py: the scenario has no login")
    print(json.dumps({"login": name}))


def thread(m: re.Match) -> None:
    data, repo = scenario(), f"{m[1]}/{m[2]}"
    if repo not in data["repos"]:
        sys.exit("gh: Not Found (HTTP 404)")
    print(json.dumps([data["threads"].get(repo, {}).get(m[3], [])]))


def pr_list(rest: list[str]) -> None:
    options = dict(zip(rest[::2], rest[1::2], strict=False))
    if (
        tuple(rest[::2]) != PR_LIST
        or len(rest) != 2 * len(PR_LIST)
        or (options["--state"], options["--limit"]) != ("all", "100")
    ):
        sys.exit(f"fake_gh.py: usage: gh pr list {' '.join(f'{o} X' for o in PR_LIST)}")
    repo, fields = options["--repo"].removeprefix(HOST + "/"), options["--json"].split(",")
    print(
        json.dumps(
            [
                {f: p[f] for f in fields if f in p}
                for p in scenario()["pulls"].get(repo, [])
                if p["headRefName"] == options["--head"]
            ]
        )
    )


def pr_view(rest: list[str]) -> None:
    """`gh pr view URL --json FIELDS`: the asked fields of `views`' URL."""
    if len(rest) != 3 or rest[1] != "--json":
        sys.exit("fake_gh.py: usage: gh pr view URL --json FIELDS")
    if (view := scenario()["views"].get(rest[0])) is None:
        sys.exit(f"no pull requests found for {rest[0]}")
    print(json.dumps({f: view[f] for f in rest[2].split(",") if f in view}))


def pr_ready(rest: list[str]) -> None:
    """`gh pr ready URL [--undo]`: sets the view's isDraft in the scenario file."""
    if len(rest) not in (1, 2) or rest[1:] not in ([], ["--undo"]):
        sys.exit("fake_gh.py: usage: gh pr ready URL [--undo]")
    data = scenario()
    if rest[0] not in data["views"]:
        sys.exit(f"no pull requests found for {rest[0]}")
    data["views"][rest[0]]["isDraft"] = rest[1:] == ["--undo"]
    path = os.environ[ENV]
    with open(f"{path}.tmp", "w") as f:
        json.dump(data, f)
    os.replace(f"{path}.tmp", path)


def git(*argv: str) -> str:
    done = subprocess.run(["git", *argv], capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        sys.stderr.write(done.stderr)
        sys.exit(done.returncode)
    return done.stdout.strip()


def repo_clone(rest: list[str]) -> None:
    """`gh repo clone O/N DIR -- FLAGS`: git clone through HOME's gitconfig, then origin over a
    local ssh: `git remote get-url` expands that gitconfig's insteadOf, which repo.py checks.
    """
    m = CLONE.fullmatch(rest[0]) if len(rest) >= 3 and rest[2] == "--" else None
    if not m:
        sys.exit("fake_gh.py: usage: gh repo clone [github.com/]O/N DIR -- [GIT FLAGS]")
    wt = rest[1]
    git("clone", *rest[3:], f"https://{HOST}/{m[1]}/{m[2]}.git", wt)
    bare = git("-C", wt, "remote", "get-url", "origin").removeprefix("file://")
    ssh = os.path.join(wt, ".git", "fake-ssh")
    with open(ssh, "w") as f:
        f.write(SSH % shlex.quote(bare))
    os.chmod(ssh, 0o700)
    for key, value in (
        ("remote.origin.url", f"ssh://git@{HOST}/{m[1]}/{m[2]}.git"),
        ("core.sshCommand", ssh),
        ("ssh.variant", "simple"),
        ("protocol.ssh.allow", "always"),
    ):
        git("-C", wt, "config", key, value)


def main(argv: list[str]) -> None:
    if argv[:2] == ["pr", "list"]:
        return pr_list(argv[2:])
    if argv[:2] == ["pr", "view"]:
        return pr_view(argv[2:])
    if argv[:2] == ["pr", "ready"]:
        return pr_ready(argv[2:])
    if argv[:2] == ["repo", "clone"]:
        return repo_clone(argv[2:])
    if argv[:3] == ["search", "prs", "--merged"]:
        return print("[]")
    if argv[:3] == ["api", "--hostname", HOST]:
        rest = argv[3:]
        if rest == ["user"]:
            return login()
        if rest[:2] == ["--paginate", "--slurp"] and (m := THREAD.fullmatch(" ".join(rest[2:]))):
            return thread(m)
        argv = ["api", *rest]
    raw = argv[1:3] == ["-H", RAW]
    rest = argv[3:] if raw else argv[1:]
    m = API.fullmatch(rest[0]) if argv[:1] == ["api"] and len(rest) == 1 else None
    if not m or (raw and not m[3]):
        sys.exit(
            f"fake_gh.py: usage: gh api [-H '{RAW}'] repos/<owner>/<name>[/contents/<path>?ref=<branch>]"
        )
    data, repo = scenario(), f"{m[1]}/{m[2]}"
    if not m[3]:
        if repo not in data["repos"]:
            sys.exit("gh: Not Found (HTTP 404)")
        print(json.dumps(data["repos"][repo]))
        return
    path = unquote(m[3])
    files = data["contents"].get(repo, {}).get(unquote(m[4]), {})
    if (listing := entries(files, path)) is not None:
        print(json.dumps(listing))
    elif path not in files:
        sys.exit("gh: Not Found (HTTP 404)")
    elif raw:
        sys.stdout.buffer.write(files[path].encode())
    else:
        print(json.dumps({"name": path.rsplit("/", 1)[-1], "path": path, "type": "file"}))


class GitCommandError(RuntimeError):
    def __init__(self, argv, stderr):
        super().__init__(f"git {' '.join(argv)}: {stderr}")


if __name__ == "__main__":
    main(sys.argv[1:])
