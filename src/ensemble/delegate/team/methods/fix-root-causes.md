# Fix Root Causes Method

How to fix a failure: a bug, a failing or intermittent test, a review finding or a failing CI check. Fix it at its root cause, never its symptom.

## Steps

1. **Reproduce** it first.
2. **Trace**: ask why until you reach the cause; stuck → instrument and read the actual error instead of guessing.
3. **Fix** it at the cause.
4. **Pattern**: search for the same pattern and fix every occurrence.

## Rules

- **No silencing**: no guard that silences the failure: a nil check, a broad except, a retry, a sleep or a loosened assertion. A workaround that needs a paragraph-long comment to justify it means the code is wrong.
- **After a restart**: a failure after a restart → suspect stale persistent state (config, caches, lock files, serialized state) before code.
- **Intermittent CI**: an intermittent failure is never rerun or committed around (no empty commits, no "base flake" label without a cause); find why it can fail (concurrent writers, an unawaited exit) and fix that.
