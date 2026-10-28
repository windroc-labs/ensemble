package launcher

import (
	"encoding/json"
	"maps"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"ensemble/broker/internal/fixtures"
	"ensemble/broker/internal/protocol"
)

func TestCredentialSpecFixtures(t *testing.T) {
	for _, c := range fixtures.Cases(t, "credential_spec") {
		t.Run(c.File, func(t *testing.T) {
			encoded, _ := json.Marshal(string(c.Data))
			_, problem := parseCredentialEnvelope(encoded, credentialKinds)
			if (problem == "") != c.Valid {
				t.Fatalf("valid=%v problem=%q", c.Valid, problem)
			}
		})
	}
}

func TestCredentialMerge(t *testing.T) {
	caller := ` { "env": [{"item":"ensemble-broker-env/caller","file":"caller"}] } `
	environment := `{"github":{"app":"bot","permissions":{"contents":"read"}},"env":[{"item":"ensemble-broker-env/env","file":"env"}]}`
	m, err := ParseManifest(withFields(t, map[string]any{"credentials": caller, "environment_credentials": environment}))
	if err != nil || len(m.Credentials) != 2 || !strings.Contains(string(m.Credentials["env"]), `"caller"`) || !strings.Contains(string(m.Credentials["github"]), `"bot"`) {
		t.Fatalf("%s %v", m.Credentials, err)
	}
	// Validate the environment independently, even when its kind would be replaced.
	_, err = ParseManifest(withFields(t, map[string]any{"credentials": caller, "environment_credentials": `{"env":false}`}))
	if code(t, err) != protocol.ManifestRejected || !strings.Contains(err.Error(), "environment_credentials.env") {
		t.Fatalf("overridden invalid environment: %v", err)
	}
}

func TestCredentialJSONFraming(t *testing.T) {
	for _, text := range []string{"", "null", "[]", `{} {}`, `{"env":false,"env":[]}`, `{"github":{"app":"a","app":"b"}}`, `{"env":"lin_api_secret"}`} {
		_, err := ParseManifest(withFields(t, map[string]any{"credentials": text}))
		if code(t, err) != protocol.ManifestRejected || strings.Contains(err.Error(), "lin_api_secret") {
			t.Fatalf("%q: %v", text, err)
		}
	}
	for _, text := range []string{" {} \n", `{ "github": {"app":"bot","permissions":{"contents":"read"}} }`} {
		if _, err := ParseManifest(withFields(t, map[string]any{"credentials": text})); err != nil {
			t.Fatalf("whitespace: %v", err)
		}
	}
	for _, raw := range []any{nil, 1, map[string]any{}} {
		if _, err := ParseManifest(withFields(t, map[string]any{"credentials": raw})); code(t, err) != protocol.ManifestRejected {
			t.Fatalf("non-envelope: %v", err)
		}
	}
}

func TestFixtureKindNeedsOnlyBrokerExtension(t *testing.T) {
	data, err := os.ReadFile(filepath.Join(fixtures.Dir(), "credential-spec-test-kind.json"))
	if err != nil {
		t.Fatal(err)
	}
	var fixture struct {
		Credentials string `json:"credentials"`
		Environment string `json:"environment_credentials"`
	}
	if err := json.Unmarshal(data, &fixture); err != nil {
		t.Fatal(err)
	}
	kinds := maps.Clone(credentialKinds)
	kinds["fixture_test"] = func(raw json.RawMessage) string {
		fields, ok := object(raw)
		file, valid := str(fields["file"])
		if !ok || !onlyKeys(fields, "file") || !valid || !validSecretFile(file) {
			return "file not valid"
		}
		return ""
	}
	for _, text := range []string{fixture.Credentials, fixture.Environment} {
		encoded, _ := json.Marshal(text)
		if _, problem := parseCredentialEnvelope(encoded, kinds); problem != "" {
			t.Fatal(problem)
		}
		if _, problem := parseCredentialEnvelope(encoded, credentialKinds); !strings.Contains(problem, "fixture_test") {
			t.Fatalf("production must refuse fixture_test: %q", problem)
		}
	}
}
