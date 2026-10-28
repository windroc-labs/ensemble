"""site_copy: the session and utils packages copied alone, for the tests of an installed session."""

import json
import os
import shutil

from tests.session import fake_client
from tests.session.case import SRC

SKIP = shutil.ignore_patterns("__pycache__")
SCENARIO = (
    {"out": {"type": "session", "id": "native-1"}},
    {"out": {"type": "message", "text": "hi"}},
    {"out": {"type": "result", "text": "done"}},
)


def site_copy(root: str, *, client: tuple[str, str] | None = None) -> str:
    """root/site with ensemble/session and ensemble/utils; `client` (dir, module): the fake client
    copied to dir/module, its [clients.fake] table appended to the copy's config.toml (0600).
    """
    site = os.path.join(root, "site")
    for package in ("session", "utils"):
        shutil.copytree(
            os.path.join(SRC[package], "ensemble", package),
            os.path.join(site, "ensemble", package),
            ignore=SKIP,
        )
    if client:
        where, module = client
        shutil.copytree(
            os.path.dirname(fake_client.__file__), os.path.join(where, module), ignore=SKIP
        )
        scenario, log = (os.path.join(root, name) for name in ("scenario.json", "log.jsonl"))
        with open(scenario, "w", encoding="utf-8") as f:
            json.dump(SCENARIO, f)
        config = os.path.join(site, "ensemble", "session", "config.toml")
        os.chmod(config, 0o600)
        with open(config, "a", encoding="utf-8") as f:
            f.write(
                f'\n[clients.fake]\nimplementation = "{module}:FakeClient"\n'
                f"scenario = {json.dumps(scenario)}\nlog = {json.dumps(log)}\n"
            )
    return site
