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

func TestCredentialRequestMerge(t *testing.T) {
	env := `{"env":[{"item":"ensemble-broker-env/old","file":"old"}],"github":{"app":"old","permissions":{"contents":"read"}}}`
	caller := `{"env":[{"item":"ensemble-broker-env/new","file":"new"}]}`
	m, err := ParseManifest(withFields(t, map[string]any{"credentials": caller, "environment_credentials": env}))
	if err != nil || m.GitHubApp != "old" || len(m.Secrets) != 1 || m.Secrets[0].File != "new" {
		t.Fatalf("merge: %+v %v", m, err)
	}
	for _, spec := range []string{`{"github":null}`, `{"env":[]}`, `{"env":[{"item":"other/secret","file":"x"}]}`, `{"github":{"app":"a","permissions":{"contents":"write","contents":"read"}}}`} {
		if _, err := ParseManifest(withFields(t, map[string]any{"credentials": spec})); err == nil {
			t.Fatalf("accepted %s", spec)
		}
	}
	for _, fields := range []map[string]any{
		{"credentials": caller, "secrets": []any{secret("ensemble-broker-env/old", "old")}},
		{"credentials": `{"github":{"app":"new","permissions":{"contents":"read"}}}`, "github": map[string]any{"app": "old", "permissions": map[string]string{"contents": "read"}}},
	} {
		if _, err := ParseManifest(withFields(t, fields)); err == nil {
			t.Fatal("accepted old/new conflict")
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
