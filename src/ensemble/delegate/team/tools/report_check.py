#!/usr/bin/env python3
"""Check research report display width without changing the report."""

import html
import re
import sys
import unicodedata

LINE_LIMIT = 80
CELL_LIMIT = 30
FINDINGS_LIMIT = 6000
_TAG = r"</?[A-Za-z][^>]*>"
_LIST = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+(?:\[[ xX]\]\s+)?")
_TYPE = re.compile(r"^[^:：]+[:：].*·\s*(?:web|local|mixed)(?:\s*·.*)?$")
_FINDINGS = re.compile(r"<details\b[^>]*\bid=[\"']?findings\b")
_LINK = re.compile(r"!?\[([^\]]*)\]\((?:[^()\n]|\([^()\n]*\))*\)")


def _plain(text):
    text = _LINK.sub(r"\1", text)
    text = re.sub(r"\[\d+\]", "", text)
    text = re.sub(r"\[([^\]]+)\]\[[^\]]*\]", r"\1", text)
    text = re.sub(r"<?https?://[^\s<>]+>?", "", text)
    text = re.sub(_TAG, "", text)
    parts = re.split(r"(`+[^`]*`+|\\.)", text)
    for i, part in enumerate(parts):
        if part.startswith("`"):
            parts[i] = part.strip("`")
        elif part.startswith("\\"):
            parts[i] = part[1:]
        else:
            parts[i] = re.sub(r"(?<!\w)[*_~]+|[*_~]+(?!\w)", "", part)
    return " ".join(html.unescape("".join(parts)).split())


def _cells(line):
    cells, cell = [], []
    code = 0
    i = 0
    while i < len(line):
        char = line[i]
        if char == "\\" and i + 1 < len(line):
            cell.extend(line[i : i + 2])
            i += 2
            continue
        if char == "`":
            end = i + 1
            while end < len(line) and line[end] == "`":
                end += 1
            size = end - i
            code = 0 if code == size else code if code else size
            cell.append(line[i:end])
            i = end
            continue
        if char == "|" and not code:
            cells.append("".join(cell).strip())
            cell = []
        else:
            cell.append(char)
        i += 1
    cells.append("".join(cell).strip())
    if line.startswith("|"):
        cells.pop(0)
    if line.endswith("|") and not cell:
        cells.pop()
    return cells


def _unquote(line):
    prefix = re.match(r"^\s*(?:>\s?)+", line)
    return (line[prefix.end() :] if prefix else line).strip(), (
        prefix[0].count(">") if prefix else 0
    )


def _separator(line):
    cells = _cells(line)
    return bool(cells) and all(re.fullmatch(r":?-+:?", cell) for cell in cells)


def check(text):
    failures = []
    pending = []
    start, kind = 0, "paragraph"
    budget = None
    depth = 0

    def measure(line, category, value, limit):
        value = _plain(value)
        width = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in value)
        if budget:
            budget[1] += width
            budget[2].append(value)
        elif width > limit:
            failures.append(f"line {line}: {category} {width}>{limit}: {value[:60]}")

    def close(line, width, texts):
        if width > FINDINGS_LIMIT:
            text = " ".join(texts)[:60]
            failures.append(f"line {line}: findings {width}>{FINDINGS_LIMIT}: {text}")

    def flush():
        if pending:
            measure(start, kind, " ".join(pending), LINE_LIMIT)
            pending.clear()

    lines = text.splitlines()
    fence = ""
    table = False
    columns = 0
    table_quote = 0
    separator_line = 0
    after_title = False
    for number, raw in enumerate(lines, 1):
        if number == separator_line:
            continue
        line, quote = _unquote(raw)
        if quote != table_quote:
            table = False
        marker = re.match(r"^(`{3,}|~{3,})(.*)$", line)
        if fence:
            if (
                marker
                and marker[1][0] == fence[0]
                and len(marker[1]) >= len(fence)
                and not marker[2].strip()
            ):
                fence = ""
            continue
        if marker:
            flush()
            fence = marker[1]
            table = after_title = False
            continue
        if not line:
            flush()
            table = False
            continue
        if re.match(r"^#{1,6}\s", line):
            flush()
            if budget and re.match(r"^#{1,2}\s", line):
                close(*budget)
                budget = None
            after_title = bool(re.match(r"^#\s", line))
            table = False
            continue
        if after_title and _TYPE.fullmatch(line):
            flush()
            after_title = False
            continue
        after_title = False
        tags = re.fullmatch(rf"(?:{_TAG}\s*)+", line)
        if (
            tags
            or re.fullmatch(r"\[![A-Z]+\]", line)
            or re.match(r"^\[[^\]]+\]:\s*\S", line)
            or re.fullmatch(r"(?:[-*_]\s*){3,}", line)
        ):
            flush()
            table = False
            if tags and not budget and _FINDINGS.search(line):
                budget, depth = [number, 0, []], 0
            if tags and budget:
                depth += line.count("<details") - line.count("</details")
                if depth <= 0:
                    close(*budget)
                    budget = None
            continue
        item = _LIST.match(line)
        if item:
            table = False
        next_line, next_quote = _unquote(lines[number]) if number < len(lines) else ("", 0)
        cells = _cells(line)
        begins_table = (
            not item
            and "|" in line
            and quote == next_quote
            and _separator(next_line)
            and len(cells) == len(_cells(next_line))
        )
        if table or begins_table:
            flush()
            if not table:
                columns = len(cells)
                table_quote = quote
                separator_line = number + 1
            table = True
            for column, cell in enumerate(cells[:columns]):
                limit = LINE_LIMIT if column == columns - 1 else CELL_LIMIT
                measure(number, "cell", cell, limit)
            continue
        table = False
        if item:
            flush()
            start, kind = number, "list"
            pending.append(line[item.end() :])
        else:
            if not pending:
                start, kind = number, "paragraph"
            pending.append(line)
    flush()
    if budget:
        close(*budget)
    return failures


def main(argv: list[str], *, out=sys.stdout) -> int:
    if len(argv) != 1:
        print("check failed: usage: report_check.py FILE", file=out)
        return 2
    try:
        with open(argv[0], encoding="utf-8") as file:
            text = file.read()
    except (OSError, UnicodeError) as error:
        print(f"check failed: {error}", file=out)
        return 2
    failures = check(text)
    print("\n".join(failures) or "ok", file=out)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
