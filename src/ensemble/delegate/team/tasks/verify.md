# Verify

Run one check on the Mac mini with the `mini-op` skill, then report its verdict.

## Input processing

[Verifier › Input processing](#input-processing).

The run requires a `Repo:` line; a missing or invalid repo or ref is a launch configuration error, before this task runs.

## Steps

1. **Read** the input: the target repo, its ref, and what to test or the steps to run. No clear check → `needs_input`, `questions` naming what is missing; stop.
2. **Report progress:**
   [ensemble-progress:start] the check and its target, `<owner>/<name>@<ref or default branch>`
3. **Check**: run the `mini-op` skill in `<Workdir>` with what to test or the input's steps, verbatim. It runs the target repo's runbook (under `.mini-op/runbooks/`) that matches, or writes a one-off runbook from the steps.
4. **Finish**, by what happened:
   - The check ran to its end, passing or not → `status: done`; `summary` opens with the report's Verdict mark, ✅, ❌ or ⚠️, then why in one line; `title` the check.
   - The check could not run (the mini unreachable, the runbook broke midway, the `mini-op` skill missing) → `status: failed`; `summary` the cause.
   - The request is still unclear → `needs_input`.

   The `done` deliverable is the report, per the Template. Whatever the status, no value from a file under `/run/ensemble/secrets/` goes into the outcome or the report.

## Resume

No outcome returned yet → redo from step 3. Otherwise redo step 4 from this session's history.
