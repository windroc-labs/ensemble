"""The delegate layer's guard data (tests/guard_data/__init__.py)."""

from tests.guards import PKG

DELEGATE_SRC = f"delegate/src/{PKG}/delegate/"

CONFIG_FILE = f"delegate/src/{PKG}/delegate/engine/config.py"


DELEGATE_INTERFACE = "tests/delegate_interface.json"


DELEGATE_INTERFACE_MODULES = ("ensemble.delegate",)


SUBPACKAGES = {
    "delegate": {"composer", "event", "team"},
}


ENTRY_MODULES = {
    "delegate": {"config.py"},
}


OBJECT_EXPORT_MODULES = (
    "delegate/src/ensemble/delegate/__init__.py",
    "delegate/src/ensemble/delegate/composer/__init__.py",
    "delegate/src/ensemble/delegate/event/__init__.py",
)


OBJECT_EXPORTS_ALLOWED = {}


OPTION_TUPLES_ALLOWED = {
    (
        f"delegate/src/{PKG}/delegate/event/outcome.py",
        "SAVES_DELIVERABLE",
    ): "the destinations whose deliverable comes back, a subset",
}


COMMAND_SRC = (f"delegate/src/{PKG}/delegate/",)


COMMAND_FILES = ("delegate/src/ensemble/delegate/config.toml",)


COMMAND_DOCS = ("delegate/skills/", f"delegate/src/{PKG}/delegate/team/")


PYTHON_CODE_ALLOWED = {
    (
        "delegate/tests/integration/harness_host_integration_test.py",
        "9ef5d7b8ea44b2330980d9492b4a186a08e4188d91f663f3ef80461300a8581a",
    ): "Loads the unexported delegate config reader in a fresh interpreter to configure Session for the real harness Host test.",
    (
        "delegate/tests/integration/child_env_integration_test.py",
        "01f96d8a4b397c1843ac5be653852bf223ac806eeea14f9ce81df0d6da168ed7",
    ): "Harness PYTHONPATH and composer access env under a source vs installed layout need a fresh interpreter with HarnessCase's RecordingHost, injectable only in-process.",
    (
        "delegate/tests/integration/composer/child.py",
        "a063662e735b26ff0fd31341f73be34e1767f34da54766fbe5136f2cfb06dfd2",
    ): "Composer access/render/check_team_dirs (ensemble.delegate.composer, not re-exported) in a fresh -P -S interpreter on the src roots, asserting its modules load from this tree and no team tools/ module is imported.",
    (
        "delegate/tests/integration/harness_contract_integration_test.py",
        "80ef6663d86afce1c0254ea3bb2ece6e4f521d955191b57728bff11dd33f957a",
    ): "The contract's class-method test calls HarnessDelegate.usage without session_cls, which configures the process-global Session from config; tests may not patch HostedSession._setup, so only a fresh interpreter keeps that out of the test process.",
    (
        "delegate/tests/integration/harness_integration_test.py",
        "0c310e5bc68290afe8c434b04617d455b71fccf0990bee6abb55f200ff5e53ed",
    ): "HarnessDelegate.usage with its default session_cls configures the process-global Session and loads UsageClient through it; no public fresh session class exists and tests may not configure or patch the shared Session in-process.",
    (
        "delegate/tests/integration/harness_integration_test.py",
        "31f0555981b3b4fb800c7523772acb7fcfe4b923ec7f7c9ffee815c641b3bb7b",
    ): "Two-process detach race needs PersistentSession's disk-backed fake, injectable only as a session_cls class object.",
    (
        "delegate/tests/integration/harness_owner_integration_test.py",
        "6a0472ac98f93b3e6a7f3673e257b036fb428f16e5c26a5a1707094b4473fffa",
    ): "Foreground owner process needs the scripted process-free HarnessSession (delegate.tests), injectable only as a session_cls class object.",
}


EMBEDDED_PROGRAMS_ALLOWED = {
    (
        "delegate/tests/fakes/callback_recorder.py",
        "9eab95e939ba98ce47fe6013b159a0526b7a4d2e044077d2b53755ba304ec33f",
    ): "Existing callback recorder fixture delays a child to observe callback ordering.",
    (
        "delegate/tests/integration/bin_integration_test.py",
        "c710e2832b40273e5a68ddb9026256785f0d2f47e44d3f285c6132d45114a427",
    ): "Existing executable-selection fixture checks the system interpreter meets Python 3.11.",
    (
        "delegate/tests/integration/child_env_integration_test.py",
        "01f96d8a4b397c1843ac5be653852bf223ac806eeea14f9ce81df0d6da168ed7",
    ): "Existing environment fixture captures client launch environments from a separate caller process.",
    (
        "delegate/tests/integration/harness_contract_integration_test.py",
        "80ef6663d86afce1c0254ea3bb2ece6e4f521d955191b57728bff11dd33f957a",
    ): "Existing process fixture checks class methods answer for its roles and refuse an unknown one.",
    (
        "delegate/tests/integration/harness_integration_test.py",
        "0c310e5bc68290afe8c434b04617d455b71fccf0990bee6abb55f200ff5e53ed",
    ): "Existing process fixture checks role client usage preserves sentinel identity and none without starting.",
    (
        "delegate/tests/integration/harness_integration_test.py",
        "31f0555981b3b4fb800c7523772acb7fcfe4b923ec7f7c9ffee815c641b3bb7b",
    ): "Existing process fixture checks two processes launch one harness.",
    (
        "delegate/tests/integration/harness_integration_test.py",
        "18800c4a6c662fc5b2c36b4a8dee771e4d0a5e7d4fc61241d0e8f38c6d49b34e",
    ): "Existing process fixture checks two processes resume once and return the same record.",
    (
        "delegate/tests/integration/harness_owner_integration_test.py",
        "6a0472ac98f93b3e6a7f3673e257b036fb428f16e5c26a5a1707094b4473fffa",
    ): "Existing harness ownership fixture starts an independent owner process for recovery checks.",
}
