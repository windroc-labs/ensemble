"""Linear GraphQL transport and Keychain access: an OAuth app's token sent as Bearer, renewed before
the call when due (missing, undated, expired or under RENEW_BEFORE left)."""

import http.client
import json
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ensemble.orchestrator.engine.types import (
    KEYCHAIN_NAME,
    GraphQLErrors,
    KeychainError,
    LinearApp,
    LinearError,
    Unavailable,
)
from ensemble.utils import one_line

URL = "https://api.linear.app/graphql"
TOKEN_URL = "https://api.linear.app/oauth/token"
SECURITY = "/usr/bin/security"  # absolute: no PATH entry stands in for the Keychain
RENEW_BEFORE = timedelta(days=5)
ACCOUNT = re.compile(r'^\s*"acct"<blob>="([^"]*)"$', re.M)


def _stderr_report(identity, reason):
    print(f"ensemble-orchestrator: token-error: {identity}: {reason}", file=sys.stderr)


@dataclass(frozen=True)
class Policy:
    """renew: a due token may be renewed (false: a dry run); report(identity, reason): a failed
    renewal of a still usable token; show(identity, status): each app's token as first read."""

    renew: bool = True
    report: Callable[[str, str], None] = _stderr_report
    show: Callable[[str, dict], None] | None = None


_policy = Policy()
_failed: dict[str, str] = {}  # token service: why its renewal failed in this process
_shown: set[str] = set()  # token services show was called for


def use(*, renew=True, report=None, show=None):
    """This process's renewal policy; forgets earlier failed renewals and shown tokens."""
    global _policy
    _policy = Policy(renew, report or _stderr_report, show)
    _failed.clear()
    _shown.clear()


def operation(query):
    m = re.match(r"[^{]*\{\s*([_A-Za-z]\w*)", query)
    return m[1] if m else "?"


