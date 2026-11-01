---
when: checking on the mini which Claude config a tmux Host worker runs on (the user's own `~/.claude`, or a registered account through `local_account` or `--account`), after a change to session account selection
---

# Check the tmux Host's Claude account choice

Rules, waiting and answering: mini-op's rules (the `mini-op` skill loads them). Installing and upgrading: `install-ensemble.md`. Gotchas for this environment: `gotchas.md` beside this runbook.

This check needs real Claude logins, so it runs only here, never in a test suite or on a developer's Mac (`AGENTS.md`). It reads no Keychain item and logs nothing in or out. Account names and dirs come from the request or the mini's `~/.ensemble/config.local.toml`; `<name>` must match `^[a-z][a-z0-9._-]{0,63}$`, otherwise stop and ask the user.

1. Needs on the mini: ensemble installed at `<ref>`, the branch or commit of windroc-labs/ensemble under test (`install-ensemble.md`), matching `^[A-Za-z0-9][A-Za-z0-9._/-]*$`; otherwise stop and ask the user. `~/.claude` is logged in and has at least one user plugin enabled (e.g. updraft). One account is registered, `[session.clients.claude.accounts.<name>]` with a `dir` other than `~/.claude`, logged in with `CLAUDE_CONFIG_DIR=<dir> claude auth login`. `[session.clients.claude]` sets no `local_account`.
2. Default start. Run `ensemble-crew worker acct-default --client claude --prompt 'Run echo "${CLAUDE_CONFIG_DIR-unset}" and reply with its output only'`. Expect:
   - its stderr has no `claude account` line (no usage read, no pick);
   - `ensemble-session reply acct-default` is `unset` (a permission prompt for the `echo`: answer it per mini-op's rules);
   - in its pane, `/plugin` lists the plugin enabled in `~/.claude`.
3. Resume on the default. In `acct-default`, send `/exit`, then run `ensemble-session restart acct-default`. Expect no `claude account` line again, and the conversation continues: ask it what it replied before.
4. `local_account`. Set `local_account = "<name>"` under `[session.clients.claude]` in `~/.ensemble/config.local.toml`. Run `ensemble-crew worker acct-local --client claude --prompt 'Run echo "${CLAUDE_CONFIG_DIR-unset}" and reply with its output only'`. Expect its stderr to have a `claude account <account>: …` reading for each registered account, then `claude account <name>: picked`. Expect its reply to be `<name>`'s `dir`.
5. `--account` with no `local_account`. Remove the `local_account` line. Run step 4's command as `acct-flag`, adding `--account <name>`. Expect the same lines and reply as step 4.
6. Resume on an account. In `acct-flag`, send `/exit`, then run `ensemble-session restart acct-flag`. Expect `claude account <name>: picked` again, and the conversation continues.
7. Clean up: `ensemble-crew close acct-default`, `ensemble-crew close acct-local` and `ensemble-crew close acct-flag`. Confirm `~/.ensemble/config.local.toml` is as it was in step 1.

Record each step's result in the run record. A start with no account that prints a `claude account` line, or that replies with a dir, fails the check.
