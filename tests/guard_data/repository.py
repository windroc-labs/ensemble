"""Guard data owned by the repository's test infrastructure."""

PYTHON_CODE_ALLOWED = {
    (
        "tests/integration/isolation_integration_test.py",
        "577bbdfa3bd428ed72f4db7bfc19d1d6dc78544da78fa886338c9334b925a99f",
    ): "clean_env allocates one per-process TMUX_TMPDIR under a thread race, kept for a sandbox owner or removed at exit; needs fresh interpreters that exit with and without ENSEMBLE_TEST_SANDBOX, which run_tests.py never gives (it always sets the sandbox).",
    (
        "tests/integration/isolation_integration_test.py",
        "0189d96ba12a767ba469154c71e75bac6f84d71166aff9b2bca21e8b6c21e11f",
    ): "A forked child's clean_env resets to its own TMUX_TMPDIR and removes only that at exit, keeping the parent's fallback directory; needs a fresh unsandboxed interpreter that forks and exits, which run_tests.py never gives (it always sets the sandbox).",
    (
        "tests/integration/isolation_integration_test.py",
        "5b6880ba2d70067ace4d479a90a4103afeb733e97688c6416f180ec7b2382d60",
    ): "clean_env recreates its cached TMUX_TMPDIR after removal, which an unsandboxed process removes at exit; needs fresh interpreters that exit with and without the sandbox, which run_tests.py never gives (it always sets the sandbox).",
    (
        "tests/integration/isolation_integration_test.py",
        "625543833e39373ea2f663bd9955446c9eba31d10f3f485f667a53e971774184",
    ): "The atexit fallback cleanup must not hang on utils.tests.sandbox._TMUX_LOCK held by a daemon thread; needs a fresh unsandboxed interpreter that exits, which run_tests.py never gives (it always sets the sandbox).",
    (
        "tests/integration/isolation_integration_test.py",
        "6ce737fd1d32c7edfbaa2a968e5ba5e4dc9e6e3f8685201efdab46b90e445edc",
    ): "Constructs isolation.Gate directly to check the exact isolation.DENIED set and reporting()'s single-file exemption, with its unit/integration argv checks on that gate; tests may not import isolation (ruff TID251) and run_tests.py never exposes reporting().",
    (
        "tests/integration/isolation_integration_test.py",
        "6f3e4f1618b409983a7f99cbb9045fdc3b8e67b4b6dc4400f033658942018b84",
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
        "577bbdfa3bd428ed72f4db7bfc19d1d6dc78544da78fa886338c9334b925a99f",
    ): "Existing process fixture checks clean env reuses a process directory and respects its cleanup owner.",
    (
        "tests/integration/isolation_integration_test.py",
        "0189d96ba12a767ba469154c71e75bac6f84d71166aff9b2bca21e8b6c21e11f",
    ): "Existing process fixture checks forked clean env children cannot remove the parents fallback directory.",
    (
        "tests/integration/isolation_integration_test.py",
        "5b6880ba2d70067ace4d479a90a4103afeb733e97688c6416f180ec7b2382d60",
    ): "Existing process fixture checks clean env recreates a cached directory when it is removed.",
    (
        "tests/integration/isolation_integration_test.py",
        "6f3e4f1618b409983a7f99cbb9045fdc3b8e67b4b6dc4400f033658942018b84",
    ): "Existing process fixture checks fallback cleanup stops owned servers and skips linked servers.",
    (
        "tests/integration/isolation_integration_test.py",
        "625543833e39373ea2f663bd9955446c9eba31d10f3f485f667a53e971774184",
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
        ".github/worker_codex.py",
        ".github/worker_codex/tools.py",
        ".github/worker_codex/skills.py",
        ".github/worker_codex/state.py",
        ".github/worker_codex/reload.py",
        ".github/worker_codex/stage.py",
    }
)
