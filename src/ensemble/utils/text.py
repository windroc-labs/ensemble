from datetime import datetime


def printable(text: str) -> str:
    """Without control characters but tabs."""
    return "".join(c for c in text if c.isprintable() or c == "\t")


def sid_error(sid) -> str | None:
    if isinstance(sid, str) and sid and sid.isprintable() and "/" not in sid:
        return None
    return f'sid must be a non-empty printable string without "/", got {sid!r:.80}'


def one_line(value):
    # Other exception attributes may contain credentials.
    text = f"{type(value).__name__}: {value}" if isinstance(value, BaseException) else str(value)
    return " ".join(text.split())


def last_lines(text: str, count: int = 5) -> str:
    return " | ".join([line.strip() for line in text.splitlines() if line.strip()][-count:])


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def timestamp(*, now=datetime.now):
    return now().astimezone().isoformat(timespec="seconds")
