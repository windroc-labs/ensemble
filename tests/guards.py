"""Repository-wide guard data: the layers, the trees the checks scan and the unit isolation
inventory. Each layer's own data: tests/guard_data/; the checks: tests/guard_checks/.
"""

LEVELS = (("utils", "session"), ("delegate",), ("orchestrator",), ("manager",))


UNIT_ISOLATION_ALLOWED = {}


PKG = "ensemble"


PROJECTS = {p: p.replace("-", "_") for level in LEVELS for p in level}


LAYERS = tuple(PROJECTS)


TREES = ("src/ensemble", "skills", "hooks", "tests", ".github")


TESTS = ("tests",)
