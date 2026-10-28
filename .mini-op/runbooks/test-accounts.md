---
when: creating or repairing ensemble's test accounts on the mini (Outlook, GitHub and its App, the Linear workspace and its OAuth apps), or restoring their keychain items after a mini reset
---

# Ensemble's test accounts

How the manager builds ensemble's live-test accounts on the Mac mini: its own email, a GitHub account and App, its own Linear workspace with an OAuth app each for the harness, the manager and every role, and their client secrets and tokens in the mini's keychain. Machine access, the browser and reconnecting mini-op to a reset mini: mini-op's `setup-test-env` runbook. Rules, driving the mini: mini-op's rules (the `mini-op` skill loads them). Gotchas for this environment: `gotchas.md` beside this runbook.

Names, addresses and ids (accounts, keychain items, the workspace and its team, the GitHub user, App and repo) come from the request or the mini's `~/.ensemble/config.local.toml` (template: `install-ensemble/test-board.toml`); never write them into this repo. Before putting a name into an ssh command line, check it matches `^[A-Za-z0-9][A-Za-z0-9._@+:-]*$` (an email: `^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+$`); if not, stop and ask the user. This runbook's scripts run from `~/mini-op-projects/ensemble/test-accounts/`; the Linear logins use mini-op's `~/apm/bin/linearlogin` and `~/apm/bin/maillink`.

Renamed on 2026-10-26 (the user's request): the test resources' agent-pm names became ensemble names: the keychain items' prefix, the Linear workspace (name and URL) and its project, the GitHub repo and App (same App ID; the Linear team keeps its key). Run records from before then use the old names. The test accounts' personal Linear API keys were revoked and their keychain items deleted then: the OAuth apps' `linear-app-*` items are the Linear credentials.

## 3. Email (Outlook)

1. On the mini, generate the password into its keychain (`openssl rand` + `security -i add-generic-password`).
2. Plain Chrome window, OpenCLI unbound, keystrokes only: `signup.live.com`, a short address (long ones come back "isn't available": `gotchas.md`), password typed from the mini keychain, birth date over 18, a name, marketing unticked.
3. The user passes the press-and-hold check.
4. At "Help protect your account", open `outlook.live.com/mail/` directly. Its "Keep access … recovery method" dialog: Not now. To stop it for good, add a recovery code (account.microsoft.com/security › Advanced security options) into the mini keychain.

## 3b. GitHub account

Do this before the install test: `install.sh` logs `gh` in.

The test GitHub account and its App are the manager's to manage, deletions included, e.g. an App private key no longer used (the user, 2026-10-23).

1. Proton (its free-plan signup page), plain window: username, password generated into the mini keychain, decline the upsell, Human Verification › Email tab › the Outlook address, code from the Outlook mail; Show the recovery phrase, select and copy it into the mini keychain, tick and Continue.
2. GitHub (`github.com/signup`) with OpenCLI: email the Proton address (GitHub refuses Outlook: `gotchas.md`), password typed from the keychain, username; Create account; the 8-digit launch code comes in the Proton mail (inside its message iframe: read it with `eval` over `iframe.contentDocument`); then sign in.

3. GitHub App for the broker (manifest flow, all on the mini as the test GitHub user):
   1. `gh repo create <test-owner>/<test-repo> --private --add-readme` (keychain unlocked first).
   2. On any github.com page, `o eval` a form POST to `https://github.com/settings/apps/new` with field `manifest` = JSON: name `<app-name>`, `hook_attributes.active: false`, `redirect_url` `https://example.com/manifest`, `public: false`, `default_permissions` exactly ensemble's `[hosts.docker.github] permissions` in `src/ensemble/session/config.toml` (now contents, pull_requests and workflows write; checks, actions and statuses read; GitHub adds metadata read itself). Read that line before each new App: a permission ensemble requests and the App lacks fails every token with 422 "permissions requested are not granted". Click "Create GitHub App for <test-owner>".
   3. Read `code` from the redirected URL; `gh api -X POST app-manifests/<code>/conversions` returns the App ID and `pem`: write the pem to `/tmp/apm-app.pem` (umask 077) with no output.
      For an existing App, get a new private key at Credentials › Key pairs (delete the older key there once the broker holds the new one) (`https://github.com/settings/apps/<app-name>/key_pairs`) › New key. Chrome downloads it into `~/Downloads`. Terminal has no Downloads permission: in an ssh session, run `umask 077; mv ~/Downloads/<downloaded-key>.pem /tmp/apm-app.pem; chmod 600 /tmp/apm-app.pem`, using the downloaded filename; never print its contents. Import it at the broker prompt (5. below; `install-ensemble.md` 6.2.5), then delete `/tmp/apm-app.pem`.
   4. `https://github.com/apps/<app-name>/installations/new`: Only select repositories › `<test-repo>` › Install.
   5. At install.sh's broker prompt give `/tmp/apm-app.pem` and the App ID. Handle its password and access prompts through [macOS password prompts](gotchas.md#macos-password-prompts-during-installs-and-logins). Delete the pem after.
   The current App's name, ID, installation id and repo: the request or the mini's local config.

## 4. Linear workspace

1. `linear.app/signup` › Continue with email › the address; `~/apm/bin/maillink mark|follow <email>` around the request opens the emailed link.
2. Create the workspace (name and URL by keystrokes), skip the onboarding steps, turn off onboarding emails.
3. Create the five OAuth apps as in "5. OAuth apps" below.
4. Run the install with `--board linear` (`install-ensemble.md`). Its Linear apps step stores each app's client secret and first token in the mini's keychain; its board configuration step then stops, as the team lacks the In Review and Handoff states and the Tasks label group. What the apps step stored stays.
5. As the workspace admin (a team admin), in Linear's web UI on the mini, mini-op driving the browser: in the test team's settings, Workflow: states In Review and Handoff under Started; Labels: label groups `Agent` (Claude, Codex, OpenCode) and `Tasks` (Build, Light Build, Light Research, Deep Research). Trash the onboarding issues. Linear refuses both creations to the manager app's token (`workflowStateCreate`, `issueLabelCreate`: `FORBIDDEN`, live test 2026-10-26), and the test accounts have no personal API key.
6. Board extras, through `~/mini-op-projects/ensemble/test-accounts/lq <manager-token-service> <manager-token-service> <body-file>` (the manager app's token item: `[orchestrator.linear.manager] token_service` in the mini's local config, default `linear-app-manager-token`; body files built on the user's Mac, scp'd to `/tmp`; a Linear error exits 1): a project (`projectCreate`, the test team); view **Dashboard**, a copy of the real workspace's: `customViewCreate` (name Dashboard, the test team, shared, filter on the team's key), then `viewPreferencesCreate` (type organization, viewType customView, preferences layout board, issueGrouping workflowState, issueSubGrouping project): columns by status, rows by project.
7. Run the install again: its board configuration fills the team, state and label ids still missing from the mini's local config, and asks each Linear project's GitHub repo: the project's is `<test-owner>/<test-repo>`.

