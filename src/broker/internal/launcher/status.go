package launcher

import "ensemble/broker/internal/credential"

// heldKinds are the credential kinds m's container holds: its client's, GitHub's for an App, env for secrets.
func heldKinds(m Manifest) []credential.Kind {
	var kinds []credential.Kind
	switch m.Client {
	case "codex":
		kinds = append(kinds, credential.Codex)
	case "opencode":
		kinds = append(kinds, credential.Opencode)
	default:
		kinds = append(kinds, credential.Claude)
	}
	if m.GitHubApp != "" {
		kinds = append(kinds, credential.GitHub)
	}
	if len(m.Secrets) > 0 {
		kinds = append(kinds, credential.Env)
	}
	if m.Linear != nil {
		kinds = append(kinds, credential.Linear)
	}
	return kinds
}

// Status checks the local usability of each valid named Claude account.
func Status(accounts []string, usable func(string) bool) StatusResult {
	result := StatusResult{Claude: map[string]bool{}}
	for _, account := range accounts {
		if ValidClaudeAccount(account) {
			result.Claude[account] = usable(account)
		}
	}
	return result
}
