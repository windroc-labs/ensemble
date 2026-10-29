# Decompose

Turn one handed-off PRD into child issues with `<team>/methods/to-tickets.md`. Publish with this task's commands and fields.

## Steps

1. **Read** the source PRD (`ensemble-orchestrator issue read <PRD> --full --output <Workdir>/tmp/prd.json`) and check out each target repo read-only ([Principles › Work › Worktree](#work)), branch `<id>-decompose`. Then report:
   [ensemble-progress:start] decompose, the source PRD
2. **Draft** with the method. Each description opens with `Repo:`, `Covers:` (PRD identifier + requirement identifiers) and `Task:`; engineer work then `Scope:`, one `- ` path per item, `(new)` when absent from the base.
3. **Scope check** ([Engineer › Input processing](#input-processing)) on the drafts; fix until clean.
4. **Publish**: `ensemble-orchestrator issue create --title <title> --body-file <file> --parent <PRD> --state ready --role engineer|verifier --client claude --priority <the PRD's> --project <the repo's mapped project> --blocked-by <issue>`, adding `--task light-build` for a small engineer ticket. Outcome `title`: the PRD's product name; `summary`: `Created <identifiers>; reused none`.

## Resume

Continue this session's source PRD; never repeat a create.
