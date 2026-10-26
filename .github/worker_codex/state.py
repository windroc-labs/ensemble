import json
import os
import stat
import tempfile
from pathlib import Path

home = Path(os.environ["CODEX_HOME"])
mounts = {}
for line in Path("/proc/self/mountinfo").read_text().splitlines():
    before, after = line.split(" - ", 1)
    fields = before.split()
    mounts[fields[4]] = (after.split()[0], set(fields[5].split(",")))
for path in (str(home), "/run/ensemble", "/run/ensemble/secrets"):
    kind, options = mounts[path]
    assert kind == "tmpfs"
    assert {"noexec", "nosuid", "nodev"} <= options
assert "/opt/ensemble/runtime" not in mounts and not Path("/opt/ensemble/runtime").exists()
assert os.readlink("/opt/ensemble/bin/codex") == "/opt/ensemble/clients/bin/codex"
secret = Path("/run/ensemble/secrets/codex/auth.json")
assert stat.S_IMODE(secret.stat().st_mode) == 0o400
assert secret.stat().st_uid == os.getuid() != 0
assert home.joinpath("auth.json").is_symlink()
document = json.loads(secret.read_bytes())
assert document["tokens"]["refresh_token"] == ""
assert document["auth_mode"] == "chatgpt"
assert document["tokens"]["account_id"] == "ci-fake"
assert document["tokens"]["id_token"]
identity = home / "sessions/identity.txt"
if identity.exists():
    assert identity.read_text() == "synthetic-native-identity"
else:
    identity.write_text("synthetic-native-identity")
fd, temporary = tempfile.mkstemp(dir=home)
with os.fdopen(fd, "w") as output:
    json.dump(document, output)
os.replace(temporary, home / "auth.json")
assert not home.joinpath("auth.json").is_symlink()
assert home.joinpath("auth.json").read_bytes()
for path in (home / "sessions").rglob("*"):
    if path.is_file():
        assert document["tokens"]["access_token"].encode() not in path.read_bytes()
