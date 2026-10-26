package launcher

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"slices"
	"time"

	"ensemble/broker/internal/audit"
	"ensemble/broker/internal/credential"
	"ensemble/broker/internal/protocol"
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

// renewable is whether any of m's credentials is a token kind; static ones a human rotates.
func renewable(m Manifest) bool {
	return slices.ContainsFunc(heldKinds(m), func(k credential.Kind) bool { return credential.Renewable(k) == nil })
}

// Refresh replaces container credentials; path is its original manifest.
func (h Host) Refresh(ctx context.Context, container, path string) (RefreshResult, error) {
	h.Audit.Begin(container, nil)
	data, err := os.ReadFile(path)
	if err != nil {
		return RefreshResult{}, protocol.New(protocol.ManifestRejected, "Cannot read manifest "+path+".")
	}
	m, err := ParseManifest(data)
	if err != nil {
		return RefreshResult{}, err
	}
	if "ensemble-"+m.Name+"-"+m.Generation != container || !renewable(m) {
		return RefreshResult{}, protocol.New(protocol.ManifestRejected, "Manifest rejected: refresh needs the container's manifest and renewable credentials.")
	}
	if err := h.checkMounts(m); err != nil {
		return RefreshResult{}, err
	}
	result := RefreshResult{Container: container, GitHub: []protocol.Expiry{}}
	// Codex and GitHub renew independently: a failed Codex step must not let the GitHub tokens expire.
	var codexErr, githubErr *protocol.Error
	if m.Client == "codex" {
		exec := h.agentExec(ctx, container)
		inject := func(argv []string, data []byte) error {
			return exec(append(argv, m.CodexDir), data)
		}
		expiry, err := h.Codex.Refresh(ctx, container, inject, func(expiry string) error {
			if err := h.publishExpiry(m, expiry); err != nil {
				return protocol.New(protocol.RefreshFailed, "Publishing the Codex credential deadline failed; retry refresh.")
			}
			return nil
		})
		if err != nil {
			codexErr = injected(err, protocol.RefreshFailed, "Writing Codex credentials into "+container+" failed.")
		} else {
			result.CredentialExpiresAt = &expiry
			h.Audit.Add(container, audit.Credential{Kind: clientKind(m.Client), Result: audit.Renewed, ExpiresAt: &expiry}, "")
		}
	}
	if m.GitHubApp != "" {
		if err := h.GitHub.Ready(m.GitHubApp); err != nil {
			githubErr = injected(err, protocol.RefreshFailed, "GitHub renewal for "+container+" failed.")
		} else if expiries, err := h.GitHub.Refresh(ctx, m.GitHubApp, m.GitHubPermissions, container, h.agentExec(ctx, container)); err != nil {
			githubErr = injected(err, protocol.RefreshFailed, "Writing a GitHub token into "+container+" failed.")
		} else {
			result.GitHub = expiries
		}
	}
	switch {
	case codexErr != nil && githubErr != nil:
		both := *codexErr
		both.Message += "; GitHub renewal also failed: " + githubErr.Message
		return RefreshResult{}, &both
	case codexErr != nil:
		return RefreshResult{}, codexErr
	case githubErr != nil:
		return RefreshResult{}, githubErr
	}
	return result, nil
}

func (h Host) publishExpiry(m Manifest, expiry string) error {
	value, err := time.Parse(time.RFC3339, expiry)
	if err != nil || value.UTC().Format(time.RFC3339) != expiry {
		return errors.New("invalid credential deadline")
	}
	data, err := json.Marshal(expiry)
	if err != nil {
		return err
	}
	return h.writeState(m, m.Generation+".credential.json", data)
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