def linear_gql(query, *, identity: LinearApp, timeout=30, **variables):
    # a clean secret only: urllib's header error would echo it
    secret = _token(identity, timeout)
    req = urllib.request.Request(
        URL,
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {secret}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:  # a URLError: before it
        errors = _http_errors(e) if e.code == 400 else []
        e.close()
        if e.code >= 500:
            reason = None
        elif e.code == 400 and _rate_limited(errors):
            reason = "RATELIMITED"
        else:
            detail = one_line("; ".join(messages(errors, secret=secret)))
            detail = detail[:1000] + "…" if len(detail) > 1000 else detail
            suffix = f": {detail}" if detail else ""
            raise LinearError(
                f"linear api error: {operation(query)}: HTTP {e.code}{suffix}"
            ) from None
        raise Unavailable(operation(query), status=e.code, reason=reason) from None
    except (OSError, http.client.HTTPException) as e:
        cause = e.reason if isinstance(e, urllib.error.URLError) else e
        raise Unavailable(operation(query), reason=one_line(cause)) from None
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise Unavailable(operation(query), reason="response not JSON") from None
    if not isinstance(body, dict):
        raise Unavailable(operation(query), reason="response not a GraphQL object")
    if body.get("errors"):
        raise GraphQLErrors(_scrub(body["errors"], secret))
    if not isinstance(body.get("data"), dict):
        raise Unavailable(operation(query), reason="response has no GraphQL data")
    return body["data"]


def messages(errors, *, secret=None):
    """Each GraphQL error's message (at most 500 characters) and its extensions.code, never the
    rest of the error.
    """
    found = []
    for error in errors if isinstance(errors, list) else [None]:
        error = error if isinstance(error, dict) else {}
        text = error.get("message")
        text = text if isinstance(text, str) else "unrecognized GraphQL error"
        text = text.replace(secret, "[redacted]") if secret else text
        text = text if len(text) <= 500 else text[:500] + "…"
        extensions = error.get("extensions")
        code = extensions.get("code") if isinstance(extensions, dict) else None
        code = code.replace(secret, "[redacted]") if secret and isinstance(code, str) else code
        found.append(f"{text} ({code})" if isinstance(code, str) else text)
    return found


def _http_errors(e):
    try:
        body = json.loads(e.read())
    except (OSError, http.client.HTTPException, json.JSONDecodeError, UnicodeDecodeError):
        return []
    if not isinstance(body, dict) or not isinstance(body.get("errors"), list):
        return []
    found = []
    for error in body["errors"]:
        if not isinstance(error, dict):
            continue
        extensions = error.get("extensions")
        if isinstance(error.get("message"), str) or (
            isinstance(extensions, dict) and isinstance(extensions.get("code"), str)
        ):
            found.append(error)
    return found


def _rate_limited(errors):
    for error in errors:
        extensions = error.get("extensions")
        if isinstance(extensions, dict) and extensions.get("code") == "RATELIMITED":
            return True
    return False


def _scrub(value, secret):
    """value with secret replaced in every string it holds."""
    if isinstance(value, str):
        return value.replace(secret, "[redacted]")
    if isinstance(value, list):
        return [_scrub(v, secret) for v in value]
    if isinstance(value, dict):
        return {_scrub(k, secret): _scrub(v, secret) for k, v in value.items()}
    return value


@dataclass(frozen=True)
class _Held:
    """A token entry as read: token None when missing, expires None when undated."""

    token: str | None
    expires: datetime | None

    def usable(self, now):
        return self.token is not None and (self.expires is None or self.expires > now)

    def due(self, now):
        return self.expires is None or self.expires - now < RENEW_BEFORE


class _RenewalFailed(Exception):
    pass


def _token(app, timeout):
    """app's token, renewed first when due and the policy allows; KeychainError when none usable."""
    held, now = _read(app.token_service, timeout), datetime.now(UTC)
    if _policy.show and app.token_service not in _shown:
        _shown.add(app.token_service)
        _policy.show(app.name, _status(held, now))
    if held.token is not None and not held.due(now):
        return held.token
    reason = _failed.get(app.token_service) or (None if _policy.renew else "dry run: no renewal")
    if reason is None:
        try:
            return _renew(app, held, now, timeout)
        except _RenewalFailed as e:
            reason = _failed[app.token_service] = str(e)
            if held.usable(now):
                _policy.report(app.name, reason)
    if held.usable(now):
        return held.token
    state = "token expired" if held.token is not None else "has no token"
    raise KeychainError(
        f"Linear {app.name} app {state} and renewal failed: {reason}; fix: check "
        f"{app.table}.client_id and the client secret in Keychain service {app.secret_service} "
        "(rotated: store the new secret), then rerun",
        identity=app.name,
    )


def _read(service, timeout):
    found = _security(["find-generic-password", "-s", service, "-w"], timeout)
    return _parse(found.stdout.strip()) if found.returncode == 0 else _Held(None, None)


def _parse(value):
    try:
        record = json.loads(value)
    except ValueError:
        record = None
    if not isinstance(record, dict):
        return _Held(value, None) if _clean(value) else _Held(None, None)
    token = record.get("access_token")
    if not isinstance(token, str) or not _clean(token):
        return _Held(None, None)
    try:
        expires = datetime.fromisoformat(record["expires_at"])
    except (KeyError, TypeError, ValueError):
        expires = None
    return _Held(token, expires if expires and expires.tzinfo else None)


def _renew(app, held, now, timeout):
    """A new token, stored; _RenewalFailed's reason holds no secret or token."""
    secret = None
    try:
        secret = _secret(app, timeout)
        token, expires_in = _request_token(app, secret, timeout)
        try:
            expires = (now + expires_in).isoformat()
        except OverflowError:
            raise _RenewalFailed("bad expires_in") from None
        record = json.dumps({"access_token": token, "expires_at": expires}, separators=(",", ":"))
        return _write(app.token_service, record, now, timeout)
    except _RenewalFailed as e:
        reason = one_line(str(e))
        for hidden in filter(None, (secret, held.token)):
            reason = reason.replace(hidden, "[redacted]")
        raise _RenewalFailed(reason[:300] + "…" if len(reason) > 300 else reason) from None


def _secret(app, timeout):
    service = app.secret_service
    try:
        found = _security(["find-generic-password", "-s", service, "-w"], timeout)
    except (OSError, subprocess.SubprocessError) as e:
        raise _RenewalFailed(f"client secret read: {type(e).__name__}") from None
    secret = found.stdout.strip() if found.returncode == 0 else ""
    if not _clean(secret):
        raise _RenewalFailed(
            f"no client secret in Keychain service {service}; fix: "
            f"security add-generic-password -s {service} -a {service} -w"
        )
    return secret


def _request_token(app, secret, timeout):
    """(token, its lifetime) from a client_credentials request with app's fixed scopes."""
    form = {
        "grant_type": "client_credentials",
        "client_id": app.client_id,
        "client_secret": secret,
        "scope": app.scopes,
    }
    req = urllib.request.Request(
        TOKEN_URL,
        data=urllib.parse.urlencode(form).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:  # a URLError: before it
        detail = _oauth_error(e)
        e.close()
        raise _RenewalFailed(f"HTTP {e.code}" + (f": {detail}" if detail else "")) from None
    except (OSError, http.client.HTTPException) as e:
        cause = e.reason if isinstance(e, urllib.error.URLError) else e
        raise _RenewalFailed(one_line(cause)) from None
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise _RenewalFailed("response not JSON") from None
    body = body if isinstance(body, dict) else {}
    token, expires_in = body.get("access_token"), body.get("expires_in")
    if token is None:
        raise _RenewalFailed("no access_token")
    if not isinstance(token, str) or not _clean(token):
        raise _RenewalFailed("bad access_token")
    if not isinstance(expires_in, int) or isinstance(expires_in, bool) or expires_in <= 0:
        raise _RenewalFailed("bad expires_in")
    try:
        return token, timedelta(seconds=expires_in)
    except OverflowError:
        raise _RenewalFailed("bad expires_in") from None


def _oauth_error(e):
    """An OAuth error body's error and error_description, else ""."""
    try:
        body = json.loads(e.read())
    except (OSError, http.client.HTTPException, json.JSONDecodeError, UnicodeDecodeError):
        return ""
    body = body if isinstance(body, dict) else {}
    return ": ".join(
        v for v in (body.get("error"), body.get("error_description")) if isinstance(v, str)
    )


def _write(service, value, now, timeout):
    """value as service's password, on security -i's stdin (never argv); the token the entry then
    holds: value's, or a concurrent renewal's. Re-read, as security -i can exit 0 on a failure."""
    try:
        attrs = _security(["find-generic-password", "-s", service], timeout)
        found = ACCOUNT.search(attrs.stdout or "") if attrs.returncode == 0 else None
        account = found[1] if found and KEYCHAIN_NAME.fullmatch(found[1]) else service
        command = f"add-generic-password -U -s {service} -a {account} -X {value.encode().hex()}\n"
        wrote = subprocess.run(
            [SECURITY, "-i"], input=command, capture_output=True, text=True, timeout=timeout
        ).returncode
        stored = _read(service, timeout)
    except (OSError, subprocess.SubprocessError):
        wrote, stored = 1, _Held(None, None)
    mine = stored.token == _parse(value).token
    if wrote or not stored.usable(now) or not (mine or not stored.due(now)):
        raise _RenewalFailed(f"Keychain write of {service} failed")
    return stored.token


def _security(args, timeout):
    return subprocess.run(
        [SECURITY, *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def _status(held, now):
    """held as a dry run shows it: {"days_left": n.n} or {"state": missing, no expiry or expired}."""
    if held.token is None:
        return {"state": "missing"}
    if held.expires is None:
        return {"state": "no expiry"}
    if held.expires <= now:
        return {"state": "expired"}
    return {"days_left": round((held.expires - now) / timedelta(days=1), 1)}


def has_token(identity: LinearApp):
    """Preflight an app through the same renewal policy as a call; discard its token."""
    _token(identity, 30)
    return True


def _clean(s):
    return bool(s) and s.isprintable() and not any(c.isspace() for c in s)
