# Third-party notices

## CodexBar

`src/ensemble/session/clients/claude/usage.py` is ported from [CodexBar](https://github.com/steipete/CodexBar) at commit `6480977b47f6e4ffd5dcaae1cb0c7fdf34ee449e`: the Claude `/usage` output parser (`ClaudeStatusProbe.parse`, `ClaudeUsageFetcher.makeSnapshot`, `ClaudeStatusProbe.parseResetDate`). `ClaudeCommon.PROBE` and `ClaudeCommon.probe` in `src/ensemble/session/clients/claude/common.py` follow its direct `/usage` probe launch (`ClaudeUsageFetcher.loadViaDirectCLI`, `ClaudeCLISession.probeSettingsArguments`, `ClaudeCLISession.launchEnvironment`).

```
MIT License

Copyright (c) 2026 Peter Steinberger

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Team texts

The sources, licenses and copyright notices of files under `src/ensemble/delegate/team/` are in `src/ensemble/delegate/team/README.md`.
