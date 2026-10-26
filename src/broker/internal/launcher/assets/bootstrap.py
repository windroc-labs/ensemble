import contextlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import tomllib
from pathlib import Path

TEMPLATES = Path("/opt/ensemble/templates")
STUB = Path("/opt/ensemble/lib/hook-stub")
SECRETS = Path("/run/ensemble/secrets")
MINI_OP_SKILL = Path("/opt/windroc/mini-op/skills/mini-op")


def bootstrap_skill(directory):
    if not MINI_OP_SKILL.exists():
        return
    if not (MINI_OP_SKILL / "SKILL.md").is_file():
        sys.exit("bootstrap: mini-op skill has no SKILL.md")
    skills = directory / "skills"
    if skills.is_symlink():
        sys.exit("bootstrap: skills must be a directory, not a link")
    skills.mkdir(mode=0o700, exist_ok=True)
    destination = skills / "mini-op"
    if destination.is_symlink() and destination.readlink() == MINI_OP_SKILL:
        return
    if os.path.lexists(destination):
        sys.exit("bootstrap: mini-op skill already exists")
    destination.symlink_to(MINI_OP_SKILL, target_is_directory=True)


def bootstrap_claude(templates, directory, cwd):
    for template in templates.iterdir():
        destination = directory / template.name
        if not os.path.lexists(destination):
            shutil.copyfile(template, destination)
    config = directory / ".claude.json"
    if config.is_symlink():
        sys.exit("bootstrap: .claude.json must not be a symlink")
    data = json.loads(config.read_text())
    data.setdefault("projects", {}).setdefault(cwd, {})["hasTrustDialogAccepted"] = True
    fd = os.open(config, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
    with os.fdopen(fd, "w") as stream:
        json.dump(data, stream)
    bootstrap_skill(directory)


def install_hook(stub, destination, owner=0, top=Path("/")):
    for parent in reversed(destination.parents):
        if parent != top and top not in parent.parents:
            continue
        if not os.path.lexists(parent):
            parent.mkdir(mode=0o755)
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != owner or info.st_mode & 0o022:
            sys.exit(
                f"bootstrap: {parent} must be a directory owned by {owner} and not group or other writable"
            )
    if os.path.lexists(destination):
        sys.exit(f"bootstrap: {destination} already exists")
    with destination.open("xb") as stream:
        stream.write(stub.read_bytes())
    destination.chmod(0o755)


def bootstrap_codex(directory, cwd, secrets=None, plugins=Path("/opt/ensemble/codex-plugins")):
    secrets = SECRETS if secrets is None else secrets
    native = tomllib.loads((plugins / "config.toml").read_text()).get("plugins", {})
    enabled = "".join(
        f"[plugins.{json.dumps(identifier)}]\nenabled = true\n" for identifier in sorted(native)
    )
    (directory / "config.toml").write_text(
        'cli_auth_credentials_store = "file"\n'
        'forced_login_method = "chatgpt"\n'
        "check_for_update_on_startup = false\n"
        f'[projects.{json.dumps(cwd)}]\ntrust_level = "trusted"\n' + enabled
    )
    (directory / "plugins").mkdir(mode=0o700)
    (directory / "plugins/cache").symlink_to(plugins / "plugins/cache", target_is_directory=True)
    (directory / "auth.json").symlink_to(secrets / "codex/auth.json")
    bootstrap_skill(directory)


def inject_codex(stream, secrets=None, directory=None):
    """Writes the access-only document to the secret file and points CODEX_HOME/auth.json at it.

    Nothing new lands in CODEX_HOME but the link: Codex's own state fills that tmpfs. Every exit
    message is fixed text, never the document's.
    """

    def reject(why="invalid managed Codex credentials"):
        sys.exit(f"bootstrap: {why}")

    def unique(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("duplicate credential field")
        return result

    secrets = SECRETS if secrets is None else secrets
    if directory is None:
        value = os.environ.get("CODEX_HOME", "")
        if not value:
            reject("invalid Codex home")
        directory = Path(value)
    if not directory.is_absolute() or directory.is_symlink() or not directory.is_dir():
        reject("invalid Codex home")
    try:
        raw = stream.read((1 << 16) + 1)
        data = json.loads(raw, object_pairs_hook=unique)
        tokens = data["tokens"]
        valid = not (
            len(raw) > 1 << 16
            or set(data) != {"auth_mode", "OPENAI_API_KEY", "tokens", "last_refresh"}
            or data["auth_mode"] != "chatgpt"
            or data["OPENAI_API_KEY"] is not None
            or not isinstance(tokens, dict)
            or set(tokens) != {"id_token", "access_token", "refresh_token", "account_id"}
            or tokens["refresh_token"] != ""
            or any(
                not isinstance(tokens[key], str) or not tokens[key]
                for key in ("id_token", "access_token", "account_id")
            )
            or not isinstance(data["last_refresh"], str)
        )
    except (ValueError, TypeError, KeyError, OSError, RecursionError):
        valid = False
    if not valid:
        reject()
    secret = secrets / "codex/auth.json"
    link = directory / "auth.json"
    if secrets.is_symlink() or secret.parent.is_symlink():
        reject("managed Codex secret directory is a link")
    managed = link.is_symlink()
    if managed and link.readlink() != secret:
        reject("CODEX_HOME/auth.json links elsewhere")
    effective = secret
    if not managed and os.path.lexists(link):
        if not stat.S_ISREG(link.lstat().st_mode):
            reject("CODEX_HOME/auth.json is not a regular file")
        effective = link
    if os.path.lexists(effective):
        if not stat.S_ISREG(effective.lstat().st_mode):
            reject("managed Codex credential is not a regular file")
        try:
            with effective.open("rb") as current:
                account = json.loads(current.read((1 << 16) + 1))["tokens"]["account_id"]
        except (ValueError, TypeError, KeyError, OSError, RecursionError):
            reject("existing Codex credentials are unreadable")
        if account != tokens["account_id"]:
            reject("existing Codex credentials belong to another account")
    staged = pointer = None
    try:
        secret.parent.mkdir(mode=0o700, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".auth-", dir=secret.parent)
        staged = Path(name)
        os.fchmod(fd, 0o400)
        with os.fdopen(fd, "wb") as output:
            output.write(raw)
        os.replace(staged, secret)
        if not managed:
            while True:
                candidate = directory / f".auth-link-{os.urandom(16).hex()}"
                try:
                    os.symlink(secret, candidate)
                except FileExistsError:
                    continue
                pointer = candidate
                break
            os.replace(pointer, link)
    except OSError as error:
        reject(f"cannot write managed Codex credentials: {error.strerror or type(error).__name__}")
    finally:
        for path in (staged, pointer):
            if path is not None:
                with contextlib.suppress(OSError):
                    path.unlink(missing_ok=True)


def stage_runtime(stream, root=Path("/run/ensemble")):
    files = json.load(stream)
    if not isinstance(files, dict) or len(files) != 1:
        sys.exit("bootstrap: expected one recovery record")
    for name, text in files.items():
        path = Path(name)
        if (
            path.parent.parent != root
            or not path.parent.name.endswith("-store")
            or not re.fullmatch(r"[0-9a-f]{64}\.json", path.name)
            or not isinstance(text, str)
            or len(text.encode()) > 1 << 20
        ):
            sys.exit("bootstrap: invalid recovery projection")
        path.parent.mkdir(mode=0o700, exist_ok=True)
        if path.parent.is_symlink():
            sys.exit("bootstrap: recovery directory is a link")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as output:
            output.write(text)


def main(argv):
    if len(argv) == 3 and argv[0] == "claude":
        bootstrap_claude(TEMPLATES, Path(argv[1]), argv[2])
    elif len(argv) == 3 and argv[0] == "codex":
        bootstrap_codex(Path(argv[1]), argv[2])
    elif argv == ["codex-auth"]:
        inject_codex(sys.stdin.buffer)
    elif len(argv) == 2 and argv[0] == "codex-auth":
        if argv[1] != os.environ.get("CODEX_HOME"):
            sys.exit("bootstrap: CODEX_HOME does not match the manifest")
        inject_codex(sys.stdin.buffer, directory=Path(argv[1]))
    elif argv == ["runtime-files"]:
        stage_runtime(sys.stdin)
    elif len(argv) == 2 and argv[0] == "hook":
        install_hook(STUB, Path(argv[1]))
    else:
        sys.exit("usage: bootstrap.py claude DIR CWD | hook PATH")


if __name__ == "__main__":
    main(sys.argv[1:])
