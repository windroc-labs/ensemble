package launcher

import (
	"encoding/json"
	"strings"
	"testing"

	"ensemble/broker/internal/fixtures"
)

func TestCredentialRequestFixtures(t *testing.T) {
	for _, c := range fixtures.Cases(t, "credential_spec") {
		t.Run(c.File, func(t *testing.T) {
			_, err := ParseManifest(withFields(t, map[string]any{"credentials": string(c.Data)}))
			if (err == nil) != c.Valid {
				t.Fatalf("valid=%v err=%v", c.Valid, err)
			}
		})
	}
}

func TestLinearCredentialRenewalRequired(t *testing.T) {
	base := fixtureBytes(t, "credential-spec-linear.json")
	var spec map[string]map[string]json.RawMessage
	if err := json.Unmarshal(base, &spec); err != nil {
		t.Fatal(err)
	}
	var app map[string]json.RawMessage
	if err := json.Unmarshal(spec["linear"]["app"], &app); err != nil {
		t.Fatal(err)
	}
	delete(app, "renewal")
	spec["linear"]["app"], _ = json.Marshal(app)
	data, _ := json.Marshal(spec)
	if _, err := ParseManifest(withFields(t, map[string]any{"credentials": string(data)})); err == nil {
		t.Fatal("app without renewal accepted")
	}
}

func TestCredentialRequestMerge(t *testing.T) {
	environment := `{"env":[{"item":"ensemble-broker-env/old","file":"old"}],"github":{"app":"old","permissions":{"contents":"read"}}}`
	caller := `{"env":[{"item":"ensemble-broker-env/new","file":"new"}]}`
	fields := map[string]json.RawMessage{}
	for name, value := range map[string]string{"credentials": caller, "environment_credentials": environment} {
		fields[name], _ = json.Marshal(value)
	}
	request, err := parseCredentials(fields)
	if err != nil || request.GitHub.App != "old" || len(request.Env) != 1 || request.Env[0].File != "new" {
		t.Fatalf("merge: %+v %v", request, err)
	}
	base := string(fixtureBytes(t, "credential-spec-linear.json"))
	for _, sources := range []map[string]any{
		{"environment_credentials": base},
		{"credentials": base},
		{"environment_credentials": strings.Replace(base, "dummy-team-id", "environment-team", 1), "credentials": base},
	} {
		m, err := ParseManifest(withFields(t, sources))
		if err != nil || m.Linear == nil || m.Linear.Board.Team != "dummy-team-id" {
			t.Fatalf("linear merge: %+v %v", m, err)
		}
	}
	// An override does not hide an invalid environment declaration.
	for _, spec := range []string{`{"github":null}`, `{"env":[]}`, `{"env":[{"item":"other/secret","file":"x"}]}`, `{"github":{"app":"a","permissions":{"contents":"write","contents":"read"}}}`} {
		if _, err := ParseManifest(withFields(t, map[string]any{"credentials": caller, "environment_credentials": spec})); err == nil {
			t.Fatalf("accepted %s", spec)
		}
	}
}

func TestCredentialRequestGitHubServed(t *testing.T) {
	spec := `{"github":{"app":"new","permissions":{"contents":"write"}},"env":[{"item":"ensemble-broker-env/new","file":"new"}]}`
	m, err := ParseManifest(withFields(t, map[string]any{"credentials": spec}))
	if err != nil || m.GitHubApp != "new" || m.GitHubPermissions["contents"] != "write" || len(m.Secrets) != 1 || m.Secrets[0].File != "new" {
		t.Fatalf("request GitHub: %+v %v", m, err)
	}
	m, err = ParseManifest(withFields(t, map[string]any{
		"credentials":             `{"github":{"app":"new","permissions":{"contents":"write"}}}`,
		"environment_credentials": `{"github":{"app":"old","repo":"o/old","permissions":{"contents":"read"}},"env":[{"item":"ensemble-broker-env/old","file":"old"}]}`,
	}))
	if err != nil || m.GitHubApp != "new" || m.GitHubRepo != "" || m.GitHubPermissions["contents"] != "write" || len(m.Secrets) != 1 || m.Secrets[0].File != "old" {
		t.Fatalf("whole-kind override: %+v %v", m, err)
	}
	for _, old := range []any{nil, map[string]any{"app": "old", "permissions": map[string]string{"contents": "read"}}} {
		if _, err := ParseManifest(withFields(t, map[string]any{"github": old})); err == nil {
			t.Fatal("legacy github field accepted")
		}
	}
}

func TestCredentialRequestEnvReplacesLegacySecrets(t *testing.T) {
	spec := `{"env":[{"item":"ensemble-broker-env/new","file":"new"}]}`
	m, err := ParseManifest(withFields(t, map[string]any{"credentials": spec}))
	if err != nil || m.GitHubApp != "" || len(m.Secrets) != 1 || m.Secrets[0].File != "new" {
		t.Fatalf("env request not served: %+v %v", m, err)
	}
	for _, value := range []any{nil, []any{}, []any{secret("ensemble-broker-env/old", "old")}} {
		_, err = ParseManifest(withFields(t, map[string]any{"credentials": spec, "secrets": value}))
		if err == nil || !strings.Contains(err.Error(), `unknown field "secrets"`) {
			t.Fatalf("legacy secrets accepted: %v", err)
		}
	}
}

func TestCredentialRequestDoesNotEchoValues(t *testing.T) {
	for _, spec := range []string{`{"github":{"app":"SECRET","permissions":{"SECRET":"SECRET"}}}`, `{"env":[{"item":"SECRET","file":"SECRET"}]}`, `{"linear":{"SECRET":"SECRET"}}`} {
		_, err := ParseManifest(withFields(t, map[string]any{"credentials": spec}))
		if err == nil || strings.Contains(err.Error(), "SECRET") {
			t.Fatalf("unsafe rejection: %v", err)
		}
	}
	// Duplicate keys are not resolved by the JSON decoder's last-value rule.
	value, _ := json.Marshal(`{"linear":{},"linear":{}}`)
	_, err := ParseManifest(withFields(t, map[string]any{"credentials": json.RawMessage(value)}))
	if err == nil {
		t.Fatal("duplicate kind accepted")
	}
}
