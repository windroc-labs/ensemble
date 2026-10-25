#!/usr/bin/env python3
"""Checks a report's SVG figure: self-contained, no script.

    svg_check.py SVG

`ok`, exit 0; else exit 1, one line per failure: `too large: <n> bytes` (over MAX_BYTES),
`invalid svg: <error>` (not UTF-8 XML, or the root is not an SVG <svg>), `unsafe markup: <what>`
(<script>, <foreignObject>, an on* attribute, <!DOCTYPE>, a processing instruction) or
`external reference: <ref>` (an href, xlink:href, animated href or url() not starting with `#`;
@import, image(), image-set()).
Unreadable: `check failed: <error>`, exit 2.
"""

import re
import sys
import xml.etree.ElementTree as ET

MAX_BYTES = 100_000
SVG = "{http://www.w3.org/2000/svg}svg"
UNSAFE = {"script", "foreignobject"}
DOCTYPE = re.compile(r"(?i)<!DOCTYPE")
PI = re.compile(r"<\?(?!xml[\s?])([^\s?>]*)")
URL = re.compile(r"(?i)url\((?P<arg>[^)]*)\)?")
IMAGE = re.compile(r"(?i)\bimage(?:-set)?\(")
ANIMATED = {"to", "from", "by", "values"}
CSS_ESCAPE = re.compile(r"\\(?:([0-9a-fA-F]{1,6})[ \t\n\r\f]?|(.))", re.S)


def _local(name):
    return name.rpartition("}")[2]


def _css(text):
    """Failures in CSS text: a url() not starting with `#`, image(), image-set() or @import."""
    text = CSS_ESCAPE.sub(lambda m: chr(int(m[1], 16)) if m[1] else m[2], text)
    failures = [
        f"external reference: {m[0]}"
        for m in URL.finditer(text)
        if not m["arg"].strip().strip("'\"").strip().startswith("#")
    ]
    failures += [f"external reference: {m[0]}" for m in IMAGE.finditer(text)]
    if "@import" in text.lower():
        failures.append("external reference: @import")
    return failures


def _check(data):
    if len(data) > MAX_BYTES:
        return [f"too large: {len(data)} bytes"]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return ["invalid svg: not UTF-8"]
    # Expat detects BOM-less UTF-16 by its NULs; the scans below must read what it parses.
    if "\x00" in text:
        return ["invalid svg: NUL byte"]
    # A DTD can define entities that expand into references; never parse one.
    if DOCTYPE.search(text):
        return ["unsafe markup: <!DOCTYPE>"]
    failures = [f"unsafe markup: <?{m[1]}?>" for m in PI.finditer(text)]
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        return [*failures, f"invalid svg: {e}"]
    if root.tag != SVG:
        return [*failures, f"invalid svg: root <{root.tag}>"]
    for element in root.iter():
        if _local(element.tag).lower() in UNSAFE:
            failures.append(f"unsafe markup: <{_local(element.tag)}>")
        if _local(element.tag) == "style":
            failures += _css("".join(element.itertext()))
        animates_href = element.get("attributeName", "").rpartition(":")[2] == "href"
        for name, value in element.attrib.items():
            local = _local(name)
            if local.lower().startswith("on"):
                failures.append(f"unsafe markup: {local}")
            elif local == "href" or (animates_href and local in ANIMATED):
                failures += [
                    f"external reference: {ref.strip()}"
                    for ref in (
                        [v for v in value.split(";") if v.strip()] if local == "values" else [value]
                    )
                    if not ref.strip().startswith("#")
                ]
            else:
                failures += _css(value)
    return failures


def main(argv: list[str], *, out=sys.stdout) -> int:
    if len(argv) != 1:
        print("check failed: usage: svg_check.py SVG", file=out)
        return 2
    try:
        with open(argv[0], "rb") as f:
            data = f.read()
    except OSError as e:
        print(f"check failed: {e}", file=out)
        return 2
    failures = _check(data)
    print("\n".join(failures) or "ok", file=out)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
