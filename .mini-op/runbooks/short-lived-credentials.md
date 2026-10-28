---
when: checking on the mini that no short-lived credential reaches the Keychain (WDRC-315), after a change to the broker's GitHub or Codex token paths or the installer's retired-item cleanup
---

# Check that short-lived credentials stay off the Keychain

Rules, waiting and answering: mini-op's rules (the `mini-op` skill loads them). Installing and upgrading: `install-ensemble.md`. Gotchas for this environment: `gotchas.md` beside this runbook.

The check needs a real Keychain, so it runs only here, never in a test suite or on a developer's Mac (`AGENTS.md`). It covers WDRC-314 NFR-2 (the upgrade deletes the retired items with one password prompt, at retrust, and no dialog; start, maintain and close add none) and NFR-3 (no copy of a GitHub token on the host). Item names never appear here: set them as shell variables in the mini's Terminal and use only those.

```sh
p=ensemble-broker
retired="$p-tokens $p-revocations $p-codex-grants"
count() { for s in $retired; do printf '%s %s\n' "$s" "$(security dump-keychain | grep -c "\"svce\"<blob>=\"$s\"")"; done; }
items() { security dump-keychain | grep -o "\"svce\"<blob>=\"$p-[^\"]*\"" | sort; }
```

`security dump-keychain` without `-d` lists attributes only: it decrypts no item, so it shows no dialog and prints no secret.

1. Needs on the mini: ensemble installed with `--docker` at a release before WDRC-315 (broker protocol 30 to 32, e.g. `v0.6.1`), a GitHub App login (`test-accounts.md` 3b) and a Codex login (`login codex`), `[session.hosts.docker.github] app` set in `~/.ensemble/config.local.toml`. `<ref>`: the branch or commit of windroc-labs/ensemble under test, matching `^[A-Za-z0-9][A-Za-z0-9._/-]*$`; otherwise stop and ask the user.
2. Seed leftovers with the old broker, in the mini's Terminal: start a Codex session (`ensemble-session start wdrc315 --client codex --display none --prompt "Reply with exactly: ok"`), wait for its reply, then remove its container behind the broker's back: `docker rm -f "$(docker ps --filter label=ensemble.session=wdrc315 --format '{{.Names}}')"`. Run `count`: `$p-tokens` and `$p-codex-grants` show at least 1; `$p-revocations` shows 0 unless an old-broker revocation failed (it holds only unconfirmed revocations), which leaves its deletion untested: say so in the report. Save `items > ~/.mini-op-local/ensemble/items.before`.
3. Upgrade: run `scripts/install.sh --docker` at `<ref>` in a Terminal window (`install-ensemble.md` 6.2.11, `REF=<ref>`). Observe: the step `retired broker keychain items` prints `deleted <n> retired broker keychain items` with `<n>` the sum `count` showed, with no dialog; the login keychain password is asked once, at `broker re-trust`, and nowhere else. A dialog or a second password prompt fails the check. Then `count` shows 0 for each.
4. Run a session on the new broker: `ensemble-session close wdrc315`, then start it again as in 2. While it runs:
   1. Renew once: wait for the harness's first credential tick, or run `/usr/local/libexec/ensemble/ensemble-broker maintain --container <container> --manifest <its manifest under ~/.ensemble/docker-state/wdrc315/> --renew`; its result shows a `github` and a `codex` expiry.
   2. Read a GitHub token from the container into a variable, never printed and never in argv: `tok=$(docker exec "$C" sh -c 'f=$(find / -path "*/secrets/github/*" -type f ! -name ".*" 2>/dev/null | head -n 1); cat "$f"')` with `C` the container name; `[ -n "$tok" ] && echo read`.
   3. Search the host for it with the value on stdin as the pattern (NFR-3): `printf '%s\n' "$tok" | grep -rlFf - ~/.ensemble | wc -l` and `security dump-keychain | grep -cFf <(printf '%s\n' "$tok")`: both 0.
   4. `items` equals `~/.mini-op-local/ensemble/items.before` minus the retired services: no new item; long-lived logins (such as the Codex login, renewed in place) keep their one item each. With the attribute search in 3 this covers the Keychain without decrypting an item.
   5. `ensemble-session close wdrc315`; then `count` still shows 0 for each, and `items` is unchanged; `unset tok`.
5. Report each observation (counts, the deleted count, prompts and dialogs seen, the two search results). Never answer a dialog with a password (no `mini pw --sa`): click Deny or leave it, and report it.
6. Clean up: `rm -f ~/.mini-op-local/ensemble/items.before`; `ensemble-session close wdrc315` if it still runs.