## 5. OAuth apps

1. Create the five apps (harness, manager, researcher, pm, engineer) as docs/upgrading.md › Linear presets says: Client credentials on, Public and Webhooks off.
2. Put each Client ID in the mini's `~/.ensemble/config.local.toml` (template: `install-ensemble/test-board.toml`). The install asks each app's client secret, stores it and the app's first token in the mini's keychain and fills each app's `user_id`. Handle the subsequent password and access prompts through [macOS password prompts](gotchas.md#macos-password-prompts-during-installs-and-logins).

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

3. Import each generic password (class `genp`) by its service (`svce`) and account (`acct`). Each name must match `^[A-Za-z0-9][A-Za-z0-9._@+:-]*$`, else stop and ask the user; the user says how many items to expect (12 at the last export), any other count: tell the user which and ask before importing. An export from before 2026-10-26 holds the agent-pm names: import a service with the old prefix under the new prefix, and the old shared account under the new one (both pairs: the request or the mini's local config); keep other names. Import none of its personal Linear API key items (services ending in `-linear-key` or `-linear-<role>` after the old prefix): those keys are revoked.

    ```sh
    ssh mini "~/mini-op-projects/ensemble/test-accounts/importkey $d/export.keychain-db <service> <account> [<new-service> [<new-account>]]" < /run/ensemble/secrets/env/mini-password
    ```

    It prints `importkey: <service>/<account> imported`, or `… imported as <new-service>/<new-account>`. An internet password (class `inet`, e.g. git's `github.com`) is not imported: the `gh` login (below) makes it again.
4. Remove the copy: `ssh mini "rm -rf $d"`.

## Log `gh` in again after a reset

Once installed (`install.sh` logs it in: `install-ensemble.md`), log `gh` in again as the test GitHub user (password: its keychain item, named in the request or the mini's local config).
