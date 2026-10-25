"""Recorded source text and exact summaries for comment budget checks."""

CODE = "def f(x):\n    return [x * 2 for x in range(100) if x % 3]\n" * 20
DOC = '"""' + "A long docstring that only explains what the code already says. " * 20 + '"""\n'
INIT = '"""' + "d" * 494 + '"""\n'
PURPOSE = (
    "The comment budget keeps comments short: meet it by cutting comments or docstrings, "
    "never by adding code to lower the ratio."
)
SOURCE_RULE = "source (15% of code + 500 characters per directory)"
TESTS_RULE = "tests (10% of code + 500 characters per directory)"


def chars(text):
    return len("".join(text.split()))


def over(folder, n, code, limit):
    allowed = limit * code // 100 + 500
    ratio = f"{n / code:.2%}" if code else "inf"
    return (
        f"  {folder}: {ratio} ({n} comment+docstring / {code} code characters; "
        f"{allowed} allowed, {n - allowed} over)"
    )
