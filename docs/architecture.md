# Core architecture

Five layers, one project each: utils < session < delegate < orchestrator < manager. Import rules: `AGENTS.md` › Import rules. The code, by package; terms in `CONTEXT.md`.

```mermaid
flowchart TB
  subgraph EN[entry points]
    LD[launchd]
    HU[the user]
    CC[Starter, control and resume commands]
  end
  subgraph MG[ensemble.manager]
    MC["cli.py<br>ensemble-crew"] --> W["engine/crew.py"] --> RS["engine/roster.py"]
    MC --> HO["engine/holder.py<br>lease while a command runs"] --> RS
  end
  subgraph LO[ensemble.orchestrator]
    LCLI["cli.py<br>ensemble-orchestrator"]
    R["engine/router.py<br>pick · usage gate · trigger"]
    LR["engine/linear_run.py<br>Delegate ownership · queries"]
    WB["engine/writeback.py<br>one event's write-back"]
    PP["engine/promote.py → prune.py"]
    LT["trackers/linear/linear_tracker.py<br>LinearTracker"]
    LB["trackers/local/<br>LocalTracker · issue files · board lock"]
    FL["engine/flow/<br>Tracker · IssueState · Handoff grammar · workflow"]
    LC["engine/config.py<br>[core] overlay"]
  end
  subgraph DL[ensemble.delegate]
    D["cli.py<br>ensemble-delegate"]
    HD["engine/harness_delegate.py<br>HarnessDelegate · state.py RunState"]
    H["engine/harness.py<br>detached run"]
    E["engine/: base · plan · drivers · runner · callback · types · config · ready_gate"]
    C["composer/<br>prompt · run spec"]
    EV["event/<br>reply · outcome check · callback line"]
    CF["config.py<br>package settings"]
    subgraph TM[team/]
      T["roles · tasks · templates"]
      AT[tools/]
    end
  end
  subgraph SE[ensemble.session]
    SC["cli.py<br>ensemble-session"]
    DR["engine/: base · hosted_session · worker · types · records · tmux_cli<br>modes/: headless · tui · in_process<br>modes/client_adapters: Headless · JsonHeadless · Tui · InProcess<br>viewer/: viewer · display · placement · grid · iterm · hidden<br>hosts/base: Host · Process · Interactive · InteractiveProcess · Piped<br>hosts/types: SessionView · CommandView<br>hosts/reclaimed: ReclaimedGroup<br>hosts/tmux/: host · hook<br>hosts/docker/: host · hook · broker · daemon · mounts · pipe · relay · stub · supervisor"]
    subgraph AD[clients/]
      CL[claude]
      CX[codex]
    end
  end
  U[ensemble.utils]
  subgraph RT[clients]
    P1[Claude Code]
    P2[Codex CLI]
  end
  LD -.->|"router · promote"| LCLI
  HU -.->|"router --issue"| LCLI
  HU -.->|"act-as · CLI"| D
  HU -.->|"ctmux · CLI"| MC
  HU -.-> SC
  SC --> DR
  LCLI --> R & PP & WB & LR
  R & PP & WB --> LR
  PP & R --> LT --> FL
  LCLI --> LB --> FL
  PP & R --> FL
  WB --> LT
  WB --> FL
  R -->|"writeback.replay"| WB
  LR -->|"ensemble.delegate: detach, views"| HD
  H -.->|"each event: ensemble-orchestrator writeback"| LCLI
  LC -->|"Delegate class methods"| HD
  W -.-> CC
  D & H --> HD
  D --> C
  HD -.->|"detach on the harness Host"| H
  HD --> E & C
  E --> C
  DL -->|ensemble.session| SE
  C -->|reads| T
  DR --> AD
  CL -.-> P1
  CX -.-> P2
  RT -.-> AT
  classDef shared stroke-dasharray: 4 3
  class EV,U,CF shared
```

Solid arrow: an import, the only direction allowed, or a file read (`reads`); its label names the package imported, by its `__all__`. Dotted: runs a command or process. Dashed: shared modules, their imports not drawn.
