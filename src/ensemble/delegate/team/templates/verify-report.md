# <check title>

**Verdict:** <✅ | ❌ | ⚠️>: <one line why>

| Requirement | Check | R1 | Notes (latest non-✅ only) |
|---|---|---|---|
| <FR-n> | <one line> | <mark> | <cause> |

Blocked / next: <one line>

## Template rules

Omit Template rules from the report. The report is the H1, the Verdict line, the table, at most the Blocked / next line, and [Principles › Overlap](#overlap)'s additions after them (its source or earlier-version links in one line, then its Board proposal section): no other section, execution record, runbook text, source list, principle, script, diagram or narrative.

- Marks, the only ones: ✅ pass, ❌ fail, ⚠️ partial, ⏭️ skipped (a refused step too, its Notes saying why).
- Rows: one per requirement checked (its id, else a short name), each with its check in one line.
- Rounds: an earlier report for this check (the input's, or the file [Output › Destination](#destination) step 2 reuses) → keep its table and add the next round's column (R2, R3, …) before Notes; a row added later leaves earlier rounds' cells empty, and a row not rerun leaves this round's cell empty. A row's latest result is its last filled cell. An earlier report not in this shape → start a new table at R1. A resumed run whose round is already published updates that column.
- Verdict: ✅ when every row's latest result is ✅, ❌ when any is ❌, else ⚠️.
- Notes: only for a row whose latest result is not ✅; its cause in one or two sentences with the one decisive evidence item. Empty otherwise.
- Blocked / next: only when the Verdict is not ✅; one line, else omitted.
