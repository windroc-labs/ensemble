---
when: live-checking that every Mac process reads Docker workers' client state through one reader service per daemon and one reader container per daemon and volume owner (WDRC-1100), on the local Docker Desktop and a remote daemon
---

# One state reader per Docker daemon

Rules, waiting and answering: mini-op's rules (the `mini-op` skill loads them). Gotchas for this environment: `gotchas.md` beside this runbook. Design: [client_state.py](../../src/ensemble/session/engine/client_state.py), [state_service.py](../../src/ensemble/session/engine/state_service.py).

## Setup

1. Install ensemble at the branch or release under test (`install-ensemble.md`), Docker Host on, broker logged in; for the remote cases a remote Docker target reachable over SSH.
2. Record `docker version --format '{{.Server.Version}}'` and the ensemble commit. Before any worker starts, no service runs: `pgrep -f engine.state_service` prints nothing.
3. Names: `R` the reader containers, `docker ps --filter label=ensemble.state-reader --format '{{.Names}}'`; `S` the Mac's services, `pgrep -fl engine.state_service`; `D` on the remote host the read connections, `pgrep -fa 'docker system dial-stdio'` while no worker starts or stops.
4. Run every case on the local daemon, then on the remote one. The counts below assume every worker's environment has the same agent uid (one volume owner); with `k` owners, `R` is `k`.

## Cases

| Case | Do | Pass |
|---|---|---|
| Nested mounts | Before any worker: `docker run --rm --read-only --mount type=volume,source=<a state volume>,target=/state/a,readonly,volume-nocopy --mount type=volume,source=<another>,target=/state/b,readonly,volume-nocopy --entrypoint ls <worker image> /state` | prints `a` and `b` |
| Restart time | Start one worker, read it (`ensemble-session list`), then start a second and time its first status read | record the seconds, local and over SSH |
| Three workers | Start a tui, a headless worker and a board run's worker on one daemon; wait 30 s | `R` 1, `S` 1, remote `D` 1 at rest |
| Eight workers | Eight workers reporting status; one of them reads a large transcript | record each status read's time: head-of-line blocking stays within `CLIENT_WAIT` (30 s) |
| Harness killed | `kill -9` one worker's harness | `R` and `S` unchanged; the other workers still report status |
| Close and resume | `ensemble-session close W`, then resume W | the resume's history check passes; W's replies are read |
| Reclaim | Reclaim a closed session's state while others run | its volume is gone (`docker volume ls`); the others read again within one restart; `R` 1 |
| Network cut | Remote only: cut the network to the remote host for 30 s, restore it | reads recover; `R` 1, `D` 1 |
| Idle exit | Close every worker and end every Mac process of ensemble that read them (harnesses, hook servers, board runs) | ≤ 70 s later: `S` empty, `R` empty |
| Memory | `ps -o rss= -p <S>`; `docker stats --no-stream <R>` with three workers | one service and one container, against 2–3 reader containers per worker before |

Record per case: daemon, commands, `R`, `S`, `D`, seconds, result. A broken volume left unlabeled after a reclaim race (`MountRejected` on the next start) is a finding: remove it with `docker volume rm` and note the timing.
