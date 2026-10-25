"""The utils layer's guard data (tests/guard_data/__init__.py)."""

STANDALONE = frozenset({"src/ensemble/utils/process_control.py"})


NO_COVER_ALLOWED = {
    (
        "src/ensemble/utils/process_control.py",
        "_Group._anchor",
    ): "Runs only in the forked group leader, which spawns the program and ends in os._exit.",
    (
        "src/ensemble/utils/process_control.py",
        "ProcessControl.serve",
    ): "The detached runner's control socket server: binds a Unix socket and launches the group.",
}


EMBEDDED_PROGRAMS_ALLOWED = {}
