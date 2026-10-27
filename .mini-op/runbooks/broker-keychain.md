---
when: checking the broker's no-UI keychain contract (a foreign item read, replaced and rewritten without a dialog) on the mini, after a change to src/broker/internal/broker/keychain_darwin.go
---

# Check the broker's no-UI keychain contract on the mini

Rules, waiting and answering: mini-op's rules (the `mini-op` skill loads them). Gotchas for this environment: `gotchas.md` beside this runbook.

The check needs a real Keychain, so it runs only here, never in a test suite or on a developer's Mac (`docs/tests.md`). It is `TestNoUIAgainstForeignItem` (`broker-keychain/keychain_noui_darwin_test.go` beside this runbook): an item `/usr/bin/security` owns, in a temporary keychain, is read (`Secret`, `AppKey`), replaced (`PutSecret`) and rewritten (`WriteSecret`, `WriteCodex`) without UI, each call failing within 10 s and the item unchanged; `Accounts` and `Services` list it.

1. Needs on the mini: Go (the version in `src/broker/go.mod`) and the Xcode command line tools; otherwise stop and tell the user. `<ref>`: a branch or commit of windroc-labs/ensemble the user named, never a fork PR's ref (it runs that ref's whole broker test package in the logged-in session); check it matches `^[A-Za-z0-9][A-Za-z0-9._/-]*$`, otherwise stop and ask the user.
2. Run it in the mini's Terminal, the logged-in session where a dialog would show: `printf '%s\n' 'REF=<ref> ~/mini-op-projects/ensemble/broker-keychain/noui-test > /tmp/noui-test.log 2>&1' | mini run 300`, then read `/tmp/noui-test.log` over ssh. `noui-test` fetches `<ref>` into a scratch clone, copies that ref's `keychain_noui_darwin_test.go` into `src/broker/internal/broker/` and runs it against a temporary keychain; the login keychain and its items are never touched.
3. Observe: `--- PASS: TestNoUIAgainstForeignItem` and the last line `noui-test: go test exited 0, search list unchanged`, with no keychain dialog on screen. A dialog fails the check: never answer it with a password (no `mini pw --sa`), click Deny or leave it, and report to the user. So do a "did not return within 10 s" error, any `FAIL` line and `CHANGED`.
4. Clean up: on exit, even after Ctrl-C or a `mini run` timeout, `noui-test` restores the search list from `~/.mini-op-local/ensemble/search-list.before`, removes its scratch clone (with the temporary keychain) and then that file. Then `rm -f /tmp/noui-test.log`. If it was killed outright, so the file is left: when it is empty, stop and ask the user; else `security list-keychains -d user -s <each path in it>`, then `rm -rf ~/.mini-op-local/ensemble/noui-test.*` and `rm ~/.mini-op-local/ensemble/search-list.before`.
