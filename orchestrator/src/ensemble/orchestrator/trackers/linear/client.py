"""Linear GraphQL transport and Keychain access."""

import http.client
import json
import re
import subprocess
import urllib.error
import urllib.request

from ensemble.orchestrator.engine.types import (
    GraphQLErrors,
    KeychainError,
    LinearError,
    Unavailable,
)
from ensemble.utils import one_line

URL = "https://api.linear.app/graphql"
SECURITY = "/usr/bin/security"  # absolute: no PATH entry stands in for the Keychain


def operation(query):
    m = re.match(r"[^{]*\{\s*([_A-Za-z]\w*)", query)
    return m[1] if m else "?"


def linear_gql(query, *, identity, timeout=30, **variables):
    # naming only service: urllib's header error would echo it
    auth = _key(identity, timeout)
    req = urllib.request.Request(
        URL,
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Content-Type": "application/json", "Authorization": auth},
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
            detail = one_line("; ".join(messages(errors, secret=auth)))
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
        raise GraphQLErrors(body["errors"])
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


def _key(identity, timeout):
    service = identity.service
    found = subprocess.run(
        [SECURITY, "find-generic-password", "-s", service, "-w"],
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout,
    ).stdout.strip()
    if not _clean(found):
        raise KeychainError(f"Linear key of {service}: empty, not printable or holds whitespace")
    return found


def has_key(identity):
    # no -w: its secret is never read
    return (
        subprocess.run(
            [SECURITY, "find-generic-password", "-s", identity.service],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )


def _clean(s):
    return bool(s) and s.isprintable() and not any(c.isspace() for c in s)
