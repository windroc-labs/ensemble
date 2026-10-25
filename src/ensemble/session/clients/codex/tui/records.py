"""The one reader of a Codex session file's records."""

TEXT = {"user": "input_text", "assistant": "output_text"}  # role → its text blocks' type


def message(record: dict) -> tuple[str, str] | None:
    """A user or assistant `response_item` message's role and its text blocks joined; else None."""
    payload = record.get("payload")
    if record.get("type") != "response_item" or not isinstance(payload, dict):
        return None
    role = payload.get("role")
    if payload.get("type") != "message" or role not in TEXT:
        return None
    content = payload.get("content")
    text = "\n".join(
        block["text"]
        for block in (content if isinstance(content, list) else [])
        if isinstance(block, dict)
        and block.get("type") == TEXT[role]
        and isinstance(block.get("text"), str)
    )
    return role, text


def failure(record: dict) -> tuple[str, str] | None:
    """A failed turn's error kind (`codex_error_info`, or its one key) and message."""
    payload = record.get("payload")
    if record.get("type") != "event_msg" or not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if payload.get("type") != "task_complete" or not isinstance(error, dict):
        return None
    info, message = error.get("codex_error_info"), error.get("message")
    if isinstance(info, dict) and len(info) == 1:
        info = next(iter(info))
    return (
        info if isinstance(info, str) and info else "unknown",
        message if isinstance(message, str) else "",
    )
