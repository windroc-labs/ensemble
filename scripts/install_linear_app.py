"""Prepare one installer's OAuth app; only public metadata leaves this process."""

import json
import os
import re
import subprocess
import sys
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

NAME = re.compile(r"[A-Za-z0-9._@+][A-Za-z0-9._@+-]*")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
TOKEN_URL = "https://api.linear.app/oauth/token"
GRAPHQL_URL = "https://api.linear.app/graphql"
BROKER = "/usr/local/libexec/ensemble/ensemble-broker"
# engine/types.py SCOPES by slot; any other slot is a role
SCOPES = {"harness": "read,write", "manager": "read,write,app:assignable"}
ROLE_SCOPES = "read,write,app:assignable,app:mentionable"


class InstallError(Exception):
    """A credential-free diagnostic."""


def clean(value):
    return (
        isinstance(value, str)
        and bool(value)
        and value.isprintable()
        and not any(c.isspace() or c in "\"'\\" for c in value)
    )


def process(args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, timeout=40, check=False, **kwargs)


def read_secret(service):
    found = process(
        ["security", "find-generic-password", "-s", service, "-w"], stdin=subprocess.DEVNULL
    )
    return found.stdout.strip() if found.returncode == 0 else ""


def held_token(service, now):
    try:
        held = json.loads(read_secret(service))
        token, expires = held["access_token"], datetime.fromisoformat(held["expires_at"])
        if clean(token) and expires.tzinfo and expires > now + timedelta(days=5):
            return token
    except (ValueError, TypeError, KeyError):
        pass
    return None


def curl(url, payload, headers, *, config=None):
    args = ["curl", "-q", "-fsS", "--connect-timeout", "10", "--max-time", "30"]
    if config is not None:
        args += ["-K", "-", "--data-binary", payload]
        incoming = config
    else:
        args += ["--data-binary", "@-"]
        incoming = payload
    sent = process([*args, "-H", headers, url], input=incoming)
    if sent.returncode:
        raise InstallError(
            "Linear request failed; check the app credentials and team access, then rerun"
        )
    try:
        answer = json.loads(sent.stdout)
    except ValueError:
        raise InstallError("Linear response is not JSON") from None
    if not isinstance(answer, dict):
        raise InstallError("Linear response is not an object")
    return answer


def request_token(client_id, secret, slot, now):
    scopes = SCOPES.get(slot, ROLE_SCOPES)
    try:
        answer = curl(
            TOKEN_URL,
            urlencode(
                {
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": secret,
                    "scope": scopes,
                }
            ),
            "Content-Type: application/x-www-form-urlencoded",
        )
    except InstallError:
        raise InstallError("token request failed") from None
    token, seconds = answer.get("access_token"), answer.get("expires_in")
    if not clean(token):
        raise InstallError("invalid OAuth access_token")
    if not isinstance(seconds, int) or isinstance(seconds, bool) or seconds <= 0:
        raise InstallError("invalid OAuth expires_in")
    try:
        expires = (now + timedelta(seconds=seconds)).isoformat()
    except OverflowError:
        raise InstallError("invalid OAuth expires_in") from None
    return token, json.dumps({"access_token": token, "expires_at": expires}, separators=(",", ":"))


def write_token(service, value):
    attrs = process(["security", "find-generic-password", "-s", service], stdin=subprocess.DEVNULL)
    found = re.search(r'"acct"<blob>="([^"]+)"', attrs.stdout) if attrs.returncode == 0 else None
    account = found[1] if found and NAME.fullmatch(found[1]) else service
    command = f"add-generic-password -U -s {service} -a {account} -X {value.encode().hex()}\n"
    result = process(["security", "-i"], input=command)
    if result.returncode or read_secret(service) != value:
        raise InstallError(f"Keychain write of {service} failed; fix that item, then rerun")


def viewer(token):
    answer = curl(
        GRAPHQL_URL,
        json.dumps({"query": "{ viewer { id } }", "variables": {}}),
        "Content-Type: application/json",
        config=f'header = "Authorization: Bearer {token}"\n',
    )
    try:
        ident = answer["data"]["viewer"]["id"]
    except (KeyError, TypeError):
        ident = None
    if answer.get("errors") or not isinstance(ident, str) or not UUID.fullmatch(ident):
        raise InstallError("invalid app viewer id; check app access, then rerun")
    return ident


def ask(slot):
    os.write(3, f"Linear OAuth Client ID for {slot}: ".encode())
    answer = b""
    while not answer.endswith(b"\n"):
        chunk = os.read(3, 1)
        if not chunk:
            raise InstallError("no Client ID answer on the terminal")
        answer += chunk
    return answer.decode(errors="replace").strip()


def settings_for(config, slot):
    data = tomllib.loads(Path(config).read_text()) if Path(config).exists() else {}
    linear = data.get("orchestrator", {}).get("linear", {})
    return linear.get(slot, {}) if slot in SCOPES else linear.get("roles", {}).get(slot, {})


