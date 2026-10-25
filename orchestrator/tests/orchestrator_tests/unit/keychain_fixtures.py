"""A fake Keychain behind /usr/bin/security and a fake Linear token endpoint, for app identities."""

import io
import json
import re
import shlex
import subprocess
import urllib.error
import urllib.parse
from datetime import UTC, datetime, timedelta
from unittest import mock

from ensemble.orchestrator.engine.types import AppKind, LinearApp
from ensemble.orchestrator.trackers.linear import client

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
SECRET = "app-SECRET-sentinel"
OLD = "tok-OLD-sentinel"
NEW = "tok-NEW-sentinel"
HARNESS = LinearApp(
    "harness",
    AppKind.HARNESS,
    "linear.harness",
    "client-harness",
    "linear-app-harness-secret",
    "linear-app-harness-token",
    "00000000-0000-4000-8000-000000000201",
)
SENTINELS = (SECRET, OLD, NEW, SECRET.encode().hex(), OLD.encode().hex(), NEW.encode().hex())


def record(token=OLD, days=10.0, now=NOW):
    return json.dumps(
        {"access_token": token, "expires_at": (now + timedelta(days=days)).isoformat()},
        separators=(",", ":"),
    )


class Keychain:
    """services: {service: (account, value)}; calls: each security (argv, stdin); fail_write: an
    -i write leaves the entry as it was.
    """

    def __init__(self, entries=None, *, fail_write=False):
        self.items = {s: (s, v) for s, v in (entries or {}).items()}
        self.calls, self.fail_write = [], fail_write

    def value(self, service):
        found = self.items.get(service)
        return found and found[1]

    def __call__(self, argv, *args, **kw):
        if not argv or argv[0] != client.SECURITY:
            raise AssertionError(f"unexpected subprocess: {argv}")
        self.calls.append((list(argv), kw.get("input")))
        if argv[1:2] == ["-i"]:
            return self._interactive(argv, kw.get("input") or "")
        service = argv[argv.index("-s") + 1]
        found = self.items.get(service)
        if found is None:
            if kw.get("check"):
                raise subprocess.CalledProcessError(44, argv)
            return subprocess.CompletedProcess(argv, 44, stdout="", stderr="not found")
        if "-w" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=found[1] + "\n", stderr="")
        attrs = f'attributes:\n    "acct"<blob>="{found[0]}"\n    "svce"<blob>="{service}"\n'
        return subprocess.CompletedProcess(argv, 0, stdout=attrs, stderr="")

    def _interactive(self, argv, text):
        for line in text.splitlines():
            words = shlex.split(line)
            if words[:1] != ["add-generic-password"] or self.fail_write:
                continue
            opt = {words[i]: words[i + 1] for i in range(1, len(words) - 1) if words[i] != "-U"}
            self.items[opt["-s"]] = (opt["-a"], bytes.fromhex(opt["-X"]).decode())
        return subprocess.CompletedProcess(argv, 0, stdout="security> " * 2, stderr="")


class Endpoint:
    """urlopen for the token endpoint (answer: a dict, bytes, or an exception) and GraphQL (data);
    tokens: each token request's form fields; graphql: each GraphQL request."""

    def __init__(self, answer=None, data=None):
        self.answer = {"access_token": NEW, "expires_in": 30 * 86400} if answer is None else answer
        self.data, self.tokens, self.graphql = data or {}, [], []

    def __call__(self, req, *args, **kw):
        if req.full_url == client.TOKEN_URL:
            self.tokens.append(dict(urllib.parse.parse_qsl(req.data.decode())))
            if isinstance(self.answer, BaseException):
                raise self.answer
            body = self.answer if isinstance(self.answer, bytes) else json.dumps(self.answer)
            return _response(body if isinstance(body, bytes) else body.encode())
        self.graphql.append(req)
        return _response(json.dumps({"data": self.data}).encode())


def _response(body):
    resp = mock.MagicMock()
    resp.__enter__.return_value = io.BytesIO(body)
    return resp


def token_http_error(code, body=b""):
    return urllib.error.HTTPError(client.TOKEN_URL, code, "Error", {}, io.BytesIO(body))


def leaks(texts):
    """Each sentinel secret or token, plain or hex, found in texts."""
    joined = "\n".join(texts)
    return [s for s in SENTINELS if re.search(re.escape(s), joined)]
