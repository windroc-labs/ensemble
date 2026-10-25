## Destination

Open or update the pull request for `<branch>` on `<owner>/<name>`; the deliverable is its description.

1. Write the description to `<Workdir>/tmp/pr.md`.
2. `git -C <worktree> push -u origin <branch>`. Denied → `status: failed`, `summary` `push not permitted`; stop.
3. By `repo.py status`'s `pr`: null → `gh pr create --draft --repo <host>/<owner>/<name> --head <branch> --base <base> --title <title> --body-file <Workdir>/tmp/pr.md`, `<title>` the outcome's `title`, shell-quoted; `OPEN` → `gh pr edit <pr.number> --repo <host>/<owner>/<name> --title <title> --body-file <Workdir>/tmp/pr.md`; closed or merged → `needs_input` asking whether to open a new PR; stop.
4. Set the outcome's `url` to the PR's URL. PR creation denied → set it to `https://<host>/<owner>/<name>/compare/<base>...<branch>?expand=1` and say in `summary` that the user must open the PR.
