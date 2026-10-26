"""The credentials inventory names every credential, label and state config key of [linear] and every broker
keychain service, and holds no secret-like value. A prefix item (a service ending in "/") needs its own Keychain items
row, `<prefix><name>`. .github/ci_scope.py's CHECKED_DOCS names INVENTORY too.
"""

import os
import re
import tomllib

from tests.guard_data.orchestrator import ORCH_FILES

INVENTORY = "docs/credentials.md"
CONFIG = f"{ORCH_FILES}config.toml"
SERVICES = "src/broker/internal/broker/keychain.go"
LIST = re.compile(r"var KeychainServices = \[\]string\{(.*?)\}", re.S)
ENTRY = re.compile(r'"([^"]+)",\s*(//.*)?')
PREFIXES = ("ghs_", "ghp_", "gho_", "ghu_", "ghr_", "github_pat_", "lin_api_", "sk-ant-")
PATTERNS = (
    *((re.compile(re.escape(prefix)), prefix) for prefix in PREFIXES),
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"), "-----BEGIN … PRIVATE KEY-----"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\."), "JWT"),
)


def credentials_inventory(repo: str) -> list[str]:
    paths = {rel: os.path.join(repo, rel) for rel in (INVENTORY, CONFIG, SERVICES)}
    missing = [f"{rel}: missing" for rel, path in paths.items() if not os.path.isfile(path)]
    if missing:
        return missing
    texts = {}
    for rel, path in paths.items():
        with open(path, encoding="utf-8") as f:
            texts[rel] = f.read()
    try:
        config = tomllib.loads(texts[CONFIG])
    except tomllib.TOMLDecodeError as error:
        return [f"{CONFIG}: {error}"]
    services, found = _services(texts[SERVICES])
    if services is None:
        return [*found, f"{SERVICES}: no KeychainServices list"]
    return found + _violations(config, services, texts[INVENTORY])


def _services(go):
    match = LIST.search(go)
    names, found = [], []
    for line in match[1].splitlines() if match else ():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        if entry := ENTRY.fullmatch(line):
            names.append(entry[1])
        else:
            found.append(f"{SERVICES}: KeychainServices entry is not a string literal: {line}")
    return names or None, found


def _required_keys(config):
    """The label, state and OAuth app keys of config's [linear], each as linear.<key>."""
    keys = []
    for key, value in config.get("linear", {}).items():
        if not isinstance(value, dict):
            keys += [key] if key.endswith("_label_group") else []
        elif {"client_id", "secret_service", "token_service"} <= set(value):  # an OAuth app
            keys += [f"{key}.{field}" for field in value]
        elif key == "states":
            keys += [f"states.{state}" for state in value]
        elif key.endswith("_labels"):
            keys.append(key)
    return [f"linear.{key}" for key in keys]


def _prefix_row(prefix):
    """A Keychain items row whose Service cell is `<prefix><placeholder>`."""
    return re.compile(rf"^\|\s*`{re.escape(prefix)}<[^>`]+>`\s*\|", re.M)


def _violations(config, services, text):
    found = [
        f"{INVENTORY}: missing config key `{key}`"
        for key in _required_keys(config)
        if f"`{key}`" not in text
    ]
    found += [
        f"{INVENTORY}: missing keychain service `{name}`"
        for name in services
        if not name.endswith("/") and f"`{name}`" not in text
    ]
    found += [
        f"{INVENTORY}: missing keychain prefix item row `{name}<name>`"
        for name in services
        if name.endswith("/") and not _prefix_row(name).search(text)
    ]
    found += [
        f"{INVENTORY}:{n}: secret-like value ({label})"
        for n, line in enumerate(text.splitlines(), 1)
        for pattern, label in PATTERNS
        if pattern.search(line)
    ]
    return found
