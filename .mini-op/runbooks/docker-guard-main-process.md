---
when: live-checking that a Docker worker's container ends with its guard (WDRC-345): kill, launch-deadline, close (a foreground headless run's too, WDRC-364), needs_input, lock and log cases
---

# Docker worker container ends with its guard

Rules, waiting and answering: mini-op's rules (the `mini-op` skill loads them). Gotchas for this environment: `gotchas.md` beside this runbook. Design: [session implementation contracts](../../docs/sessions.md#implementation-contracts).

## Setup

1. Install ensemble at the branch or release under test (`install-ensemble.md`), Docker Host on, broker logged in. `"$B" version` prints its `protocol_version`: 32 or later.
2. Record `docker version --format '{{.Server.Version}}'` and the ensemble commit.
3. Names: `W` a new worker name per case; `C` its container, `docker ps --filter label=ensemble.session=W --format '{{.Names}}'`; `A` the harness's attach process, `pgrep -f "attach --sig-proxy=false $C"`; `H` the harness, `ps -o ppid= -p $A`; `G` the guard's PID in the container, from `docker exec $C ps -o pid,ppid,args` (PID 1 is tini; the guard is its child running `guard.py --serve`; the client is the guard's child).
4. Each case runs twice: TUI (`ensemble-session start W --client claude --host docker --cwd <test repo checkout> --prompt 'Say hi and wait.'`) and headless (a role run, as `python3 tests/session/live_docker.py role --repo OWNER/NAME` starts one, observed while it runs).
5. Time each case from the kill to the moment `docker inspect -f '{{.State.Status}}' $C` stops printing `running`, polling every 0.5 s. Record per case: mode, commands, `C`, `H`, `A`, `G`, client PID, seconds, result.

## Cases

| Case | Do | Pass |
|---|---|---|
| FR-1 tree | `docker exec $C ps -o pid,ppid,args`; on the Mac `ps -o pid,args -p $A` | tini PID 1 → `guard.py --serve` → client; the Mac runs one `docker attach --sig-proxy=false $C` per worker and no `docker exec … guard.py` besides a display |
| FR-4 harness | `kill -9 $H` | ≤ 10 s: container not running, no client (`docker top` fails or lists none) |
| FR-4 guard | `docker exec $C kill -9 $G` | same |
| FR-4 both | `kill -9 $H; docker exec $C kill -9 $G` | same |
| FR-5 attach only | `kill -9 $A` | same |
| FR-5 close | `ensemble-session close W` | returns once the container is removed (`docker ps -a` has no `C`) |
| FR-5 foreground headless close (WDRC-364) | headless only: in one terminal a foreground role run on the Docker Host (`ensemble-delegate run --role dummy-tester …`, not detached); `W` is its agent session (`docker ps --filter label=ensemble.session --format '{{.Label "ensemble.session"}}'`). From a second terminal, once `C` runs: `ensemble-session close W; echo $?`. Repeat on a new run with `ensemble-delegate close <its run workdir>; echo $?` | each close returns once `C` is removed (`docker ps -a` has no `C`, timed as step 5, ≤ 10 s) and the foreground run then ends; or it exits non-zero naming why. Exit 0 with `C` still running is a miss |
| FR-3 killed during start | start W; `kill -9 $H` as soon as `C` exists, before its client runs | no client ever; container stopped ≤ 60 s after it started |
| FR-3 manual start | on a stopped worker container, `docker start $C` | no client; stopped ≤ 60 s later |
| FR-6 needs_input | a locked run (an environment with `lock`) that answers `needs_input`; then `ensemble-crew resume --input …` | `C` and `G` unchanged; the answer is processed |
| FR-7 lock | the locked run: `kill -9 $H` | 10 s later a new start of that lock is not busy |
| FR-7 resume | after FR-4 harness, `ensemble-session resume W` | new container ID; conversation continues; workspace files kept; a resume while the old container still runs (during the reap) is refused busy |
| NFR-2 logs | after a run with a prompt, a hook and output, and after a handoff error (`docker start` of a stopped one) | `docker logs $C` fails with `configured logging driver does not support reading` and prints nothing of the prompt, hooks, output or env |
| Long session | a TUI worker idle 10 minutes, then typed into | still running; input reaches the client |
| Start time | the harness log's `docker timing W` lines | broker start + staging + attach well under 60 s |

## Report

One table per mode with every case's row; Docker version and commit at the top. Any miss: the case, its commands and output, and the container's `docker inspect -f '{{json .State}}' $C`.
