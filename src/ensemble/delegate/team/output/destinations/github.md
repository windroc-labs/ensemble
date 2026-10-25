## Destination

Publish the deliverable to `<out-repo>`, branch `<out-branch>`, folder `<out-dir>`. `<pub>` is `<Workdir>/publish`.

1. `<pub>` missing → `gh repo clone <out-host>/<out-repo> <pub> -- --branch <out-branch>`; else `git -C <pub> fetch origin` and `git -C <pub> rebase --autostash origin/<out-branch>`, after `git -C <pub> rebase --abort` if `git -C <pub> status` shows a rebase in progress.
2. **File**: reuse `<out-dir>*-<Reference>-*.md`, else create `<out-dir><date +%Y-%m-%d-%H%M>-<Reference>-<kebab-slug>.md`. No id → always create.
3. `git -C <pub> add <file>`, `git -C <pub> commit -F <Workdir>/tmp/msg -- <file>`, `<Workdir>/tmp/msg` holding `<Add|Update> <Reference>: <title>`; then `git -C <pub> fetch origin`, `git -C <pub> rebase origin/<out-branch>` and `git -C <pub> push origin HEAD:<out-branch>`, repeating those three on rejection.
4. Set the outcome's `url` to `https://<out-host>/<out-repo>/blob/<out-branch>/<file>`, `<file>` URL-encoded (`/` kept, a space → `%20`).

**Published**: `git -C <pub> status --porcelain -- <file>` and, after a fetch, `git -C <pub> log origin/<out-branch>..HEAD` print nothing.
