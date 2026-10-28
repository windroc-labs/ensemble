# <check title>

**Verdict:** <✅ | ❌ | ⚠️>: <one line why>

| Requirement | Check | R1 | Notes (latest non-✅ only) |
|---|---|---|---|
| <FR-n> | <one line> | <mark> | <cause> |

Blocked / next: <one line>

## Template rules

Omit Template rules from the report. The report is the H1, the Verdict line, the table and at most the Blocked / next line: no other section, execution record, runbook text, source list, principle, script, diagram or narrative.

- Marks, the only ones: ✅ pass, ❌ fail, ⚠️ partial, ⏭️ skipped.
- Rows: one per requirement checked (its id, else a short name), each with its check in one line.
- Rounds: the input gives an earlier report → keep its table and add the next round's column (R2, R3, …) before Notes; a row added later leaves earlier rounds' cells empty, and a row not rerun leaves this round's cell empty. A row's latest result is its last filled cell.
- Verdict: ✅ when every row's latest result is ✅, ❌ when any is ❌, else ⚠️.
- Notes: only for a row whose latest result is not ✅; its cause in one or two sentences with the one decisive evidence item. Empty otherwise.
- Blocked / next: only when the Verdict is not ✅; one line, else omitted.
