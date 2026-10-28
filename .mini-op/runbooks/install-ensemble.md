---
when: installing, rerunning, upgrading or testing ensemble's install.sh on the mini, then checking an OpenCode worker through Strata
---

# Install ensemble on the mini

Rules, waiting and answering: mini-op's rules (the `mini-op` skill loads them). Accounts, keys and the GitHub App: `test-accounts.md`. Gotchas for this environment: `gotchas.md` beside this runbook.

Names and addresses (accounts, keychain items, the GitHub user, the Strata settings) come from the request or the mini's `~/.ensemble/config.local.toml`; never write them into this repo. Before putting one into an ssh command line, check it matches `^[A-Za-z0-9][A-Za-z0-9._@+:-]*$`; if not, stop and ask the user.

## 6. Then

1. Fill the mini's `~/.ensemble/config.local.toml` from the template `~/mini-op-projects/ensemble/install-ensemble/test-board.toml` (`install-ensemble/test-board.toml` beside this runbook), each app's Client ID from the test workspace; the install fills the ids (`test-accounts.md` 4., docs/upgrading.md › Linear presets).
2. Install script tests (TASK-812), in a real Terminal window on the mini so the user watches every prompt:
   1. Trim the Dock to Terminal, Chrome, iTerm and System Settings (`defaults write com.apple.dock persistent-apps …`, `killall Dock`).
   2. The Strata key into the mini's keychain; the Terminal runs in the logged-in session, whose keychain is unlocked. When `/run/ensemble/secrets/env/strata-key` exists, pipe it in with the mini's password, the item's service and account from the request or the mini's local config (each checked as above):

      ```sh
      { cat /run/ensemble/secrets/env/mini-password; printf '\n'; cat /run/ensemble/secrets/env/strata-key; } | ssh mini '~/mini-op-projects/ensemble/install-ensemble/storekey-stdin <service> <account>'
      ```

      It prints `storekey-stdin: <service>/<account> stored`; the key and the password reach no argv, env or output. Without that file, ask the user to pipe the key from their Mac's keychain into the mini's.
   3. Strata's OpenCode settings: `~/.mini-op-local/ensemble/strata.conf` on the mini, made once from `~/mini-op-projects/ensemble/install-ensemble/strata.conf.example` with the URL, model and context from the request (outside the deployed runbook dir, so a later run never overwrites it). Open a Terminal window on the mini and enlarge it, then start it: `printf '~/mini-op-projects/ensemble/install-ensemble/install-test' | mini run 60`. `install-test` runs PR #547's `install.sh` from `windroc-labs/ensemble` with `--clients claude,opencode --docker`, Strata as the OpenCode provider (`OC=1`), its sudo password typed at its prompt.
   4. How waiting works: mini-op's rules › Waiting and answering.
   5. Answer at once when a wait times out at a prompt (mini-op's rules › Waiting and answering). install.sh's prompts:
      - Docker Desktop first run: unattended when install.sh writes its presets (license by Docker's own install command; Rosetta off and onboarding done in its Group Container; WDRC-177); if it printed `Docker Desktop presets not written`, macOS denied the Group Container and the Welcome to Docker sign-in window shows. Only if a dialog shows: Rosetta prompt (process `com.apple.Virtualization.LinuxRosetta`) › Not Now, then Disable Rosetta (decided: no Rosetta); Subscription Agreement › Accept; Welcome to Docker › Skip; check `docker info`, then Enter in the Terminal.
      - `gh` logs in as the test GitHub user:

        ```mermaid
        flowchart LR
          A[Terminal Enter] --> B[Continue and code]
          B --> C[Focus and Authorize github]
          C --> D{sudo mode?}
          D -->|yes| E[Password and Confirm]
          D -->|no| F[Close extra gh tabs]
          E --> F
        ```

        At `Press Enter to open https://github.com/login/device in your browser...`, let `gh` open Chrome itself. Bind to the existing tab with `o bin --session <name>` (e.g. `gh`); do not use `o open` to make another window. Continue picks the account; type the code one character per box (`#user-code-0..3`, `#user-code-5..8`; one paste fills only the first box). After Continue with the code filled, Authorize github needs real page focus: real-click blank space until `document.hasFocus()` is true, move the real pointer onto the button, wait about 1 s, then real-click. For sudo mode (`#sudo_password`), unlock the mini's keychain in the same ssh session (mini-op's rules › Gotchas), then `source ~/apm/lib/keychain.zsh; keychain_read <github-password-service> | ~/apm/bin/secfield <session> '#sudo_password'`; real-click Confirm. Close any extra tabs `gh` opened when done.
      - The user does the Claude browser login (Claude Code login, then `claude setup-token` for the broker); paste the setup token into the broker prompt from the Terminal text on the mini, never reading it back.
      - broker GitHub: the App's pem path and ID (`test-accounts.md` 3b.3); a fresh install asks nothing more (2026-10-23). An upgrade may still ask the login password and raise the keychain dialog (6.2.11).
      - broker OpenCode: at `opencode auth login` type `Other`, then `strata`; at the hidden key prompt, unlock the keychain in the same ssh session, then `source ~/apm/lib/keychain.zsh; keychain_read <strata-service> <strata-account> | ~/apm/bin/ans 120 --to <id>` (the key stays on the mini).
   6. A rerun from the same Terminal: `eval "$(/opt/homebrew/bin/brew shellenv)"` first (the shell predates the `.zprofile` line); finished steps print `skip`.
   7. Send each install.sh defect to the TASK-812 engineer as it shows up.
   8. Cleanup: `clean-ensemble.md`.
   9. OpenCode worker check, from the mini's Terminal (an ssh shell's keychain is locked): `cd ~/.ensemble/adhoc/oc-test && ensemble-crew run --manager mini-test oc-test -- ensemble-session start oc-test --client opencode --display none --prompt "Reply with exactly: ok from opencode"`. The first run builds the worker image (~80 s) and macOS asks to let Docker reach the local network: Allow. Show it to the user in a Terminal window with the printed attach command, its `-t '=oc-test'` quoted.
      `ensemble-session close <name>` removes the worker's container but keeps its workspace volume `ensemble-ws-<name>` and its state dir `~/.ensemble/docker-state/<name>`. Close's reclaim removes the volume only once its cwd is gone and it holds no unpushed work (a kept one is warned about in `~/.ensemble/docker-state/reclaim.log`). `ensemble-session resume <name>` keeps both; a new start of the same name reuses the volume at the same cwd (at another cwd it removes it, refusing while it holds unpushed work) and empties the client home in the state dir. To check again, use a fresh worker name (`oc-test-2`, …), never delete the state dir by hand. Cleanup, from the mini's Terminal: `docker volume ls --filter label=ensemble.kind` lists the volumes; `/usr/local/libexec/ensemble/ensemble-broker workspace-rm --volume ensemble-ws-<name>` removes one (exit 12 `workspace_dirty`: unpushed work; `--force` skips that check; src/broker/internal/launcher/workspace.go).
   10. Runs: fresh install, rerun (all skipped), cleanup, fresh install; then an OpenCode worker answers through Strata.
   11. Upgrade in place (2026-10-21, 40c06c4 → 664ca15, OpenCode-only + Docker + local board): rerun with the first run's choices, `CLIENTS=opencode OC=1 ~/mini-op-projects/ensemble/install-ensemble/install-test -- --docker --board local`, in a new Terminal window (`do script`). Pass `--board` again: the saved choice (`~/.ensemble/install/state` `board_tracker`) wins over `config.local.toml`. It keeps the config, the board and an unloaded router job (a plist already in place is skipped, never bootstrapped). It refreshes OpenCode's ensemble copy (the `~/.ensemble/bin` commands run it) and, when the broker sources changed, rebuilds the broker (`mini pw` at sudo) and asks its logins again: `login github ensemble-broker-bot` asks the login password on the Terminal (`mini pw`); a SecurityAgent change-access dialog **may or may not appear**. It appeared during this in-place upgrade on 2026-10-21 and the next on 2026-10-22, but not during another fresh login on 2026-10-21. If it appears, the user types the login password into it and clicks Allow (mini-op: `mini pw --sa` does both); if not, continue when the Terminal reports success, without waiting for or trying to summon a dialog. `login opencode strata` asks the Strata key at a hidden prompt; the user pastes it. Auto mode blocks the manager from typing a secret anywhere but `mini pw`'s Terminal route. A hidden prompt's line does not change after the answer, so wait for the next step's line (`API key saved`, `==> ctmux`), not for the prompt to go away. The first role run afterwards rebuilds the worker image (~35 s).
       Upgrade to ef0dd8813 (2026-10-22, `CLIENTS=opencode OC=1 ~/mini-op-projects/ensemble/install-ensemble/install-test -- --docker --board local --no-launchd`), differences: `==> OpenCode provider` asks `install.sh will write to ~/.config/opencode/opencode.jsonc: provider: strata (URL, model, context; credentials stay in auth.json) Write it? [y/N]` (default N): y; the config before it is kept at `~/apm-tests/opencode.jsonc.before-847`. No Strata key prompt: it prints `skip: strata in …/auth.json`. The plugin step finds OpenCode's copy at another commit and downloads it again (about as long as the first time). The next upgrade, past TASK-858 (PR #591), asks to add `~/.ensemble/opencode/bin` to the shell profile: y. Past TASK-862 (PR #592, broker protocol 19) the broker must be reinstalled.
