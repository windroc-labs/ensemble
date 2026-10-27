"""Guard data owned by the repository's test infrastructure."""

PYTHON_CODE_ALLOWED = {
    (
        "tests/integration/isolation_integration_test.py",
        "fc13604a9ed3134649dccffb0c7d6d3f74e62361dfd680709dadc385a17ce802",
    ): "clean_env allocates one per-process TMUX_TMPDIR under a thread race, kept for a sandbox owner or removed at exit; needs fresh interpreters that exit with and without ENSEMBLE_TEST_SANDBOX, which run_tests.py never gives (it always sets the sandbox).",
    (
        "tests/integration/isolation_integration_test.py",
        "0eabbbdfd8b30a01d1eba616efb7934f18bb856782841a244e2ee6e840608bd7",
    ): "A forked child's clean_env resets to its own TMUX_TMPDIR and removes only that at exit, keeping the parent's fallback directory; needs a fresh unsandboxed interpreter that forks and exits, which run_tests.py never gives (it always sets the sandbox).",
    (
        "tests/integration/isolation_integration_test.py",
        "734a3aaee8ed013170081f25b3260c3870d5a7b042c905a3adbfff4b1d5c9975",
    ): "clean_env recreates its cached TMUX_TMPDIR after removal, which an unsandboxed process removes at exit; needs fresh interpreters that exit with and without the sandbox, which run_tests.py never gives (it always sets the sandbox).",
    (
        "tests/integration/isolation_integration_test.py",
        "a28d15339588cb7650601d111f96e5fac172ff523bb20eb711f24f8a2413fa50",
    ): "The atexit fallback cleanup must not hang on tests.utils.sandbox._TMUX_LOCK held by a daemon thread; needs a fresh unsandboxed interpreter that exits, which run_tests.py never gives (it always sets the sandbox).",
    (
        "tests/integration/isolation_integration_test.py",
        "6ce737fd1d32c7edfbaa2a968e5ba5e4dc9e6e3f8685201efdab46b90e445edc",
    ): "Constructs isolation.Gate directly to check the exact isolation.DENIED set and reporting()'s single-file exemption, with its unit/integration argv checks on that gate; tests may not import isolation (ruff TID251) and run_tests.py never exposes reporting().",
    (
        "tests/integration/isolation_integration_test.py",
        "2324ee596f886b26c39109c0e75c5b4e3ade35486abc20c8b97eddb9c1ae4f94",
    ): "The atexit fallback cleanup of clean_env's TMUX_TMPDIR stops the tmux servers it owns and skips symlinked ones; needs a fresh unsandboxed interpreter that exits, which run_tests.py never gives (it always sets the sandbox).",
    (
        "tests/integration/imports_integration_test.py",
        "b0328cd127a47ff8244ca6bf0c49ce9a53a3d77fd3d6daa81c3979f617e6bc5d",
    ): "Imports each ensemble module named in argv in a fresh interpreter to check its import-time side effects; the module names are the test's parameters.",
    (
        "tests/integration/module_loading_integration_test.py",
        "8b068c0827660a51d6c9fa6475221d857fa42279c4861a0b7e88609bd5349de8",
    ): "Imports every ensemble module named in argv in a fresh interpreter to check where each loads from; the module names are the test's parameters.",
}


EMBEDDED_PROGRAMS_ALLOWED = {
    (
        "tests/integration/install_integration_test.py",
        "7226227308e90cab184cb47b28a0ed58d79f5692206155d1f35d310d1c759d9d",
    ): "Existing installation fixture sources shell startup and resolves the installed OpenCode command in a fresh shell.",
    (
        "tests/integration/isolation_integration_test.py",
        "b965d7a59c855e7af2b95c14041a59b547c6bf7442d5ffeded014ad510fce5fa",
    ): "Existing process fixture checks clean env children cannot reach the parents private default tmux server.",
    (
        "tests/integration/isolation_integration_test.py",
        "fc13604a9ed3134649dccffb0c7d6d3f74e62361dfd680709dadc385a17ce802",
    ): "Existing process fixture checks clean env reuses a process directory and respects its cleanup owner.",
    (
        "tests/integration/isolation_integration_test.py",
        "0eabbbdfd8b30a01d1eba616efb7934f18bb856782841a244e2ee6e840608bd7",
    ): "Existing process fixture checks forked clean env children cannot remove the parents fallback directory.",
    (
        "tests/integration/isolation_integration_test.py",
        "734a3aaee8ed013170081f25b3260c3870d5a7b042c905a3adbfff4b1d5c9975",
    ): "Existing process fixture checks clean env recreates a cached directory when it is removed.",
    (
        "tests/integration/isolation_integration_test.py",
        "2324ee596f886b26c39109c0e75c5b4e3ade35486abc20c8b97eddb9c1ae4f94",
    ): "Existing process fixture checks fallback cleanup stops owned servers and skips linked servers.",
    (
        "tests/integration/isolation_integration_test.py",
        "a28d15339588cb7650601d111f96e5fac172ff523bb20eb711f24f8a2413fa50",
    ): "Existing process fixture checks fallback exit cleanup ignores a daemon held allocation lock.",
    (
        "tests/integration/isolation_integration_test.py",
        "6ce737fd1d32c7edfbaa2a968e5ba5e4dc9e6e3f8685201efdab46b90e445edc",
    ): "Existing process fixture checks hooks reject unit effects and unisolated integration tools.",
    (
        "tests/unit/checks_test.py",
        "99fdaa70172d3c5eaeecfd6adfcca37d659a778daee6ff2f7f38f149466b02ba",
    ): "Existing discovery fixture needs functions whose defining module is the synthetic module, alongside an imported function.",
    (
        "tests/unit/checks_test.py",
        "6dbf0cae68b3e24a719650f41f70507996ee26060e3c3f0c3bc1c4c775ba974b",
    ): "Existing duplicate-discovery fixture needs identically named functions defined by two distinct synthetic modules.",
}


STANDALONE = frozenset(
    {
        ".github/mini_op_guard.py",
        ".github/worker_codex.py",
        ".github/worker_codex/tools.py",
        ".github/worker_codex/skills.py",
        ".github/worker_codex/state.py",
        ".github/worker_codex/reload.py",
    }
)


COMMAND_DOCS = ("skills/",)


KEYCHAIN_FREE_ALLOWED = {
    (
        "src/broker/internal/broker/keychain_dialog_darwin_test.go",
        "c96d40cdb530d8fcc9f1e9383f88fa44f2c0217eb28ba9291d96b558f8504b92",
    ): "A fileKeychain without ui on a path with no keychain file: GetWithDialog and TrustAppsByDialog return ErrNeedsDialog before any Security call.",
}
