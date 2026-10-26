package launcher

import (
	"context"
	"encoding/json"
	"strings"

	"ensemble/broker/internal/audit"
	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/protocol"
)

// remove deletes c and reports whether it existed: rm --force exits 0 for an absent container but echoes no name.
func (h Host) remove(ctx context.Context, c string) (bool, error) {
	out, err := h.Docker.Run(ctx, []string{"rm", "--force", c}, nil)
	if err != nil && docker.Absent(err) {
		return false, nil
	}
	return err == nil && strings.TrimSpace(string(out)) == c, err
}

// held is what c's secrets tmpfs holds, only for the audit: kind is its client credential's audit kind ("": none or
// unknown), from its client label, else (a container an older broker started) its env's client home; secrets are the
// environment secrets its secrets label names.
func (h Host) held(ctx context.Context, c string) (kind string, secrets []string) {
	if h.Audit == nil {
		return "", nil
	}
	out, err := h.Docker.Run(ctx, []string{"container", "inspect", "--format", `{"env":{{json .Config.Env}},"labels":{{json .Config.Labels}}}`, c}, nil)
	var config struct {
		Env    []string          `json:"env"`
		Labels map[string]string `json:"labels"`
	}
	if err != nil || json.Unmarshal(out, &config) != nil {
		return "", nil
	}
	for _, item := range strings.Split(config.Labels[secretsLabel], ",") {
		if ValidEnvItem(item) {
			secrets = append(secrets, item)
		}
	}
	if client, ok := config.Labels[clientLabel]; ok {
		return clientKind(client), secrets
	}
	for _, kv := range config.Env {
		switch {
		case strings.HasPrefix(kv, "CLAUDE_CONFIG_DIR="):
			return audit.ClaudeSetupToken, secrets
		case strings.HasPrefix(kv, "CODEX_HOME="):
			return audit.CodexAccessToken, secrets
		}
	}
	return "", secrets
}

// Close revokes the container's GitHub tokens, then deletes it and its tmpfs; an absent container is success.
// A revocation failure is reported after the deletion, so it never keeps a container alive.
func (h Host) Close(ctx context.Context, container string) (CloseResult, error) {
	h.Audit.Begin(container, nil)
	kind, secrets := h.held(ctx, container)
	revoked := h.GitHub.Revoke(ctx, container)
	removed, err := h.remove(ctx, container)
	if removed {
		if kind != "" {
			h.Audit.Add(container, audit.Credential{Kind: kind, Result: audit.Withdrawn}, "")
		}
		for _, item := range secrets {
			h.Audit.Add(container, audit.Credential{Kind: audit.EnvSecret, Item: item, Result: audit.Withdrawn}, "")
		}
	}
	if err != nil {
		return CloseResult{}, failure(err, protocol.CloseFailed, "Deleting container "+container+" failed.")
	}
	if revoked != nil {
		return CloseResult{}, injected(revoked, protocol.KeychainFailed, "Revoking the GitHub tokens of "+container+" failed.")
	}
	return CloseResult{Container: container, Removed: removed}, nil
}
