"""The issue command's output rules: stdout holds one JSON object of at most LIMIT UTF-8 bytes, a
write's receipt at most RECEIPT; diagnostics go to stderr, as bounded; exit OK, FAILED (the tracker
failed, a write's result is unknown, a refusal, or create's relations partly failed, its report on
stdout) or USAGE (usage, configuration or an unresolved reference).
"""

import contextlib
import copy
import functools
import json
import os
import tempfile

from ensemble.orchestrator.engine.flow.types import (
    COLUMN,
    COMMENT_CHARS,
    COMMENTS,
    DESCRIPTION_CHARS,
)
from ensemble.orchestrator.engine.types import Receipt, RelationsFailed

OK, FAILED, USAGE = 0, 1, 2
LIMIT, RECEIPT = 8192, 1024
PAGE, MAX_PAGE = 20, 50
PROG = "ensemble-orchestrator issue"
FULL = "--full --output FILE"
SHOWN = 20
ERROR_CHARS = 200
RELATION = "ensemble-orchestrator issue relation add"
TEXTS = (
    "title",
    "description",
    "parent.title",
    "project.name",
    "milestone.name",
    "assignee.name",
    "assignee.email",
    "state.name",
)
# never shortened by fit_page
REFS = frozenset(
    (
        "id",
        "identifier",
        "url",
        "key",
        "type",
        "email",
        "team_id",
        "group_id",
        "project_id",
        "parent_id",
    )
)


def encode(payload):
    # backslashreplace: a lone surrogate becomes its JSON escape
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return text.encode("utf-8", "backslashreplace") + b"\n"


def fit(payload, tiers, texts, follow):
    """payload encoded within LIMIT: drop the last item of its largest list in the first of tiers
    (tuples of list fields) with one not empty, then halve its longest text at a dotted path in
    texts, naming each cut in truncated, until it fits; continue is follow once anything is
    truncated, absent when follow is None.
    """
    payload = copy.deepcopy(payload)
    payload.setdefault("truncated", [])
    while True:
        if follow is not None:
            payload["continue"] = follow if payload["truncated"] else None
        data = encode(payload)
        if len(data) <= LIMIT:
            return data
        if full := next((f for f in ([f for f in t if payload[f]] for t in tiers) if f), None):
            field = max(full, key=lambda f: len(encode(payload[f])))
            payload[field].pop()
            kept = len(payload[field])
            payload["truncated"] = [m for m in payload["truncated"] if not _gone(m, field, kept)]
            _mark(payload, field)
            continue
        found = {path: text for path in texts if (text := _text(payload, path))}
        if not found:
            raise OverflowError(f"result exceeds {LIMIT} bytes: read it with {FULL}")
        path = max(found, key=lambda p: len(encode(found[p])))
        *parents, leaf = path.split(".")
        node = functools.reduce(lambda d, k: d[k], parents, payload)
        node[leaf] = found[path][: len(found[path]) // 2]
        _mark(payload, path)


def _gone(marker, field, kept):
    # a marker inside an item fit dropped, such as comments.4.body once 4 comments remain
    parts = marker.split(".")
    return len(parts) > 2 and parts[0] == field and parts[1].isdigit() and int(parts[1]) >= kept


def _mark(payload, field):
    if field not in payload["truncated"]:
        payload["truncated"].append(field)


def _text(payload, path):
    node = payload
    for key in path.split("."):
        node = node.get(key) if isinstance(node, dict) else None
    return node if isinstance(node, str) else None


def fit_page(rows, cursor):
    """A page's rows [(item, cut fields, cursor), …] and the cursor after it (None: last page) as
    {items, has_next_page, next_cursor, truncated} within LIMIT: trailing items drop, next_cursor
    then following the last kept one; a lone item's longest text other than a reference halves;
    each cut is items.<n>.<field>.
    """
    items = [copy.deepcopy(item) for item, _, _ in rows]
    cut = [f"items.{n}.{field}" for n, (_, fields, _) in enumerate(rows) for field in fields]
    while True:
        page = {"items": items, "has_next_page": cursor is not None, "next_cursor": cursor}
        data = encode({**page, "truncated": cut})
        if len(data) <= LIMIT:
            return data
        if len(items) > 1:
            items.pop()
            cursor = rows[len(items) - 1][2]
            cut = [m for m in cut if int(m.split(".")[1]) < len(items)]
            continue
        found = dict(_texts(items[0], ()))
        if not found:
            raise OverflowError(f"page exceeds {LIMIT} bytes: write it with {FULL}")
        path = max(found, key=lambda p: len(encode(found[p])))
        node = functools.reduce(lambda node, key: node[key], path[:-1], items[0])
        node[path[-1]] = found[path][: len(found[path]) // 2]
        if (marker := ".".join(map(str, ("items", 0, *path)))) not in cut:
            cut.append(marker)


def _texts(node, path):
    # (path, text) of each non-empty string in node but a reference
    pairs = node.items() if isinstance(node, dict) else enumerate(node)
    for key, value in pairs:
        if isinstance(value, (dict, list)):
            yield from _texts(value, (*path, key))
        elif isinstance(value, str) and value and key not in REFS:
            yield (*path, key), value


def write_full(path, payload):
    """payload as normalized JSON in path, through a temp file renamed over it; its absolute path."""
    path = os.path.abspath(path)
    fd, temp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".issue-", suffix=".tmp")
    try:
        with open(fd, "w", encoding="utf-8", errors="backslashreplace") as f:
            f.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)
        raise
    return path


