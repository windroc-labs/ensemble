import json
import os
import stat
import subprocess
from pathlib import Path

home = Path(os.environ["CODEX_HOME"])
cache = Path("/opt/ensemble/codex-plugins/plugins/cache")
(root,) = (cache / "windroc/updraft").iterdir()
manifest = next(
    path
    for path in (root / ".codex-plugin/plugin.json", root / ".claude-plugin/plugin.json")
    if path.is_file()
)
skill = root / "skills/light-build/SKILL.md"
for path in (root, manifest, skill):
    assert path.stat().st_uid == 0
    assert not path.stat().st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    assert not os.access(path, os.W_OK)
assert home.joinpath("plugins/cache").resolve() == cache
assert not root.is_symlink()
assert json.loads(manifest.read_text())["name"] == "updraft"
proc = subprocess.Popen(
    ["codex", "app-server"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.DEVNULL,
    text=True,
)


def request(identifier, method, params):
    proc.stdin.write(json.dumps({"id": identifier, "method": method, "params": params}) + "\n")
    proc.stdin.flush()
    for line in proc.stdout:
        reply = json.loads(line)
        if reply.get("id") == identifier:
            assert "error" not in reply
            return reply["result"]
    raise AssertionError("app-server closed before its reply")


try:
    request(
        0,
        "initialize",
        {"clientInfo": {"name": "ensemble_ci", "title": "Ensemble CI", "version": "0.0.1"}},
    )
    proc.stdin.write(json.dumps({"method": "initialized", "params": {}}) + "\n")
    proc.stdin.flush()
    found = request(1, "skills/list", {"cwds": ["/workspace"], "forceReload": True})
    (entry,) = found["data"]
    assert entry["cwd"] == "/workspace"
    assert not entry["errors"]
    (loaded,) = [s for s in entry["skills"] if s["name"] == "updraft:light-build"]
    assert loaded["enabled"] is True
    assert loaded["pluginId"] == "updraft@windroc"
    assert Path(loaded["path"]).resolve() == skill.resolve()
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
