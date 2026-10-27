"""The session layer's guard data (tests/guard_data/__init__.py)."""

from tests.guards import PKG

SESSION = "session"


SESSION_SRC = f"src/{PKG}/{SESSION}/"


PLUGIN_SOURCE_CONFIG = f"{SESSION_SRC}config.toml"


TMUX_HOST = "src/ensemble/session/hosts/tmux/host.py"


TMUX_HOME = (
    f"{SESSION_SRC}hosts/tmux/",
    f"{SESSION_SRC}viewer/",
    f"{SESSION_SRC}engine/tmux_cli.py",
    # The Docker Host drives its container's tmux: its Host and hook callback are tmux code too.
    f"{SESSION_SRC}hosts/docker/host.py",
    f"{SESSION_SRC}hosts/docker/hook.py",
)


CLIENTS_HOME = f"{SESSION_SRC}clients/registry.py"


STATUS_LOG_HOME = f"{SESSION_SRC}engine/status_log.py"


STATUS_LOG_CLOCK = "%H:%M:%S"


SESSION_COMMAND = "ensemble-session"


SESSION_INTERFACE = "tests/session_interface.json"


SESSION_INTERFACE_MODULES = ("ensemble.session", "ensemble.session.extension")


SUBPACKAGES = {
    SESSION: {"clients", "harness", "hosts", "modes", "viewer"},
}


ENTRY_MODULES = {
    SESSION: {"extension.py"},
}


OBJECT_EXPORT_MODULES = (
    f"{SESSION_SRC}__init__.py",
    *(f"{SESSION_SRC}{module}" for module in sorted(ENTRY_MODULES[SESSION])),
)


OBJECT_EXPORTS_ALLOWED = {
    (f"{PKG}.{SESSION}", "SESSION_NAME"): "shared session-name validation pattern",
    (f"{PKG}.{SESSION}", "SESSION_NAME_RULE"): "shared session-name validation message",
    (f"{PKG}.{SESSION}", "get_viewer"): "the decided public entry to the Viewer",
}


STANDALONE = frozenset(
    f"{SESSION_SRC}{path}"
    for path in (
        "harness/guard.py",
        "clients/codex/common/app_server.py",
        "clients/opencode/launcher.py",
        "clients/opencode/probe.py",
        "clients/opencode/transcript.py",
    )
)


NOQA_ALLOWED = {
    (
        f"{SESSION_SRC}modes/headless.py",
        "BLE001",
        "HeadlessMode._pump",
    ): "Forward failures to the caller thread.",
    (
        f"{SESSION_SRC}modes/in_process.py",
        "BLE001",
        "InProcessMode._pump",
    ): "Forward failures to the caller thread.",
    (
        f"{SESSION_SRC}modes/tui_hook.py",
        "BLE001",
        "run",
    ): "the hook must never fail the client",
    (
        f"{SESSION_SRC}clients/codex/tui/replies.py",
        "BLE001",
        "on_hook",
    ): "steer replies must never fail the hook or keep its done line",
    (
        f"{SESSION_SRC}clients/claude/replies.py",
        "BLE001",
        "on_hook",
    ): "replies must never fail the hook or keep its done line",
}


OPTION_TUPLES_ALLOWED = {
    (f"{SESSION_SRC}clients/claude/tui.py", "MONTHS"): "month names, indexed by month",
    (f"{SESSION_SRC}clients/claude/tui.py", "WEEKDAYS"): "weekday names, indexed by weekday",
    (
        f"{SESSION_SRC}viewer/grid.py",
        "GRID_HOOKS",
    ): "tmux hook names the grid sets and unsets together",
}


COMMAND_SRC = (SESSION_SRC,)


