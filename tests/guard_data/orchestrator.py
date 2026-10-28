"""The orchestrator layer's guard data (tests/guard_data/__init__.py)."""

import re

from tests.guards import PKG

ORCH = "orchestrator"


ORCH_PKG = f"{PKG}.{ORCH}"


# config.toml tables keyed by client name: a `# local: <client> = <whole number>` key line in one may name a client;
# the rest of the line, and a real or plain-commented key line, may not
CLIENT_LIMIT_TABLES = ("client_max_runs",)


ORCH_SRC = f"src/{PKG}/{ORCH}/"


ORCH_PROJECT = "orchestrator"

ENTRY_MODULES = {ORCH_PROJECT: {"worker_cli.py"}}


ORCH_FILES = f"{ORCH_SRC}"


ORCH_SESSION_CONTRACT = "orchestrator reaches session only through delegate"


OBJECT_EXPORT_MODULES = (f"{ORCH_FILES}__init__.py",)


SUBPACKAGES = {
    ORCH_PROJECT: {"issue", "trackers"},
}


# FR-25: the router only triggers a run; its Delegate's harness runs it and calls write-back
ORCH_ABSENT = ("engine/run.py",)


NOQA_ALLOWED = {
    (
        f"{ORCH_SRC}engine/run_comments.py",
        "BLE001",
        "_bounded.run",
    ): "Forward failures to the caller thread.",
    (
        f"{ORCH_SRC}engine/promote.py",
        "BLE001",
        "Promoter.pending",
    ): "Keep reading the other handoffs.",
    (
        f"{ORCH_SRC}engine/promote.py",
        "BLE001",
        "Promoter.run",
    ): "Keep processing other handoffs.",
    (
        f"{ORCH_SRC}engine/promote.py",
        "BLE001",
        "run_prune",
    ): "Prune failures leave promotions intact.",
    (
        f"{ORCH_SRC}engine/router.py",
        "BLE001",
        "start_run",
    ): "A recorded launch may fail.",
    (
        f"{ORCH_SRC}engine/writeback.py",
        "BLE001",
        "progress",
    ): "A sink failure must not stop the run.",
    (
        f"{ORCH_SRC}engine/writeback.py",
        "BLE001",
        "finish",
    ): "Report the session after failed steps.",
}


OPTION_TUPLES_ALLOWED = {
    (f"{ORCH_FILES}engine/log.py", "ONCE"): "the log fields keying a once-only line",
    (
        f"{ORCH_FILES}engine/prune.py",
        "ARCHIVE_ROLES",
    ): "roles whose Done issues are archived",
}


CONFIG_PY = f"{ORCH_SRC}engine/config.py"


ENTRIES = (
    f"{ORCH_SRC}cli.py",
    f"{ORCH_SRC}issue/command.py",
    *(f"{ORCH_SRC}engine/{name}.py" for name in ("promote", "prune", "router")),
)


ONCE = (
    f"{ORCH_PKG}.engine.config.load",
    f"{ORCH_PKG}.engine.log.use_logs",
)


LOAD_HELPERS = ("_load_config", "_runnable", "_docs")


HANDOFF_HOME = f"{ORCH_SRC}engine/flow/handoff.py"


COMMAND_SRC = (f"{ORCH_SRC}",)


COMMAND_FILES = (f"{ORCH_SRC}config.toml",)


PYTHON_CODE_ALLOWED = {
    (
        "tests/orchestrator/integration/config_isolation_integration_test.py",
        "9f4ce7132599a4c1ee5c309ce0cd746c9f8c2eb407aece1b8c41af9fea3cd09e",
    ): "Import-time globals and file opens of ensemble.orchestrator.engine.config (no public re-export) observed in a fresh interpreter.",
    (
        "tests/orchestrator/integration/config_isolation_integration_test.py",
        "4df5a38f4023d418d717f321dc198079e026879a2067a2e977871fb2353d9329",
    ): "engine.config.load() in a copied-package deployment interpreter; no public loader, and CLI commands that load config go on to call Linear.",
    (
        "tests/orchestrator/integration/linear_run_extension_integration_test.py",
        "341c66f3420ffd966f8481cf15f0eca595a67f71b765d16cbee1793a768d98a9",
    ): "Creating a pipeline run with a config-loaded external Delegate needs engine.config.load() and runs.note; no public loader.",
}


# engine/ imports nothing under trackers/, even indirectly, but the router's Linear transport
ENGINE_CONTRACT = "engine imports no tracker"


ENGINE_WIRING = frozenset({f"{ORCH_PKG}.engine.router -> {ORCH_PKG}.trackers.linear.client"})


GRAPHQL = re.compile(r"\b(?:query|mutation)\s*(?:\w+\s*)?(?:\(\s*\$|\{)")


# Linear terms engine/flow/, the tracker-agnostic core, never names
FLOW_LINEAR = re.compile(rf"{GRAPHQL.pattern}|linear|graphql|project|email|state id|label id", re.I)


EMBEDDED_PROGRAMS_ALLOWED = {}
