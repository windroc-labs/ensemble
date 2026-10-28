"""Recorded source text and exact summaries for comment budget checks."""

CODE = "def f(x):\n    return [x * 2 for x in range(100) if x % 3]\n" * 20
DOC = '"""' + "A long docstring that only explains what the code already says. " * 20 + '"""\n'
INIT = '"""' + "d" * 294 + '"""\n'
PURPOSE = (
    "The comment budget keeps comments short: meet it by cutting comments or docstrings, "
    "never by adding code to lower the ratio."
)
SOURCE_RULE = "source (10% of code + 300 characters per changed file)"
TESTS_RULE = "tests (5% of code + 300 characters per changed file)"


def chars(text):
    return len("".join(text.split()))


def over(path, n, code, limit):
    allowed = limit * code // 100 + 300
    ratio = f"{n / code:.2%}" if code else "inf"
    return (
        f"  {path}: {ratio} ({n} comment+docstring / {code} code characters; "
        f"{allowed} allowed, {n - allowed} over)"
    )
