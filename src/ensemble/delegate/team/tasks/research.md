# Research

Run one round of parallel agents, then write a short report.

## Inputs

[Researcher › Input processing](#input-processing): the question, its type and, for local or mixed, its target repos.

## Capabilities

No updraft capability: one round of subagents, restricted per [Researcher › Type and target](#type-and-target).

## Role limits

- One round of step 4's agents; agents restricted, worktree code never run ([Researcher › Type and target](#type-and-target)).
- No retry or replacement outside Resume ([Researcher › Failure](#failure)).

## Steps

1. **Input** (Inputs).
2. **Prepare** (local, mixed): [Researcher › Type and target](#type-and-target).
3. **Report progress:**
   [ensemble-progress:start] the type, plus each target repo and its commit for local or mixed
4. **Research.** 3–6 **angles** (web for web, worktree for local; for mixed, some web angles for web agents and some worktree angles for readers), one subagent each, all in parallel, restricted per [Researcher › Type and target](#type-and-target); each checks its own findings and returns, per finding, the claim, its evidence, its sources, how many are independent, and confidence; plus what it could not cover.
5. **Failure**: Failure.
6. **Report** ([Researcher › Standards](#standards)). Type line and verification depth: the Template. Revising a Light Research report (its Light Research type line marks it, in whatever language it was written) → update the line for this task.
7. **Finish**: Result.

## Result

- `done`: the report ([Researcher › Standards](#standards)). One round can't settle the question (core gaps, single-source key claims, conflicting sources) → add the status item `Suggest upgrading to Deep Research: <reason>`.
- `needs_input`: the input too vague, or a checkout exit 2 ([Researcher › Type and target](#type-and-target)).

## Failure

[Researcher › Failure](#failure). Failed angles go under Gaps.

## Resume

Reuse this session's results. Nothing dispatched → step 2. Report not written → re-dispatch each angle without a result (none, or an error) with its original prompt, once, in one parallel batch. Then steps 6–7; still nothing usable → `failed`.
