# Light Research

Search, read and write a short report yourself, in one pass.

## Inputs

[Researcher › Input processing](#input-processing): the question, its type and, for local or mixed, its target repos.

## Capabilities

No updraft capability: your own web search and fetch and worktree reads.

## Role limits

- Subagents, workflows and web queries: step 4.
- Worktree code never run ([Researcher › Type and target](#type-and-target)).

## Steps

1. **Input** (Inputs).
2. **Prepare** (local, mixed): [Researcher › Type and target](#type-and-target).
3. **Report progress:**
   [ensemble-progress:start] the type, plus each target repo and its commit for local or mixed
4. **Research** yourself, in one pass: no subagents, no workflow. Web search and fetch for web, the worktrees for local, both for mixed; web queries carry no private detail ([Researcher › Type and target › Agents](#type-and-target)). Note per finding the claim, its evidence, its sources, how many are independent, and confidence; plus what you could not cover.
5. **Failure**: Failure.
6. **Report** ([Researcher › Standards](#standards)). Type line and verification depth: the Template.
7. **Finish**: Result.

## Result

- `done`: the report ([Researcher › Standards](#standards)). One pass can't settle the question (core gaps, single-source key claims, conflicting sources) → add the status item `Suggest upgrading to Research: <reason>`.
- `needs_input`: the input too vague, or a checkout exit 2 ([Researcher › Type and target](#type-and-target)).

## Failure

[Researcher › Failure](#failure).

## Resume

Step 4 not started → step 2. Else reuse this session's findings; continue step 4 where it stopped. Then steps 6–7; nothing usable → `failed`.
