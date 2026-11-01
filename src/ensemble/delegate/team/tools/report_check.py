#!/usr/bin/env python3
"""Check research report display width without changing the report."""

import html
import re
import sys
import unicodedata

LINE_LIMIT = 80
CELL_LIMIT = 30
FINDINGS_LIMIT = 15000
ASPECT_LIMIT = 3500
ANSWER_LIMIT = 160
ORIENTATION_LIMIT = 160
CLAIM_LIMIT = 80
BODY_LIMIT = 480
BODY_SENTENCES = 5
CODE_LINES = 15
_TAG = r"</?[A-Za-z][^>]*>"
_LIST = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+(?:\[[ xX]\]\s+)?")
_TYPE = re.compile(r"^[^:：]+[:：].*·\s*(?:web|local|mixed)(?:\s*·.*)?$")
_FINDINGS = re.compile(r"<details\b[^>]*\bid=[\"']?findings\b")
_LINK = re.compile(r"!?\[((?:[^\[\]]|\[[^\]]*\])*)\]\((?:[^()\n]|\([^()\n]*\))*\)")
_CLOSERS = "\"'\u201d\u2019)]"
_ABBREVIATION = re.compile(r"(?:[A-Za-z]\.){2,}|(?:vs|cf|al|etc)\.", re.IGNORECASE)


class Budget:
    """One scope's running display width: the findings block, an aspect or one of its parts."""

    def __init__(self, line, kind, limit):
        self.line, self.kind, self.limit = line, kind, limit
        self.width, self.sentences, self.texts = 0, 0, []

    def add(self, width, text):
        self.width += width
        self.texts.append(text)

    def failures(self):
        text = " ".join(self.texts)[:60]
        if self.width > self.limit:
            yield f"line {self.line}: {self.kind} {self.width}>{self.limit}: {text}"
        if self.sentences > BODY_SENTENCES:
            yield f"line {self.line}: sentences {self.sentences}>{BODY_SENTENCES}: {text}"


def _over(line, kind, width, limit, text):
    return [f"line {line}: {kind} {width}>{limit}: {text[:60]}"] if width > limit else []


class Findings:
    """The findings block's open scopes: the block, its current aspect and that aspect's part.

    Every measured item adds to each open scope. An aspect's first quoted item is its answer;
    other text before its first claim heading is orientation; text after a claim is that
    finding's body.
    """

    def __init__(self, line):
        self.block = Budget(line, "findings", FINDINGS_LIMIT)
        self.aspect = self.part = None
        self.answered = False
        self.failures = []

    def heading(self, line, level, title):
        self.close_part()
        if level == 3:
            self.close_aspect()
            self.aspect = Budget(line, "aspect", ASPECT_LIMIT)
            self.answered = False
        else:
            self.measure(line, "claim", title, CLAIM_LIMIT)
            self.part = Budget(line, "finding", BODY_LIMIT)

    def measure(self, line, category, value, limit, quote=0):
        width = _width(value)
        for scope in (self.block, self.aspect):
            if scope:
                scope.add(width, value)
        if category in ("cell", "claim"):
            self.failures += _over(line, category, width, limit, value)
        elif self.part:
            self.part.add(width, value)
            if self.part.kind == "finding":
                self.part.sentences += _sentences(value)
        elif self.aspect:
            if quote and not self.answered:
                self.failures += _over(line, "answer", width, ANSWER_LIMIT, value)
            else:
                self.part = Budget(line, "orientation", ORIENTATION_LIMIT)
                self.part.add(width, value)
            self.answered = True

    def close_part(self):
        if self.part:
            self.failures += self.part.failures()
        self.part = None

    def close_aspect(self):
        self.close_part()
        if self.aspect:
            self.failures += self.aspect.failures()
        self.aspect = None

    def close(self):
        self.close_aspect()
        self.failures += self.block.failures()
        return self.failures


def _width(text):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _sentences(text):
    count = 0
    for token in text.split():
        count += len(re.findall(r"[\u3002\uff01\uff1f]+", token))
        core = token.rstrip(_CLOSERS)
        if core[-1:] in (".", "!", "?") and not _ABBREVIATION.fullmatch(
            core.lstrip(_CLOSERS + "(\"'")
        ):
            count += 1
    return count


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
    start, kind, start_quote = 0, "paragraph", 0
    findings = None
    depth = 0
    code = None

    def measure(line, category, value, limit, quote=0):
        value = _plain(value)
        if findings:
            findings.measure(line, category, value, limit, quote)
        else:
            failures.extend(_over(line, category, _width(value), limit, value))

    def flush():
        if pending:
            measure(start, kind, " ".join(pending), LINE_LIMIT, start_quote)
            pending.clear()

    def close_code():
        nonlocal code
        if code:
            line, body = code
            failures.extend(_over(line, "code", len(body), CODE_LINES, (body or [""])[0]))
        code = None

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
                close_code()
            elif code:
                code[1].append(line)
            continue
        if marker:
            flush()
            fence = marker[1]
            if findings and marker[2].strip().split()[:1] != ["mermaid"]:
                code = (number, [])
            table = after_title = False
            continue
        if not line:
            flush()
            table = False
            continue
        heading = re.match(r"^(#{1,6})\s", line)
        if heading:
            flush()
            level = len(heading[1])
            if findings and level <= 2:
                failures.extend(findings.close())
                findings = None
            elif findings:
                findings.heading(number, level, _plain(line[heading.end() :]))
            after_title = level == 1
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
            if tags and not findings and _FINDINGS.search(line):
                findings, depth = Findings(number), 0
            if tags and findings:
                depth += line.count("<details") - line.count("</details")
                if depth <= 0:
                    failures.extend(findings.close())
                    findings = None
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
            start, kind, start_quote = number, "list", quote
            pending.append(line[item.end() :])
        else:
            if not pending:
                start, kind, start_quote = number, "paragraph", quote
            pending.append(line)
    flush()
    close_code()
    if findings:
        failures.extend(findings.close())
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
