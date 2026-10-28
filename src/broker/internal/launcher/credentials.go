package launcher

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"maps"
	"regexp"
	"slices"
	"strings"

	githubapi "ensemble/broker/internal/github"
)

// CredentialRequest is a validated, merged kind table. Issuance is not implemented yet.
type CredentialRequest map[string]json.RawMessage

func (c CredentialRequest) Kinds() []string {
	var kinds []string
	for _, kind := range []string{"github", "linear", "env"} {
		if _, ok := c[kind]; ok {
			kinds = append(kinds, kind)
		}
	}
	return kinds
}

var credentialKinds = map[string]func(json.RawMessage) string{
	"github": func(raw json.RawMessage) string { _, problem := parseGrant(raw); return problem },
	"linear": checkLinear,
	"env":    checkCredentialEnv,
}

// Each envelope is checked before replacement, including the environment's overridden kinds.
func mergedCredentials(fields map[string]json.RawMessage) (CredentialRequest, string) {
	var result CredentialRequest
	for _, source := range []string{"environment_credentials", "credentials"} {
		raw, ok := fields[source]
		if !ok {
			continue
		}
		found, problem := parseCredentialEnvelope(raw, credentialKinds)
		if problem != "" {
			return nil, source + problem
		}
		if result == nil {
			result = CredentialRequest{}
		}
		maps.Copy(result, found)
	}
	return result, ""
}

func parseCredentialEnvelope(raw json.RawMessage, kinds map[string]func(json.RawMessage) string) (CredentialRequest, string) {
	var text string
	if len(raw) == 0 || raw[0] != '"' || json.Unmarshal(raw, &text) != nil {
		return nil, ": want JSON text"
	}
	decoder := json.NewDecoder(strings.NewReader(text))
	decoder.UseNumber()
	if strictCredentialJSON(decoder) != nil {
		return nil, ": invalid JSON or duplicate key"
	}
	if _, err := decoder.Token(); err != io.EOF {
		return nil, ": trailing JSON content"
	}
	fields, ok := object(json.RawMessage(bytes.TrimSpace([]byte(text))))
	if !ok {
		return nil, ": want an object"
	}
	for _, kind := range slices.Sorted(maps.Keys(fields)) {
		check, ok := kinds[kind]
		if !ok {
			if credentialKindName.MatchString(kind) {
				return nil, "." + kind + ": unknown kind"
			}
			return nil, ": unknown kind"
		}
		if problem := check(fields[kind]); problem != "" {
			return nil, "." + kind + ": " + problem
		}
	}
	return CredentialRequest(fields), ""
}

func strictCredentialJSON(d *json.Decoder) error {
	token, err := d.Token()
	if err != nil {
		return err
	}
	delimiter, nested := token.(json.Delim)
	if !nested {
		return nil
	}
	seen := map[string]bool{}
	for d.More() {
		if delimiter == '{' {
			key, err := d.Token()
			if err != nil {
				return err
			}
			name, ok := key.(string)
			if !ok || seen[name] {
				return fmt.Errorf("duplicate key")
			}
			seen[name] = true
		}
		if err := strictCredentialJSON(d); err != nil {
			return err
		}
	}
	_, err = d.Token()
	return err
}

var (
	credentialKindName = regexp.MustCompile(`^[a-z][a-z0-9_]{0,62}$`)
	linearReference    = regexp.MustCompile(`^[A-Za-z0-9._@+-]{1,128}$`)
	linearUserID       = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)
)

func checkCredentialEnv(raw json.RawMessage) string {
	items, ok := list(raw, maxSecrets)
	if !ok {
		return "want 1 to 16 secrets"
	}
	files := map[string]bool{}
	for i, raw := range items {
		entry, problem := parseEnvSecret(raw)
		if problem == "" && files[entry.File] {
			problem = "file repeated"
		}
		if problem != "" {
			return fmt.Sprintf("entry %d: %s", i, problem)
		}
		files[entry.File] = true
	}
	return ""
}

func reference(raw json.RawMessage) bool {
	text, ok := str(raw)
	return ok && text != "" && len(text) <= 256 && !strings.ContainsFunc(text, func(r rune) bool { return r <= 0x20 || r > 0x7e })
}

func stringTable(raw json.RawMessage, repo bool) bool {
	fields, ok := object(raw)
	if !ok {
		return false
	}
	for key, value := range fields {
		if key == "" || hasControl(key) || !reference(value) {
			return false
		}
		if repo {
			text, _ := str(value)
			if !githubapi.ValidRepo(text) {
				return false
			}
		}
	}
	return true
}

func checkLinear(raw json.RawMessage) string {
	fields, ok := object(raw)
	if !ok || len(fields) != 2 || !onlyKeys(fields, "app", "board") {
		return "want app and board"
	}
	app, ok := object(fields["app"])
	if !ok || len(app) != 5 || !onlyKeys(app, "role", "client_id", "user_id", "secret_service", "token_service") {
		return "app not valid"
	}
	for _, key := range []string{"role", "client_id", "user_id", "secret_service", "token_service"} {
		if !reference(app[key]) {
			return "app " + key + " not valid"
		}
	}
	role, _ := str(app["role"])
	user, _ := str(app["user_id"])
	secret, _ := str(app["secret_service"])
	token, _ := str(app["token_service"])
	if !rolePattern.MatchString(role) || role == "harness" || role == "manager" {
		return "app role not allowed"
	}
	if !linearUserID.MatchString(user) {
		return "app user_id not valid"
	}
	for _, service := range []string{secret, token} {
		if !linearReference.MatchString(service) || service == "." || service == ".." {
			return "app service not valid"
		}
	}
	if secret == token {
		return "app services must differ"
	}
	board, ok := object(fields["board"])
	if !ok || len(board) != 5 || !onlyKeys(board, "team", "states", "labels", "project_repos", "human_members") {
		return "board not valid"
	}
	if !reference(board["team"]) || !stringTable(board["project_repos"], true) {
		return "board references not valid"
	}
	states, ok := object(board["states"])
	names := []string{"backlog", "todo", "in_progress", "in_review", "handoff", "done", "canceled"}
	if !ok || len(states) != len(names) || !onlyKeys(states, names...) || !stringTable(board["states"], false) {
		return "board states not valid"
	}
	labels, ok := object(board["labels"])
	if !ok || len(labels) != 2 || !onlyKeys(labels, "task_labels", "agent_labels") || !stringTable(labels["task_labels"], false) || !stringTable(labels["agent_labels"], false) {
		return "board labels not valid"
	}
	var members []json.RawMessage
	if len(board["human_members"]) == 0 || board["human_members"][0] != '[' || json.Unmarshal(board["human_members"], &members) != nil {
		return "board human_members not valid"
	}
	for _, member := range members {
		if !reference(member) {
			return "board human_members not valid"
		}
	}
	return ""
}
