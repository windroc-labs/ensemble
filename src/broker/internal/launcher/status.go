package launcher

import (
	"context"

	"ensemble/broker/internal/credential"
)

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

// Status lists the revocation backlog without changing credentials or emitting audit.
func (h Host) Status(_ context.Context) (StatusResult, error) {
	pending, err := h.GitHub.Pending()
	if err != nil {
		return StatusResult{}, err
	}
	return StatusResult{Revocations: pending}, nil
}

// Cleanup explicitly retries the revocation backlog and lists what is still pending.
func (h Host) Cleanup(ctx context.Context) (StatusResult, error) {
	pending, err := h.GitHub.Retry(ctx)
	if err != nil {
		return StatusResult{}, err
	}
	return StatusResult{Revocations: pending}, nil
}
