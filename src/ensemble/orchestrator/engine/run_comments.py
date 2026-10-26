"""Session comments: an agent run's one tracker comment (`Run <sid> · …`), posted by the harness
app through the engine Tracker under the ref (sid, "session"), numbered `#<seq>`; repair rewrites
it when a late write left an older one. Resume commands come from the owned run view.
"""

import re
import threading

from ensemble.delegate import ConfigError, Resumable, SessionError
from ensemble.orchestrator.engine.types import ConfigError as RunConfigError
from ensemble.utils import one_line, timestamp

LIMIT = 10
SEQ = re.compile(r" · #(\d+) · (\S+)")


def base(*, sid, workdir, client, started_at, info, seq=0):
    """`seq`: the comment's last written number."""
    return {
        "sid": sid,
        "workdir": workdir,
        "client": client,
        "started_at": started_at,
        "seq": seq,
        "info": info,
    }


def command(rec):
    try:
        st = rec["info"]()
    except (ConfigError, RunConfigError, SessionError):
        return None
    return st.resume_command if st.resumable != Resumable.UNKNOWN and st.resume_command else None


def _status(rc, outcome, limit):
    if outcome is not None:
        return outcome.status
    if limit:
        return "limit"
    return "no outcome" if rc == 0 else "interrupted"


def body(rec, rc=None, ended_at=None, *, outcome=None, limit=False):
    """Ended once rc or ended_at is given; an ended run without rc (Recover's replay) shows no exit."""
    rec["seq"] += 1
    if rc is None and ended_at is None:
        line = f"running · #{rec['seq']} · {rec['started_at']}"
    else:
        status = _status(rc, outcome, limit)
        code = "" if rc is None else f" · exit {rc}"
        line = f"{status} · #{rec['seq']} · {rec['started_at']} → {ended_at}{code}"
    cmd = command(rec)
    return f"Run {rec['sid']} · {line}" + ("" if cmd is None else f"\n\n```\n{cmd}\n```")


def _bounded(work, limit):
    # the thread bounds every call, key reads included
    out = {}

    def run():
        try:
            out["value"] = work()
        except BaseException as e:  # noqa: BLE001  # Forward failures to the caller thread.
            out["error"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(limit)
    if t.is_alive():
        return None, f"timed out after {limit}s"
    if "error" in out:
        error = out["error"]
        if not isinstance(error, (Exception, SystemExit)):
            raise error
        return None, one_line(error)
    return out["value"], None


def _ref(sid):
    return (sid, "session")


def _write(tracker, sid, issue, text, limit):
    ok, error = _bounded(lambda: tracker.put_comment(issue, text, _ref(sid)), limit)
    return error or (None if ok else "success: false")


def _text(rec):
    rc, ended_at, outcome, limit = rec["args"]
    out = body(rec, rc, ended_at, outcome=outcome, limit=limit)
    rec["last"] = rec["seq"]
    return out


def post(issue, rec, *, tracker, timeout=LIMIT, rc=None, ended_at=None, outcome=None, limit=False):
    rec["args"] = (
        rc,
        None if rc is None and ended_at is None else ended_at or timestamp(),
        outcome,
        limit,
    )
    return _write(tracker, rec["sid"], issue, _text(rec), timeout)


def repair(issue, issue_id, rec, *, tracker, limit=LIMIT):
    found, error = _bounded(lambda: tracker.find_comment(issue_id, _ref(rec["sid"])), limit)
    if error:
        return error
    seq = found and SEQ.search(found.split("\n", 1)[0])
    if seq and int(seq[1]) >= rec["last"] and seq[2] == rec["started_at"]:
        return None
    return _write(tracker, rec["sid"], issue, _text(rec), limit)
