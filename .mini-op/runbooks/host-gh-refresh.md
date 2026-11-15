---
when: checking on the mini that a running gh watch survives the host gh wrapper's token refresh, after a change to src/ensemble/utils/host_gh.py or the broker's host-gh-login, and before raising its VERIFIED_GH
---

# Check that a gh watch survives a host token refresh

Rules, waiting and answering: mini-op's rules (the `mini-op` skill loads them). Installing and upgrading: `install-ensemble.md`. Gotchas for this environment: `gotchas.md` beside this runbook.

The wrapper relies on gh rereading a keyring token on every request, which gh does not document. Only a real Keychain shows it, so this runs only here, never in a test suite or on a developer's Mac (`AGENTS.md`). Tokens stay in shell variables: never print one or put it in argv.

```sh
fp() { printf '%s' "$1" | shasum -a 256 | cut -c1-12; }
stamp=~/.ensemble/host-gh/refreshed
```

1. Needs on the mini: ensemble installed with `--docker` at `<ref>`, the branch or commit of windroc-labs/ensemble under test, matching `^[A-Za-z0-9][A-Za-z0-9._/-]*$` (otherwise stop and ask the user). Also: a second GitHub App of the test user, set up per the README's Host GitHub App steps 2 to 4 on the test repo's owner, and `gh --version` printing `gh version 2.102.0`, the `VERIFIED_GH` of `src/ensemble/utils/host_gh.py` (raising it means rerunning this check with the new gh). The test repo has a `workflow_dispatch` workflow whose one job runs `sleep 360` (add `.github/workflows/sleep.yml` once if missing).
2. Switch on: `mkdir -m 700 ~/.ensemble/host-gh`, then `gh api rate_limit --jq .resources.core.limit`. Observe: `command -v gh` is `~/.ensemble/bin/gh`; `gh api repos/<test-owner>/<test-repo> --jq .permissions` has every flag false; `gh api graphql -f 'query={viewer{login}}' --jq .data.viewer.login` ends in `[bot]`; `gh api installation/repositories --jq '.repositories[].full_name'` lists the test repo. Any other answer fails the check: report it, since the router's and repo.py's push checks rely on all three.
3. Start a run and watch it in a second Terminal tab: `gh workflow run sleep.yml -R <test-owner>/<test-repo>`, take its id from `gh run list -R <test-owner>/<test-repo> --workflow sleep.yml --limit 1 --json databaseId --jq '.[0].databaseId'`, then `gh run watch <id> -R <test-owner>/<test-repo> --exit-status 2>&1 | tee ~/.mini-op-local/ensemble/watch.log`.
4. In the first tab, while it watches: `old=$(gh auth token)`, `fp "$old"`. Force a refresh: `touch -t "$(date -v-51M +%Y%m%d%H%M.%S)" "$stamp"`. Within 90 s, `stat -f %Sm "$stamp"` shows a time in the last 2 minutes (the new token's issue time) and `fp "$(gh auth token)"` differs from the old fingerprint.
5. Revoke the old token, with it on stdin only: `printf 'header = "Authorization: Bearer %s"\n' "$old" | curl -sS -o /dev/null -w '%{http_code}\n' -X DELETE --config - https://api.github.com/installation/token` prints `204`. Then `unset old`.
6. Pass: the watch keeps printing updates after step 5 and ends with exit 0 when the run succeeds, and `grep -cE '401|Bad credentials' ~/.mini-op-local/ensemble/watch.log` is 0. Report the gh version, both fingerprints' difference (not their values), the step 2 answers and the result.
7. Clean up: `rm -f ~/.mini-op-local/ensemble/watch.log`; leave `~/.ensemble/host-gh` only if the request asks to keep the host App on.
