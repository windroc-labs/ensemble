---
when: creating or repairing ensemble's test accounts on the mini (Outlook, GitHub and its App, the Linear workspace, role accounts, API keys), or restoring their keychain items after a mini reset
---

# Ensemble's test accounts

How the manager builds ensemble's live-test accounts on the Mac mini: its own email, a GitHub account and App, its own Linear workspace with role accounts, and their keys in the mini's keychain. Machine access, the browser and reconnecting mini-op to a reset mini: mini-op's `setup-test-env` runbook. Rules, driving the mini: mini-op's rules (`/mini-op:run` loads them). Gotchas for this environment: `gotchas.md` beside this runbook.

Names, addresses and ids (accounts, keychain items, the workspace and its team, the GitHub user, App and repo) come from the request or the mini's `~/.ensemble/config.local.toml` (template: `install-ensemble/test-board.toml`); never write them into this repo. Before putting a name into an ssh command line, check it matches `^[A-Za-z0-9][A-Za-z0-9._@+:-]*$` (an email: `^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+$`); if not, stop and ask the user. This runbook's scripts run from `~/mini-op-projects/ensemble/test-accounts/`; the Linear logins use mini-op's `~/apm/bin/linearlogin` and `~/apm/bin/maillink`.

## 3. Email (Outlook)

1. On the mini, generate the password into its keychain (`openssl rand` + `security -i add-generic-password`).
2. Plain Chrome window, OpenCLI unbound, keystrokes only: `signup.live.com`, a short address (long ones come back "isn't available": `gotchas.md`), password typed from the mini keychain, birth date over 18, a name, marketing unticked.
3. The user passes the press-and-hold check.
4. At "Help protect your account", open `outlook.live.com/mail/` directly. Its "Keep access … recovery method" dialog: Not now. To stop it for good, add a recovery code (account.microsoft.com/security › Advanced security options) into the mini keychain.

## 3b. GitHub account

Do this before the install test: `install.sh` logs `gh` in.

The test GitHub account and its App are the manager's to manage, deletions included, e.g. an App private key no longer used (the user, 2026-10-23).

Pre-rename names: the test resources made before the ensemble rename (the GitHub App and its repo, the Linear workspace, its team and its project) keep their old names, since renaming them changes their URLs and ids.

1. Proton (its free-plan signup page), plain window: username, password generated into the mini keychain, decline the upsell, Human Verification › Email tab › the Outlook address, code from the Outlook mail; Show the recovery phrase, select and copy it into the mini keychain, tick and Continue.
2. GitHub (`github.com/signup`) with OpenCLI: email the Proton address (GitHub refuses Outlook: `gotchas.md`), password typed from the keychain, username; Create account; the 8-digit launch code comes in the Proton mail (inside its message iframe: read it with `eval` over `iframe.contentDocument`); then sign in.

3. GitHub App for the broker (manifest flow, all on the mini as the test GitHub user):
   1. `gh repo create <test-owner>/<test-repo> --private --add-readme` (keychain unlocked first).
   2. On any github.com page, `o eval` a form POST to `https://github.com/settings/apps/new` with field `manifest` = JSON: name `<app-name>`, `hook_attributes.active: false`, `redirect_url` `https://example.com/manifest`, `public: false`, permissions contents and pull_requests write, checks, actions and metadata read. Click "Create GitHub App for <test-owner>".
   3. Read `code` from the redirected URL; `gh api -X POST app-manifests/<code>/conversions` returns the App ID and `pem`: write the pem to `/tmp/apm-app.pem` (umask 077) with no output.
      For an existing App, get a new private key at Credentials › Key pairs (delete the older key there once the broker holds the new one) (`https://github.com/settings/apps/<app-name>/key_pairs`) › New key. Chrome downloads it into `~/Downloads`. Terminal has no Downloads permission: in an ssh session, run `umask 077; mv ~/Downloads/<downloaded-key>.pem /tmp/apm-app.pem; chmod 600 /tmp/apm-app.pem`, using the downloaded filename; never print its contents. Import it at the broker prompt (5. below; `install-ensemble.md` 6.2.5), then delete `/tmp/apm-app.pem`.
   4. `https://github.com/apps/<app-name>/installations/new`: Only select repositories › `<test-repo>` › Install.
   5. At install.sh's broker prompt give `/tmp/apm-app.pem` and the App ID; type the login password at the prompt. If the keychain change-access dialog appears, have the user enter it there too and click Allow; otherwise continue on Terminal success (`install-ensemble.md` 6.2.11). Delete the pem after.
   The current App's name, ID, installation id and repo: the request or the mini's local config.

## 4. Linear workspace

