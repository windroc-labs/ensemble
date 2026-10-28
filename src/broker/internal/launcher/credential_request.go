package launcher

import (
	"bytes"
	"encoding/json"
	"regexp"
	"strings"

	"ensemble/broker/internal/credential"
	githubapi "ensemble/broker/internal/github"
	"ensemble/broker/internal/protocol"
)

func (m *Manifest) parseCredentials(fields map[string]json.RawMessage) error {
	merged := map[string]json.RawMessage{}
	for _, source := range []string{"environment_credentials", "credentials"} {
		raw, present := fields[source]
		if !present {
			continue
		}
		reject := func(kind string) error {
			return protocol.New(protocol.ManifestRejected, "Manifest rejected: "+source+": invalid "+kind)
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
			valid := false
			switch kind {
			case "github":
				var parsed Manifest
				valid = string(value) != "null" && parsed.parseGitHub(value) == nil
			case "env":
				var parsed Manifest
				valid = parsed.parseSecrets(value) == nil && len(parsed.Secrets) > 0
			case "linear":
				_, err := parseLinear(value)
				valid = err == nil
			default:
				return protocol.New(protocol.ManifestRejected, "Manifest rejected: "+source+": unknown kind "+kind)
			}
			if !valid {
				return reject(kind)
			}
			merged[kind] = value
		}
	}
	for kind, value := range merged {
		switch kind {
		case "github":
			if m.GitHubApp != "" {
				return protocol.New(protocol.ManifestRejected, "Manifest rejected: github specified twice")
			}
			_ = m.parseGitHub(value)
		case "env":
			if _, exists := fields["secrets"]; exists {
				return protocol.New(protocol.ManifestRejected, "Manifest rejected: env specified twice")
			}
			_ = m.parseSecrets(value)
		case "linear":
			m.Linear, _ = parseLinear(value)
		}
	}
	return nil
}

var rolePattern = regexp.MustCompile(`^[a-z][a-z0-9-]{0,62}$`)
var linearServicePattern = regexp.MustCompile(`^[A-Za-z0-9._@+-]{1,128}$`)
var linearUserPattern = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)

func credentialReference(s string) bool {
	return s != "" && !strings.ContainsFunc(s, func(r rune) bool { return r < 0x20 || r >= 0x7f && r <= 0x9f })
}

func parseLinear(data []byte) (*credential.LinearRequest, error) {
	bad := protocol.New(protocol.ManifestRejected, "Manifest rejected: invalid linear")
	var r credential.LinearRequest
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	if dec.Decode(&r) != nil {
		return nil, bad
	}
	a, b := r.App, r.Board
	if !rolePattern.MatchString(a.Role) || a.Role == "harness" || a.Role == "manager" || !credentialReference(a.ClientID) || !linearUserPattern.MatchString(a.UserID) {
		return nil, bad
	}
	for _, s := range []string{a.SecretService, a.TokenService} {
		if !linearServicePattern.MatchString(s) || s == "." || s == ".." {
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
			if !credentialReference(key) || !credentialReference(value) {
				return nil, bad
			}
		}
	}
	for key, repo := range b.ProjectRepos {
		if !credentialReference(key) || !githubapi.ValidRepo(repo) {
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
