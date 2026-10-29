---
when: running an ensemble manager (and its workers) on the mini, e.g. an OpenCode manager through Strata
---

# Manager on the mini

Gotchas for this environment: `gotchas.md` beside this runbook.

Strata is a PC on the LAN with one slot: run at most one OpenCode manager and one OpenCode worker at a time (the user, 2026-10-21), and close both when a test ends.
0. Screen: only two windows, Terminal and Chrome; never move or resize one (mini-op's rules, which the `mini-op` skill loads › Rules). Close Chrome's extra "New Tab" window first, and every window a step no longer needs. The worker's pane needs to be wider than 37 columns: if the Terminal is narrower, tell the user rather than resizing it.
   Docker Desktop's first launch leaves a Containers window: use System Events, in process `Docker Desktop`, to click that window's button whose `subrole` is `AXCloseButton` (inspect the process/window names first). Docker keeps running. After every step check Terminal and Chrome for extra windows or tabs and close the extras. Terminal's AppleScript window list may keep closed windows with `tabs=0`; use the windows visible to System Events as the authority.
1. From the mini's Terminal: `mkdir -p ~/manager && source ~/.zprofile && ensemble mini`; in it `cd ~/manager && claude --model sonnet` (the user, 2026-10-21: the test manager runs Sonnet at most; never in the home directory). Trust `~/manager`, then `/ensemble:manage`.
2. Drive it with `tmux send-keys -t '=mini:' …` and read it with `tmux capture-pane -p -t '=mini:'` (`/opt/homebrew/bin/tmux`); keystrokes through System Events can land in another Terminal window.
3. Its first start raises macOS folder prompts for Terminal (Downloads, Documents, Desktop, Photos): Don't Allow each (System Events: click button "Don’t Allow" in process UserNotificationCenter).
