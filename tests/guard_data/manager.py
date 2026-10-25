"""The manager layer's guard data (tests/guard_data/__init__.py)."""

COMMAND_DOCS = ("manager/skills/",)


ENTRY_MODULES = {
    "manager": {"rearm.py"},
}


STANDALONE = frozenset({"manager/src/ensemble/manager/rearm.py"})


NO_COVER_ALLOWED = {
    (
        "manager/src/ensemble/manager/engine/holder.py",
        "Holder.run",
    ): "Spawns the held command in its own process group, gives it the terminal, forwards signals.",
}


PYTHON_CODE_ALLOWED = {
    (
        "manager/tests/integration/events_integration_test.py",
        "22088daca1e84da5de6c5f3b9e6327a45042839d4a186cda2cae54d16812ce3b",
    ): "Crash at each os.replace of Events.rotate/recover (ensemble.manager.engine.events, not re-exported) needs a process killed mid-rename.",
    (
        "manager/tests/integration/holder_integration_test.py",
        "e95c71f9314d674cc510604958807fd5226f6c820b40fecc12d5cbbc037a5ea3",
    ): "Holder.run's tty-handoff failure path needs os.tcsetpgrp patched inside the holder's own process; Holder is not re-exported.",
    (
        "manager/tests/integration/events_integration_test.py",
        "6fc2361e2a854a715b8c09da5acf2163809a7916d747e9250a154594c4d14f4c",
    ): "Dies (os._exit) inside the rotation lock right after Events.rotate and a durable ledger save, so recovery must finish without rollback; Events (ensemble.manager.engine.events) is not re-exported.",
}


EMBEDDED_PROGRAMS_ALLOWED = {
    (
        "manager/tests/live_crew.py",
        "9daa6ab4c97cc1be973fdcc46409449cf75024a77d4633ac96c5fa5f35ef9279",
    ): "Existing crash fixture marks child exit and holds the shell until the test releases its gate.",
    (
        "manager/tests/integration/crew_cli_integration_test.py",
        "7dd5c56b98872100110ef2568b8730ca9e1be5f02f50a8257f1d9428e58eb040",
    ): "Existing crew CLI fixture races independent appenders against the shared locked log.",
    (
        "manager/tests/integration/holder_integration_test.py",
        "5b97f978e713712d6ab39e9d10292af6a1c28933736d4242951f5b6ffb7497a6",
    ): "Existing process fixture checks child environment replaces inherited identity and preserves exit code.",
    (
        "manager/tests/integration/holder_integration_test.py",
        "b1ee562c7ce5b505e3c9868c3e68a6372c4ee8caadc2efc709af38bf831c15b7",
    ): "Existing holder fixture observes whether inherited terminal stop signals remain blocked.",
    (
        "manager/tests/integration/receipt_integration_test.py",
        "4cedd9588e01d09bf9c5098ae80527faab7aebb3f136972b2259847be301685a",
    ): "Existing process fixture checks child stdout and alias survive crew death before commit.",
    (
        "manager/tests/integration/receipt_integration_test.py",
        "c06318614be97fc19dcc6601e28022065a8894247d14f5f0eb5fbc299babb48e",
    ): "Existing process fixture checks child stdout and alias survive crew death before commit.",
    (
        "manager/tests/integration/roster_integration_test.py",
        "fed174559c561fddd2d1ee756d92c737857c79467b373ac84efb143712f9e310",
    ): "Existing process fixture checks concurrent distinct process writes preserve both entries.",
}