def writer(config, slot):
    selected = settings_for(config, slot).get("renewal", "transport")
    if selected not in ("transport", "broker"):
        raise InstallError("renewal must be transport or broker")
    return selected


def broker_token(settings, slot, client_id, service, secret_service, *, now):
    identity = {
        "name": slot,
        "kind": slot if slot in SCOPES else "role",
        "client_id": client_id,
        "user_id": settings.get("user_id", ""),
        "secret_service": secret_service,
        "token_service": service,
    }
    failed = False
    try:
        result = process([BROKER, "linear-maintain"], input=json.dumps(identity))
        reply = (
            json.loads(result.stdout)
            if not result.returncode and len(result.stdout) <= 4096
            else {}
        )
        if (
            not isinstance(reply, dict)
            or not {"version", "renewed", "expires_at"}
            <= reply.keys()
            <= {"version", "renewed", "expires_at", "audit_warning"}
            or type(reply.get("version")) is not int
            or reply["version"] != 1
            or type(reply.get("renewed")) is not bool
        ):
            failed = True
        elif "audit_warning" in reply:
            print(
                "install.sh: Linear broker audit persistence failed; the credential result still applies",
                file=sys.stderr,
            )
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        failed = True
    try:
        held = json.loads(read_secret(service))
        token, expires = held["access_token"], datetime.fromisoformat(held["expires_at"])
        if clean(token) and expires.tzinfo and expires > max(now, datetime.now(UTC)):
            if failed:
                print(
                    "install.sh: Linear broker maintenance failed; using the current usable token",
                    file=sys.stderr,
                )
            return token
    except (ValueError, TypeError, KeyError):
        pass
    raise InstallError(
        "no usable broker-published token; check enrollment and broker installation, then rerun"
    )


def prepare(config, slot, service, secret_service, output):
    if not all(isinstance(v, str) and NAME.fullmatch(v) for v in (slot, service, secret_service)):
        raise InstallError("invalid app or Keychain service name")
    if service == secret_service:
        raise InstallError("token and client secret must use different Keychain services")
    settings = settings_for(config, slot)
    selected = writer(config, slot)
    client_id = settings.get("client_id")
    if client_id in (None, ""):
        if selected == "broker":
            raise InstallError("broker enrollment requires a configured Client ID and user_id")
        client_id = ask(slot)
    if (
        not isinstance(client_id, str)
        or not client_id
        or not client_id.isprintable()
        or any(c.isspace() for c in client_id)
    ):
        raise InstallError("invalid OAuth Client ID")
    configured_id = settings.get("user_id")
    if configured_id and (not isinstance(configured_id, str) or not UUID.fullmatch(configured_id)):
        raise InstallError("invalid configured app user_id")
    now = datetime.now(UTC)
    if selected == "broker":
        if not configured_id:
            raise InstallError("broker enrollment requires a configured user_id")
        token = broker_token(settings, slot, client_id, service, secret_service, now=now)
        renewed = False
    else:
        secret = read_secret(secret_service)
        if not clean(secret):
            raise InstallError(
                f"no usable client secret in {secret_service}; fix that item, then rerun"
            )
        token = held_token(service, now)
        renewed = token is None
        if renewed:
            token, record = request_token(client_id, secret, slot, now)
            write_token(service, record)
    ident = viewer(token) if renewed or not configured_id else configured_id
    if configured_id and ident != configured_id:
        raise InstallError("configured user_id differs from app viewer id")
    planned = json.loads(Path(output).read_text())
    for other, old in planned.items():
        if other != slot and (old["user_id"] == ident or old.get("client_id") == client_id):
            raise InstallError(f"the apps for {other} and {slot} are the same Linear app")
    planned[slot] = {
        "client_id": client_id,
        "secret_service": secret_service,
        "token_service": service,
        "user_id": ident,
    }
    if selected == "broker":
        planned[slot]["renewal"] = selected
    Path(output).write_text(json.dumps(planned))


def main(args):
    if len(args) == 3 and args[0] == "--writer":
        try:
            print(writer(args[1], args[2]))
        except (InstallError, OSError, ValueError, TypeError, AttributeError):
            print("install.sh: cannot determine Linear renewal writer", file=sys.stderr)
            return 1
        return 0
    if len(args) != 5:
        print(
            "usage: install_linear_app.py <config> <slot> <token-service> <secret-service> <plan>",
            file=sys.stderr,
        )
        return 2
    try:
        prepare(*args)
    except (InstallError, OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError):
        # Keep tool/JSON diagnostics from reflecting credentials.
        error = sys.exception()
        reason = (
            str(error)
            if isinstance(error, InstallError)
            else "cannot prepare app credentials; check configuration and Keychain, then rerun"
        )
        print(f"install.sh: Linear {args[1]}: {reason}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
