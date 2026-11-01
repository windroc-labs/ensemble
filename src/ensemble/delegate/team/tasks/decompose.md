# Decompose

Turn one handed-off PRD into child issues with `<team>/methods/to-tickets.md`. This task's method overrides take precedence. Publish with this task's commands and fields.

## Steps

1. **Read** the source PRD (`ensemble-orchestrator issue read <PRD> --full --output <Workdir>/tmp/prd.json`) and check out each target repo read-only ([Principles › Work › Worktree](#work)), branch `<id>-decompose`; fetch in full each document the input links but did not inline. Then report:
   [ensemble-progress:start] decompose, the source PRD
2. **Draft** with the method. Each description opens with `Repo:`, `Covers:` (PRD identifier + requirement identifiers, at most 8 requirement identifiers on an engineer ticket: split a larger one along an interface, e.g. read-only vs write) and `Task:`; engineer work then acceptance quoting, verbatim and in full, every PRD scenario tagged with a requirement in its `Covers:`. Verifier tickets list the ids of the `@ops` scenarios they run and link the PRD instead.
3. **Publish**: `ensemble-orchestrator issue create --title <title> --body-file <file> --parent <PRD> --state ready --role engineer|verifier --client claude --priority <the PRD's> --project <the repo's mapped project> --blocked-by <issue>`, adding `--task light-build` for a small engineer ticket. Outcome `title`: the PRD's product name; `summary`: `Created <identifiers>; reused none`.

## Method overrides

- **Step 1, Gather context.** Skip `/setup-matt-pocock-skills`. Task step 1 supplies the source PRD and repos; task step 3 supplies the board command and fields.
- **Step 2, Explore the codebase.** Exploration is mandatory. Grep the target repo for every symbol, option, command and file the PRD changes or removes, plus their callers and tests. Use the hits to split the tickets and set their blocking edges: every hit belongs to one ticket's work or is ruled out in a line.
- **Step 3, Draft vertical slices.** Add task step 2's fields and scenario acceptance to the method's issue template.
- **Step 4, Quiz the user.** Review the breakdown yourself as a numbered list with each ticket's title, blockers and end-to-end behaviour. Judge whether the granularity is too coarse or too fine, whether each dependency genuinely gates its ticket, and whether tickets should be merged or split further. Revise until these checks pass, then publish. Do not ask the user or wait for approval.
- **Step 5, Publish.** Publish only with task step 3, in dependency order (blockers first), using its native blocking and sub-issue relationships. `--state ready` replaces the `ready-for-agent` label. Do not publish local ticket files. Stop after publishing; this task does not implement the tickets.

## Resume

Continue this session's source PRD; never repeat a create.