1. `linear.app/signup` › Continue with email › the address; `~/apm/bin/maillink mark|follow <email>` around the request opens the emailed link.
2. Create the workspace (name and URL by keystrokes), skip the onboarding steps, turn off onboarding emails.
3. Create the manager's key as in "API keys" below; store it under the manager key's item (`[orchestrator.linear] manager_key` and its account: the mini's local config).
4. Through `~/mini-op-projects/ensemble/test-accounts/lq <manager-key-service> <manager-key-account> <body-file>` (body files built on the user's Mac, scp'd to `/tmp`): states In Review and Handoff (type `started`); label groups `Agent` (Claude, Codex, OpenCode) and `Tasks` (Build, Light Build, Light Research, Deep Research); trash the onboarding issues. Record every id in the mini's `~/.ensemble/config.local.toml` (template: `install-ensemble/test-board.toml`).

Board extras, with the same `lq` call (manager key): a project (`projectCreate`, the test team), mapped in `[orchestrator.linear.project_repos]` to `<test-owner>/<test-repo>`; view **Dashboard**, a copy of the real workspace's: `customViewCreate` (name Dashboard, the test team, shared, filter on the team's key), then `viewPreferencesCreate` (type organization, viewType customView, preferences layout board, issueGrouping workflowState, issueSubGrouping project): columns by status, rows by project.

## 5. Role accounts

1. Invite each role account (harness, researcher, pm, engineer; addresses: `[orchestrator.linear.roles.<role>] account` in the mini's local config): `organizationInviteCreate(role: admin, teamIds: [team])`. The free plan invites admins only.
2. Per role, one at a time: `ssh mini '~/apm/bin/linearlogin --code <email>'` in the background (logs out, requests the login mail, enters its code, prints where it lands). Then by hand with OpenCLI: Join, the onboarding steps (profile name `Agent <role>`, Skip, Finish), then the key.

## API keys

Minimum permissions, from `docs/credentials.md`; team access only the test team:

| Account | Permissions |
|---|---|
| manager, harness | Read, Write (Linear then forces Create issues and Create comments on) |
| researcher, pm, engineer | Read, Write (the same: Linear does not allow Write without Create issues) |

1. Settings › Security & access › Personal API keys: revoke any older key of the account.
2. New API key: name `ensemble-<account>`, Only select permissions… (check `#scope-read`, `#scope-write`), Only select teams… › the test team, Create, Copy to clipboard.
3. From mini-op, `ssh mini '~/mini-op-projects/ensemble/test-accounts/storekey <service> <account>' < /run/ensemble/secrets/env/mini-password` stores it, clears the clipboard and prints `viewer`.

## Restore the keychain items after a reset

After mini-op's `setup-test-env` runbook has reconnected mini-op to the reset mini (its Reconnect steps). The export of the old login keychain is on the user's Mac in `~/.mini-op/keychain-backup/`, unlocked by the same password as the account; it never enters the container.

1. The user, on their Mac: copy it into a new private dir on the mini, and give mini-op the printed dir:

   ```sh
   d=$(ssh -F ~/.mini-op/ssh_config mini 'mktemp -d') && ssh -F ~/.mini-op/ssh_config mini "cat > $d/export.keychain-db" < ~/.mini-op/keychain-backup/<export-file> && echo "$d"
   ```

2. In mini-op, with `d` set to that dir: list its items, attributes only (never `dump-keychain -d`):

    ```sh
    ssh mini "security dump-keychain $d/export.keychain-db" | grep -E 'class:|"(svce|acct)"<blob>='
    ```

3. Import each generic password (class `genp`) by its service (`svce`) and account (`acct`). Each name must match `^[A-Za-z0-9][A-Za-z0-9._@+:-]*$`, else stop and ask the user; the user says how many items to expect (12 at the last export), any other count: tell the user which and ask before importing. The export predates the ensemble rename: import a service with the old prefix under the new prefix, and the old shared account under the new one (both pairs: the request or the mini's local config); keep other names.

    ```sh
    ssh mini "~/mini-op-projects/ensemble/test-accounts/importkey $d/export.keychain-db <service> <account> [<new-service> [<new-account>]]" < /run/ensemble/secrets/env/mini-password
    ```

    It prints `importkey: <service>/<account> imported`, or `… imported as <new-service>/<new-account>`. An internet password (class `inet`, e.g. git's `github.com`) is not imported: the `gh` login (below) makes it again.
4. Remove the copy: `ssh mini "rm -rf $d"`.

## Log `gh` in again after a reset

Once installed (`install.sh` logs it in: `install-ensemble.md`), log `gh` in again as the test GitHub user (password: its keychain item, named in the request or the mini's local config).