def diagnose(err, message):
    data = f"{PROG}: {message}".encode("utf-8", "backslashreplace")
    if len(data) > LIMIT - 1:
        kept = data[: LIMIT // 2].decode("utf-8", "ignore").encode()
        data = kept + f" … ({len(data) - len(kept)} bytes omitted)".encode()
    print(data.decode(), file=err)


def receipt_payload(r: Receipt):
    payload = {
        "issue": {"id": r.id, "identifier": r.identifier, "url": r.url},
        "operation": r.operation,
        "result": r.result,
        "actor": {"id": r.actor_id, "name": r.actor_name},
    }
    if r.operation == "comment":
        payload["comment_id"] = r.comment_id
    return payload


def partial_report(e: RelationsFailed):
    """create's receipt, partial: the relations done, failed (error cut) and recover commands."""
    failed = [{b.direction: b.other, "error": error[:ERROR_CHARS]} for b, error in e.failed]
    return {
        **receipt_payload(e.receipt),
        "result": "partial",
        "done": [{b.direction: b.other} for b in e.done],
        "failed": failed,
        "recover": [_recover(e.receipt.identifier, b) for b, _ in e.failed],
    }


def _recover(ident, b):
    return f"{RELATION} {ident} --{b.direction.replace('_', '-')} {b.other}"


def partial_message(e: RelationsFailed):
    recover = "; ".join(_recover(e.receipt.identifier, b) for b, _ in e.failed)
    return (
        f"create {e.receipt.identifier}: created {e.receipt.url}, but {len(e.failed)} of "
        f"{len(e.failed) + len(e.done)} blocking relations failed; add only them: {recover}; "
        "do not run create again"
    )


def receipt(payload):
    """payload encoded within RECEIPT, its actor's name halved until it fits."""
    actor = payload["actor"]
    while len(data := encode(payload)) > RECEIPT and actor["name"]:
        actor["name"] = actor["name"][: len(actor["name"]) // 2]
    return data


def compact(full):
    """full cut to the default limits; truncated names each cut field, or one with more pages."""
    out, pages, cuts = dict(full), set(full["truncated"]), []
    if full["description"] is not None and len(full["description"]) > DESCRIPTION_CHARS:
        out["description"] = full["description"][:DESCRIPTION_CHARS]
        cuts.append("description")
    if "comments" in pages or len(full["comments"]) > COMMENTS:
        cuts.append("comments")
    out["comments"] = [dict(c) for c in full["comments"][:COMMENTS]]
    for i, c in enumerate(out["comments"]):
        if len(c["body"]) > COMMENT_CHARS:
            c["body"] = c["body"][:COMMENT_CHARS]
            cuts.append(f"comments.{i}.body")
    if "labels" in pages:
        cuts.append("labels")
    for column in ("blocks", "blocked_by", "children"):
        out[column] = full[column][:COLUMN]
        if column in pages or len(full[column]) > COLUMN:
            cuts.append(column)
    out["truncated"] = cuts
    return out


def dry_run(plan):
    """What archive would do, with the descendants it would archive and the unfinished issues."""
    return {
        "issue": {"id": plan.key, "identifier": plan.ident, "url": plan.url},
        "operation": "archive",
        "dry_run": True,
        "result": plan.outcome,
        "descendant_count": len(plan.descendants),
        "descendants": [_item(s) for s in plan.descendants],
        "unfinished_count": len(plan.unfinished),
        "unfinished": [_item(s) for s in plan.unfinished],
    }


def _item(sub):
    return {"id": sub.key, "identifier": sub.ident, "state": sub.state}


def refusal(ident, plan):
    """archive's refusal of ident: the unfinished issues of plan, the first SHOWN by name."""
    unfinished = plan.unfinished
    shown = ", ".join(f"{s.ident} ({s.state})" for s in unfinished[:SHOWN])
    more = f" and {len(unfinished) - SHOWN} more" if len(unfinished) > SHOWN else ""
    return (
        f"archive {ident} refused, nothing archived: {len(unfinished)} unfinished of "
        f"{len(plan.descendants) + 1} (it and its unarchived sub-issues): {shown}{more}; complete "
        f"or cancel each first (ensemble-orchestrator issue archive {ident} --dry-run lists them)"
    )
