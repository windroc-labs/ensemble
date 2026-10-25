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

// heldKind is the audit kind of the client credential c's secrets tmpfs holds ("": none or unknown), from its client
// label, else (a container an older broker started) its env's client home. Only for the audit: nothing else is kept.
func (h Host) heldKind(ctx context.Context, c string) string {
	if h.Audit == nil {
		return ""
	}
	out, err := h.Docker.Run(ctx, []string{"container", "inspect", "--format", `{"env":{{json .Config.Env}},"labels":{{json .Config.Labels}}}`, c}, nil)
	var config struct {
		Env    []string          `json:"env"`
		Labels map[string]string `json:"labels"`
	}
	if err != nil || json.Unmarshal(out, &config) != nil {
		return ""
	}
	if client, ok := config.Labels[clientLabel]; ok {
		return clientKind(client)
	}
	for _, kv := range config.Env {
		switch {
		case strings.HasPrefix(kv, "CLAUDE_CONFIG_DIR="):
			return audit.ClaudeSetupToken
		case strings.HasPrefix(kv, "CODEX_HOME="):
			return audit.CodexAccessToken
		}
	}
	return ""
}

// Close revokes the container's GitHub tokens, then deletes it and its tmpfs; an absent container is success.
// A revocation failure is reported after the deletion, so it never keeps a container alive.
func (h Host) Close(ctx context.Context, container string) (CloseResult, error) {
	h.Audit.Begin(container, nil)
	kind := h.heldKind(ctx, container)
	revoked := h.GitHub.Revoke(ctx, container)
	removed, err := h.remove(ctx, container)
	if removed && kind != "" {
		h.Audit.Add(container, audit.Credential{Kind: kind, Result: audit.Withdrawn}, "")
	}
	if err != nil {
		return CloseResult{}, failure(err, protocol.CloseFailed, "Deleting container "+container+" failed.")
	}
	if revoked != nil {
		return CloseResult{}, injected(revoked, protocol.KeychainFailed, "Revoking the GitHub tokens of "+container+" failed.")
	}
	return CloseResult{Container: container, Removed: removed}, nil
}
