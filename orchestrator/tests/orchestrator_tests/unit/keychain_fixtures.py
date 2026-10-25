"""A fake Keychain behind /usr/bin/security and a fake Linear token endpoint, for app identities."""

import io
import json
import re
import shlex
import urllib.error
import urllib.parse
from datetime import UTC, datetime, timedelta
from unittest import mock

from ensemble.orchestrator.engine.types import AppKind, LinearApp
from ensemble.orchestrator.trackers.linear import client
from orchestrator_tests.case import system_probe
from orchestrator_tests.process_fixtures import CalledProcessError, CompletedProcess

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


def record(token=OLD, days=10.0):
    """A token record expiring days from now."""
    expires = datetime.now(UTC) + timedelta(days=days)
    return json.dumps(
        {"access_token": token, "expires_at": expires.isoformat()}, separators=(",", ":")
    )


def renewed(case, value, since):
    """Asserts value is NEW's record, expiring 30 days after the renewal (made after since)."""
    found = json.loads(value)
    expires = datetime.fromisoformat(found.pop("expires_at"))
    case.assertEqual(found, {"access_token": NEW})
    case.assertLessEqual(since + timedelta(days=30), expires)
    case.assertLessEqual(expires, datetime.now(UTC) + timedelta(days=30))


class Keychain:
    """items: {service: (account, value)}; calls: each (argv, stdin); fail_write: -i writes
    nothing; race: the value another process writes over ours; raises: what lookups without -w
    raise. Other subprocesses: system_probe."""

    def __init__(self, entries=None, *, fail_write=False, race=None, raises=None):
        self.items = {s: (s, v) for s, v in (entries or {}).items()}
        self.calls, self.fail_write, self.race, self.raises = [], fail_write, race, raises

    def value(self, service):
        found = self.items.get(service)
        return found and found[1]

    def __call__(self, argv, *args, **kw):
        if not argv or argv[0] != client.SECURITY:
            return system_probe(argv, *args, **kw)
        self.calls.append((list(argv), kw.get("input")))
        if argv[1:2] == ["-i"]:
            return self._interactive(argv, kw.get("input") or "")
        service = argv[argv.index("-s") + 1]
        if self.raises is not None and "-w" not in argv:
            raise self.raises
        found = self.items.get(service)
        if found is None:
            if kw.get("check"):
                raise CalledProcessError(44, argv)
            return CompletedProcess(argv, 44, stdout="", stderr="not found")
        if "-w" in argv:
            return CompletedProcess(argv, 0, stdout=found[1] + "\n", stderr="")
        attrs = f'attributes:\n    "acct"<blob>="{found[0]}"\n    "svce"<blob>="{service}"\n'
        return CompletedProcess(argv, 0, stdout=attrs, stderr="")

    def _interactive(self, argv, text):
        for line in text.splitlines():
            words = shlex.split(line)
            if words[:1] != ["add-generic-password"] or self.fail_write:
                continue
            opt = {words[i]: words[i + 1] for i in range(1, len(words) - 1) if words[i] != "-U"}
            value = self.race or bytes.fromhex(opt["-X"]).decode()
            self.items[opt["-s"]] = (opt["-a"], value)
        return CompletedProcess(argv, 0, stdout="security> " * 2, stderr="")


class Endpoint:
    """urlopen: the token endpoint answers answer (dict, bytes or exception), GraphQL data or
    data(query, variables); tokens, graphql: the requests made."""

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
        body = json.loads(req.data)
        data = self.data(body["query"], body["variables"]) if callable(self.data) else self.data
        return _response(json.dumps({"data": data}).encode())


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