PYTHON_CODE_ALLOWED = {
    (
        "tests/session/integration/engine/base_integration_test.py",
        "1a36105f9ea4517947d974b341422a0263475c87d737adc54c4b74455b260c6a",
    ): "Session class-method dispatch before and after configuration requires a fresh interpreter; Hosts is imported from its defining module after its public export was removed.",
    (
        "tests/session/integration/extension_client_integration_test.py",
        "9b00dc3c8b97bf1d4ade627cbf8d52ed6d2f9f82c829b5a55b6732f458c6ff3b",
    ): "Loads the unexported session config reader in a fresh installed-layout interpreter to configure and launch the extension Client.",
    (
        "tests/session/integration/installed_integration_test.py",
        "8a84d0e700c5ef374725eec9289eb2f33c67a387b7d29025c073c55bbedf6943",
    ): "Loads the unexported session config reader from the copied installation; only a fresh interpreter proves it uses the installed package.",
    (
        "tests/session/integration/clients/codex/tui/start_integration_test.py",
        "c8fb72314c0cf520ad5a14e8e027bb27d258fba56fabe1318331d8c7ee663c71",
    ): "Fresh-interpreter import of codex tui submodules: sys.path untouched, canonical clients.common/codex.common identity, no _ensemble_codex_* copies; needs internal submodules and clean sys.modules.",
    (
        "tests/session/integration/clients/codex/tui/start_integration_test.py",
        "69680777b770082268cec7a6126b7a273da4e5d5babbf5109d7d1845300d7ea9",
    ): "Imports codex.tui.HOOKS under -I with only session+utils roots, as isolated hook scripts do; HOOKS is not re-exported.",
    (
        "tests/session/integration/clients/codex/tui/start_integration_test.py",
        "81320d3e3cd6d6bc389d76d2420665e794512ac4062ad326c5fb2d442990f2fa",
    ): "Runs codex tui start as __main__ under -P -s from PYTHONPATH roots (and from a cwd decoy), then checks in that interpreter that internal codex modules are canonical; needs internal submodules and a fresh interpreter.",
    (
        "tests/session/integration/extension_client_integration_test.py",
        "ec5dfcbc92032c9ec70596f6a779897ed3305cd4f9acfd399665c83bdcff3d2f",
    ): "Codex tui launch (argv, PYTHONPATH) from an installed-layout site through HostedSession's client registry; no public API returns a configured Client's launch, and only a fresh interpreter on the copied site sees that layout.",
    (
        "tests/session/integration/hosted_integration_test.py",
        "2443c6161954fdc9009fbdde92c6fc001de6e6c6f62f0fde8fb162ae6a0d74d6",
    ): "HostedSession.list, Session.list and TuiStore.attach refused while HostedSession is unconfigured; tests may not patch HostedSession._setup (test_boundaries), and another test may have configured it in-process, so only a fresh interpreter has it unconfigured.",
    (
        "src/ensemble/session/engine/tmux_cli.py",
        "2353827626a3c6e192d6841bde2f5dfdcdbbc4a8bff857d91303607d99299413",
    ): "Shipped tmux pane wrapper: stdlib-only f-string interpolating TERMINAL_KEYS (one home), run with -I; production code, no ensemble import.",
    (
        "tests/session/integration/clients/codex/tui/start_integration_test.py",
        "7e4699b393a08c5b495eef12293120ce9612e2b3e1ddb34344a1329cd6505d0f",
    ): "Runs internal codex tui start (a hook entry named in argv, not a layer command) as __main__ under -P -s and counts json.loads calls in that interpreter.",
    (
        "tests/session/integration/clients/codex/tui/start_integration_test.py",
        "0842107b14800fd722eaff5e4ccafe1155e2741a5ec4a5fce845607c25f369af",
    ): "Runs internal codex tui start (named in argv) as __main__ under -P -s with a lone-surrogate argument to check its one-line stderr and exit status.",
    (
        "tests/session/integration/clients/codex/tui/start_integration_test.py",
        "57fc6aac3dc49a226570cf6f0855e21aea62e766ca50d9db62743186f3e0e6f6",
    ): "Runs the standalone account probe file by path under -I and audits that it imports only the standard library; run_path of that file is the subject.",
}


EMBEDDED_PROGRAMS_ALLOWED = {
    (
        "src/ensemble/session/hosts/docker/host.py",
        "ee61a1ec24e436f6fef0bc86511b4bfbfa91036687b7b6668107fcfbfc1bb84e",
    ): "Existing private prompt writer runs as the container agent; freeze its current program body.",
    (
        "tests/session/integration/installed_integration_test.py",
        "8a84d0e700c5ef374725eec9289eb2f33c67a387b7d29025c073c55bbedf6943",
    ): "Existing process fixture checks the copy reads its packaged config and runs a headless session.",
    (
        "tests/session/integration/clients/codex/tui/start_integration_test.py",
        "c8fb72314c0cf520ad5a14e8e027bb27d258fba56fabe1318331d8c7ee663c71",
    ): "Existing process fixture checks package imports keep sys path and share canonical modules.",
    (
        "tests/session/integration/clients/codex/tui/start_integration_test.py",
        "69680777b770082268cec7a6126b7a273da4e5d5babbf5109d7d1845300d7ea9",
    ): "Existing process fixture checks shared schema preserves hooks in isolated scripts and client.",
    (
        "tests/session/integration/clients/codex/tui/start_integration_test.py",
        "57fc6aac3dc49a226570cf6f0855e21aea62e766ca50d9db62743186f3e0e6f6",
    ): "Existing process fixture checks standalone account probe uses stdlib.",
    (
        "tests/session/integration/modes/tui_hook_integration_test.py",
        "1a5f0fb5b44d358c0c9cca725a5f3b3e8df06645090d413a073d7fa0d2164754",
    ): "Existing hook subprocess fixture drives journal replay, completion or configuration through the hook CLI.",
    (
        "tests/session/integration/modes/tui_hook_integration_test.py",
        "98e6019d48021ff1a528c2a5346f52266e340c36ca9f52ac983fbb9770ce070d",
    ): "Existing hook subprocess fixture drives journal replay, completion or configuration through the hook CLI.",
    (
        "tests/session/integration/modes/tui_hook_integration_test.py",
        "6138ed0fff57048f56a487d4ce38a6a8bb472e3b5e34dae0a41d8027b9da7bc7",
    ): "Existing hook subprocess fixture drives journal replay, completion or configuration through the hook CLI.",
    (
        "tests/session/integration/modes/tui_hook_integration_test.py",
        "65efdb35b0c074fec52bfac23d7723db7011deaa77ed1d2196b97da4fe6ac7e0",
    ): "Existing hook subprocess fixture drives journal replay, completion or configuration through the hook CLI.",
    (
        "tests/session/integration/modes/tui_hook_integration_test.py",
        "c787a4d85b5693fea07e23f2e0344e024ca5a289f1dde761144e3621b48c8950",
    ): "Existing process fixture checks two synchronized writers keep every complete journal record.",
}


EMBEDDED_PROGRAM_ARGV_RETURNS = {"ensemble.session.hosts.docker.daemon.docker_call": 0}


# (path under SESSION_SRC, word): a role name the session uses as a plain word (NFR-5)
ROLE_WORDS_ALLOWED = {
    ("clients/claude/tui.py", "pm"): "the AM/PM half of a reset time",
    ("clients/claude/usage.py", "pm"): "the AM/PM half of a reset time",
}
