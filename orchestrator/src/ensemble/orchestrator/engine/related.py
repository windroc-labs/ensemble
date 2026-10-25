"""An issue's first-claim search: the board's issues (through the engine Tracker), docs and merged PRs
its title words or the ids its description names match, minus its upstream chain; inputs.render
lists them as Related work.
"""

import json
import re
from dataclasses import replace
from functools import cache

from ensemble.orchestrator.engine import inputs
from ensemble.orchestrator.engine.config import TASKS
from ensemble.orchestrator.engine.flow import handoff
from ensemble.orchestrator.engine.flow.types import (
    ISSUE_ID,
)
from ensemble.orchestrator.engine.types import (
    InputError,
    Ref,
    Related,
)
from ensemble.utils import SHORT, err_text

STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "do",
        "for",
        "from",
        "how",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "per",
        "should",
        "the",
        "then",
        "this",
        "that",
        "to",
        "up",
        "use",
        "via",
        "vs",
        "what",
        "when",
        "which",
        "why",
        "with",
        "without",
    }
)
MAX_WORDS, MAX_HOPS = 5, 5


def _prefix():
    return _prefix_re(tuple(sorted(t.title_prefix for t in TASKS.values() if t.title_prefix)))


@cache
def _prefix_re(prefixes):
    # keyed by the workflow's prefixes: a later load() is seen
    return re.compile(rf"^(?:{'|'.join(map(re.escape, prefixes))}): ")


def keywords(title) -> list[str]:
    # a task's title prefix (`ENG: `, `PRD: `) is on every issue of its kind
    text = re.sub(ISSUE_ID, " ", _prefix().sub("", title))
    words = []
    for w in re.findall(r"\w+", text.lower()):
        if len(w) > 1 and not w.isdigit() and w not in STOPWORDS and w not in words:
            words.append(w)
    return words[:MAX_WORDS]


def _mentioned(issue):
    key = issue.identifier.rsplit("-", 1)[0] + "-"
    found = re.findall(ISSUE_ID, issue.description)
    return list(dict.fromkeys(i for i in found if i.startswith(key) and i != issue.identifier))


def _chain(tracker, issue):
    chain, src = [], issue.handoff and issue.handoff.source
    while src and src != issue.identifier and src not in chain:
        chain.append(src)
        if len(chain) == MAX_HOPS:
            break
        described = tracker.description(src)
        h = described is not None and handoff.parse_handoff(described)
        src = h.source if h else None
    return chain


def _owner(text, names, excluded):
    """The candidate id text names, "" for none, None when it names an excluded issue."""
    tagged = re.findall(ISSUE_ID, text)
    if excluded.intersection(tagged) or inputs.CONTROL.search(text):
        return None
    return next((i for i in tagged if i in names), "")


def _doc_refs(run, docs, words, names, excluded):
    refs = []
    for d in dict.fromkeys(x.rstrip("/") for x in docs.dirs.values()):
        for name in inputs.listing(run, docs, d) or []:
            owner = _owner(name, names, excluded)
            tokens = re.findall(r"\w+", name.lower())
            if owner is not None and (owner or any(t.startswith(tuple(words)) for t in tokens)):
                path = f"{d}/{name}"
                refs.append(Ref(path, inputs.doc_url(docs, path), owner or None))
    return refs


def _pr_refs(run, target, words, names, excluded):
    res = run(
        [
            *(["env", f"GH_HOST={target.host}"] if target.host != "github.com" else []),
            *("gh", "search", "prs", "--merged", "--repo", f"{target.owner}/{target.name}"),
            *("--limit", "5", "--json", "number,title,url", "--"),
            *[t for w in words for t in ("OR", w)][1:],
        ],
        SHORT,
    )
    if res.returncode != 0:
        raise InputError(f"gh search prs: {err_text(res)}")
    try:
        prs = [(int(p["number"]), str(p["title"]), str(p["url"])) for p in json.loads(res.stdout)]
    except (ValueError, KeyError, TypeError):
        raise InputError("gh search prs: unusable response") from None
    return [
        Ref(f"#{n} {title}", url, owner or None)
        for n, title, url in prs
        if (owner := _owner(title, names, excluded)) is not None and not inputs.CONTROL.search(url)
    ]


def search(tracker, issue, cfg, *, target, run) -> Related:
    words, ids = keywords(issue.title), _mentioned(issue)
    if not words and not ids:
        return Related()
    nodes = tracker.search(words, ids)
    chain = _chain(tracker, issue)
    excluded, found = {issue.identifier, *chain}, {}
    for n in nodes:
        ident, h = n.identifier, handoff.parse_handoff(n.description)
        if h and h.source in chain:
            excluded.add(ident)
        elif ident not in excluded and ident not in found:
            urls = tuple(u for u in n.urls if not inputs.CONTROL.search(u))
            found[ident] = replace(n, urls=urls)
    refs = _doc_refs(run, cfg.docs, words, found, excluded)
    if target and words:
        refs += _pr_refs(run, target, words, found, excluded)
    return Related(tuple(found.values()), tuple(refs))
