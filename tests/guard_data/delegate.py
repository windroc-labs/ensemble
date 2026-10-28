"""The delegate layer's guard data (tests/guard_data/__init__.py)."""

from tests.guards import PKG

DELEGATE_SRC = f"src/{PKG}/delegate/"

CONFIG_FILE = f"src/{PKG}/delegate/engine/config.py"


DELEGATE_INTERFACE = "tests/delegate_interface.json"


DELEGATE_INTERFACE_MODULES = ("ensemble.delegate",)


SUBPACKAGES = {
    "delegate": {"composer", "event", "team"},
}


ENTRY_MODULES = {
    "delegate": {"config.py"},
}


OBJECT_EXPORT_MODULES = (
    "src/ensemble/delegate/__init__.py",
    "src/ensemble/delegate/composer/__init__.py",
    "src/ensemble/delegate/event/__init__.py",
)


OBJECT_EXPORTS_ALLOWED = {}


OPTION_TUPLES_ALLOWED = {}


COMMAND_SRC = (f"src/{PKG}/delegate/",)


COMMAND_FILES = ("src/ensemble/delegate/config.toml",)


COMMAND_DOCS = (f"src/{PKG}/delegate/team/",)


PYTHON_CODE_ALLOWED = {
    (
        "tests/delegate/integration/child_env_integration_test.py",
        "118b630ac258347793732e1124cc9ae51b0aba0fd29877ba2917e9a82d2cb6e0",
    ): "Harness PYTHONPATH and composer access env under a source vs installed layout need a fresh interpreter with HarnessCase's FakeControl drive, patched only in-process.",
    (
        "tests/delegate/integration/composer/child.py",
        "a063662e735b26ff0fd31341f73be34e1767f34da54766fbe5136f2cfb06dfd2",
    ): "Composer access/render/check_team_dirs (ensemble.delegate.composer, not re-exported) in a fresh -P -S interpreter on the src roots, asserting its modules load from this tree and no team tools/ module is imported.",
    (
        "tests/delegate/integration/harness_contract_integration_test.py",
        "59fe3b8a4b565bc2929dcc4ca9a08aab98884f0813693836ebb2241fd595bb17",
    ): "The contract's class-method test calls HarnessDelegate.usage without session_cls, which configures the process-global Session from config; tests may not patch HostedSession._setup, so only a fresh interpreter keeps that out of the test process.",
    (
        "tests/delegate/integration/harness_integration_test.py",
        "18f10c6337d4fdd297ccadc7943384323e8832b3ecc0552fa1d5dee70c7ef775",
    ): "HarnessDelegate.usage with its default session_cls configures the process-global Session and loads UsageClient through it; no public fresh session class exists and tests may not configure or patch the shared Session in-process.",
    (
        "tests/delegate/integration/harness_integration_test.py",
        "6388d395c30e89fdf22436b81638660fae74038141f6087419c4edfa2dd7b654",
    ): "Two-process detach race needs DrivenDelegate's disk-backed FakeControl drive and PersistentSession's Hosts, injectable only as a session_cls class object.",
    (
        "tests/delegate/integration/harness_integration_test.py",
        "8b560b5807af62409d5d047244c51131db9cb39657059b5cba03ab83fbd61285",
    ): "Two-process resume race needs DrivenDelegate's disk-backed FakeControl drive.",
    (
        "tests/delegate/integration/harness_owner_integration_test.py",
        "a1c12b66651799dd60cf58c932a8e2be8f34089ecfb19bcef946f5b3b037c32f",
    ): "Foreground owner process needs the scripted process-free HarnessSession (tests.delegate), injectable only as a session_cls class object.",
}


EMBEDDED_PROGRAMS_ALLOWED = {
    (
        "tests/delegate/fakes/callback_recorder.py",
        "9eab95e939ba98ce47fe6013b159a0526b7a4d2e044077d2b53755ba304ec33f",
    ): "Existing callback recorder fixture delays a child to observe callback ordering.",
    (
        "tests/delegate/integration/bin_integration_test.py",
        "c710e2832b40273e5a68ddb9026256785f0d2f47e44d3f285c6132d45114a427",
    ): "Existing executable-selection fixture checks the system interpreter meets Python 3.11.",
    (
        "tests/delegate/integration/child_env_integration_test.py",
        "118b630ac258347793732e1124cc9ae51b0aba0fd29877ba2917e9a82d2cb6e0",
    ): "Existing environment fixture captures client launch environments from a separate caller process.",
    (
        "tests/delegate/integration/harness_contract_integration_test.py",
        "59fe3b8a4b565bc2929dcc4ca9a08aab98884f0813693836ebb2241fd595bb17",
    ): "Existing process fixture checks class methods answer for its roles and refuse an unknown one.",
    (
        "tests/delegate/integration/harness_integration_test.py",
        "18f10c6337d4fdd297ccadc7943384323e8832b3ecc0552fa1d5dee70c7ef775",
    ): "Existing process fixture checks role client usage preserves sentinel identity and none without starting.",
    (
        "tests/delegate/integration/harness_integration_test.py",
        "6388d395c30e89fdf22436b81638660fae74038141f6087419c4edfa2dd7b654",
    ): "Existing process fixture checks two processes launch one harness.",
    (
        "tests/delegate/integration/harness_integration_test.py",
        "8b560b5807af62409d5d047244c51131db9cb39657059b5cba03ab83fbd61285",
    ): "Existing process fixture checks two processes resume once and return the same record.",
    (
        "tests/delegate/integration/harness_owner_integration_test.py",
        "a1c12b66651799dd60cf58c932a8e2be8f34089ecfb19bcef946f5b3b037c32f",
    ): "Existing harness ownership fixture starts an independent owner process for recovery checks.",
}
