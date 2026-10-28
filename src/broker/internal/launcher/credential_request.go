package launcher

import (
	"encoding/json"
	"strings"

	"ensemble/broker/internal/credential"
	githubapi "ensemble/broker/internal/github"
	"ensemble/broker/internal/protocol"
)

type credentialRequest struct {
	GitHub *githubRequest
	Env    []Secret
	Linear *credential.LinearRequest
}

func parseCredentials(fields map[string]json.RawMessage) (credentialRequest, error) {
	var merged credentialRequest
	for _, source := range []string{"environment_credentials", "credentials"} {
		raw, present := fields[source]
		if !present {
			continue
		}
		reject := func(kind string) (credentialRequest, error) {
			return credentialRequest{}, protocol.New(protocol.ManifestRejected, "Manifest rejected: "+source+": invalid "+kind)
		}
		var text string
		if json.Unmarshal(raw, &text) != nil || string(raw) == "null" {
			return reject("JSON text")
		}
		spec, err := manifestObject([]byte(text))
		if err != nil {
			return reject("credential object")
		}
		for kind, value := range spec {
			switch kind {
			case "github":
				merged.GitHub, err = parseGitHub(value)
				if err != nil || merged.GitHub == nil {
					return reject(kind)
				}
			case "env":
				merged.Env, err = parseSecrets(value)
				if err != nil || len(merged.Env) == 0 {
					return reject(kind)
				}
			case "linear":
				merged.Linear, err = parseLinear(value)
				if err != nil {
					return reject(kind)
				}
			default:
				return credentialRequest{}, protocol.New(protocol.ManifestRejected, "Manifest rejected: "+source+": unknown kind "+kind)
			}
		}
	}
	return merged, nil
}

func credentialReference(s string) bool {
	return len(s) > 0 && len(s) <= 256 && !strings.ContainsFunc(s, func(r rune) bool { return r < 0x21 || r > 0x7e })
}

func credentialKey(s string) bool {
	return s != "" && !strings.ContainsFunc(s, func(r rune) bool { return r < 0x20 || r >= 0x7f && r <= 0x9f })
}

func exactCredentialObject(data []byte, keys ...string) map[string]json.RawMessage {
	var object map[string]json.RawMessage
	if json.Unmarshal(data, &object) != nil || len(object) != len(keys) {
		return nil
	}
	for _, key := range keys {
		if _, ok := object[key]; !ok {
			return nil
		}
	}
	return object
}

func parseLinear(data []byte) (*credential.LinearRequest, error) {
	bad := protocol.New(protocol.ManifestRejected, "Manifest rejected: invalid linear")
	var r credential.LinearRequest
	object := exactCredentialObject(data, "app", "board", "scopes")
	app := exactCredentialObject(object["app"], "role", "client_id", "user_id", "secret_service", "token_service")
	board := exactCredentialObject(object["board"], "team", "states", "labels", "project_repos", "human_members", "role_users", "task_label_group", "agent_label_group")
	labels := exactCredentialObject(board["labels"], "task_labels", "agent_labels")
	if object == nil || app == nil || board == nil || labels == nil || json.Unmarshal(data, &r) != nil {
		return nil, bad
	}
	a, b := r.App, r.Board
	if (a.Role == "reader") != (r.Scopes == "read") {
		return nil, bad
	}
	if !credential.ValidLinearScopes(r.Scopes) || b.RoleUsers == nil || !credentialReference(b.TaskLabelGroup) || (b.AgentLabelGroup != "" && !credentialReference(b.AgentLabelGroup)) {
		return nil, bad
	}
	for role, user := range b.RoleUsers {
		if !credential.ValidLinearRole(role) || !credential.ValidLinearUser(user) {
			return nil, bad
		}
	}
	if !credential.ValidLinearRole(a.Role) || a.Role == "harness" || a.Role == "manager" || !credentialReference(a.ClientID) || !credential.ValidLinearUser(a.UserID) {
		return nil, bad
	}
	for _, s := range []string{a.SecretService, a.TokenService} {
		if !credential.ValidLinearService(s) {
			return nil, bad
		}
	}
	if a.SecretService == a.TokenService || !credentialReference(b.Team) || len(b.States) != 7 || b.Labels.TaskLabels == nil || b.Labels.AgentLabels == nil || b.ProjectRepos == nil || b.HumanMembers == nil {
		return nil, bad
	}
	for _, key := range []string{"backlog", "todo", "in_progress", "in_review", "handoff", "done", "canceled"} {
		if !credentialReference(b.States[key]) {
			return nil, bad
		}
	}
	for _, labels := range []map[string]string{b.Labels.TaskLabels, b.Labels.AgentLabels} {
		for key, value := range labels {
			if !credentialKey(key) || !credentialReference(value) {
				return nil, bad
			}
		}
	}
	for key, repo := range b.ProjectRepos {
		if !credentialKey(key) || !githubapi.ValidRepo(repo) {
			return nil, bad
		}
	}
	for _, member := range b.HumanMembers {
		if !credentialReference(member) {
			return nil, bad
		}
	}
	return &r, nil
}
