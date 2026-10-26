# Verify

Run one check on the Mac mini with `/mini-op:run`, then report its verdict.

## Input processing

[Verifier › Input processing](#input-processing).

## Steps

1. **Read** the input: the target repo, its ref, and what to test or the steps to run. No target repo, or no clear check → `needs_input`, `questions` naming what is missing; stop.
2. **Report progress:**
   [ensemble-progress:start] the check and its target, `<owner>/<name>@<ref or default branch>`
3. **Check**: run `/mini-op:run` in `<Workdir>` with what to test or the input's steps, verbatim. It runs the target repo's runbook (under `.mini-op/runbooks/`) that matches, or writes a one-off runbook from the steps.
4. **Finish**, by what happened:
   - The check ran to its end, passing or not → `status: done`; `summary` opens with the verdict, `Pass:` or `Fail:`, then why in one line; `title` the check.
   - The check could not run (the mini unreachable, the runbook broke midway, `/mini-op:run` missing) → `status: failed`; `summary` the cause.
   - The request is still unclear → `needs_input`.

   The `done` deliverable is the report: the verdict; the runbook used, the runbook's source and path (the target repo's, or a one-off written for this run); for a one-off runbook, its full text in a fenced block; the steps run and their results. Whatever the status, no value from a file under `/run/ensemble/secrets/` goes into the outcome or the report.

## Resume

No outcome returned yet → redo from step 3. Otherwise redo step 4 from this session's history.
