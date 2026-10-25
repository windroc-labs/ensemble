"""OpenCode unit fixtures: the packaged [clients.opencode] table, a session id and a fake
opencode.db."""

import json
import os
import sqlite3
from contextlib import closing
from types import SimpleNamespace

from ensemble.session.clients import registry
from ensemble.session.engine.config import read_config
from ensemble.session.engine.types import ClientContext, Container, Permission

SES = "ses_0123456789abABCDEFGHIJKLMN"
# The Docker Host's (hosts/docker/host.py _client_context).
DOCKER_CONTEXT = ClientContext(
    "host.docker.internal",
    Permission.YOLO,
    Container(
        "/opt/ensemble/bin",
        "/opt/ensemble/venv/bin/python",
        "/opt/ensemble/session/src:/opt/ensemble/utils/src",
        "/run/ensemble",
    ),
)
IMAGE_CLIENT = "/opt/ensemble/session/src/ensemble/session/clients/opencode"


def packaged_table() -> dict:
    """The packaged session config.toml's [clients.opencode], without implementation."""
    table = dict(registry.settings(read_config(local=os.devnull))["clients"]["opencode"])
    table.pop("implementation")
    return table


VERSION = "1.19.3"


def fake_run(argv: list[str], *_: object, **__: object) -> SimpleNamespace:
    """subprocess.run of `<opencode> --version` (VERSION), or of git rev-parse in a dir holding a
    .git dir: its own toplevel.
    """
    if argv[1:] == ["--version"]:
        return SimpleNamespace(returncode=0, stdout=VERSION + "\n", stderr="")
    top = argv[argv.index("-C") + 1]
    ok = os.path.isdir(os.path.join(top, ".git"))
    out = top + "\n" if "--show-toplevel" in argv else ".git\n.git\n"
    return SimpleNamespace(returncode=0 if ok else 128, stdout=out if ok else "", stderr="fatal")


SCHEMA = (
    "CREATE TABLE session (id TEXT PRIMARY KEY, parent_id TEXT, directory TEXT, title TEXT,"
    " time_created INTEGER, time_updated INTEGER)",
    "CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER,"
    " time_updated INTEGER, data TEXT)",
    "CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,"
    " time_created INTEGER, time_updated INTEGER, data TEXT)",
)


def make_db(path, sessions=None, messages=()) -> str:
    """An opencode.db in OpenCode 1.19.3's layout: `sessions` maps id → time_updated (ms);
    each message is (session, role, time_created ms, [part data dicts]).
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with closing(sqlite3.connect(path)) as db, db:
        for statement in SCHEMA:
            db.execute(statement)
        for ident, updated in (sessions or {}).items():
            db.execute(
                "INSERT INTO session VALUES (?, NULL, '/', 't', ?, ?)", (ident, updated, updated)
            )
        for index, (session, role, created, parts) in enumerate(messages):
            mid = f"msg_{index:04d}"
            db.execute(
                "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
                (mid, session, created, created, json.dumps({"role": role})),
            )
            for number, data in enumerate(parts):
                db.execute(
                    "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        f"prt_{index:04d}_{number:02d}",
                        mid,
                        session,
                        created,
                        created,
                        data if isinstance(data, str) else json.dumps(data),
                    ),
                )
    return str(path)


def text(value: str, **flags) -> dict:
    return {"type": "text", "text": value, **flags}
