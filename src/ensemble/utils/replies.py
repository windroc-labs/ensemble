"""The reply store's one home: `<events>.replies/<id>` holds a `reply <id>` event's text."""

import os
import re
import stat

from ensemble.utils.files import drop_stale, read_beneath

REPLY_ID = re.compile(r"[0-9a-f]{32}")
REPLY_MAX = 1 << 20


def _checked(reply_id: str) -> str:
    if not isinstance(reply_id, str) or not REPLY_ID.fullmatch(reply_id):
        raise ValueError(f"reply id {reply_id!r:.80}: want 32 lowercase hex characters")
    return reply_id


def _store(events: str) -> str:
    return os.fspath(events) + ".replies"


def _cut(text: str) -> bytes:
    data = text.encode(errors="replace")
    return data[:REPLY_MAX].decode(errors="ignore").encode() if len(data) > REPLY_MAX else data


def save_reply(events: str, reply_id: str, text: str) -> bool:
    """Stores `text`'s first REPLY_MAX bytes once per id (else False), in this user's directory."""
    directory = _store(events)
    _checked(reply_id)
    try:
        os.mkdir(directory, 0o700)
    except FileExistsError:
        found = os.lstat(directory)
        if not stat.S_ISDIR(found.st_mode) or found.st_uid != os.getuid():
            raise OSError(f"{directory}: not a directory of this user") from None
    temporary = os.path.join(directory, f".{reply_id}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(_cut(text))
        try:
            os.link(temporary, os.path.join(directory, reply_id), follow_symlinks=False)
        except FileExistsError:
            return False
    finally:
        os.unlink(temporary)
    drop_stale(directory)
    return True


def read_reply(events: str, reply_id: str) -> str | None:
    """`reply_id`'s text; None when it has none or it is no regular file."""
    data = read_beneath(
        _store(events), os.path.join(_store(events), _checked(reply_id)), limit=REPLY_MAX
    )
    return None if data is None else data.decode(errors="replace")
