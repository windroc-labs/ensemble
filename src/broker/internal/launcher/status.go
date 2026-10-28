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
	return kinds
}
