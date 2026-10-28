import os
import sys
from pathlib import Path

client, hook, uid, gid = sys.argv[1:]
assert os.getuid() == int(uid) and os.getgid() == int(gid)
for process in ("self", "1"):
    status = dict(
        line.split(":", 1) for line in Path(f"/proc/{process}/status").read_text().splitlines()
    )
    for field in ("CapInh", "CapPrm", "CapEff", "CapAmb"):
        assert int(status[field], 16) == 0, (process, field, status[field])
    assert int(status["NoNewPrivs"]) == 1
    assert int(status["CapBnd"], 16) == (0xC0 if hook else 0)
state = Path("/tmp/ci-state") / (".claude.json" if client == "claude" else "config.toml")
assert state.stat().st_uid == int(uid) and state.stat().st_gid == int(gid)
if hook:
    assert Path(hook).stat().st_uid == 0
    assert os.access(hook, os.X_OK) and not os.access(hook, os.W_OK)
