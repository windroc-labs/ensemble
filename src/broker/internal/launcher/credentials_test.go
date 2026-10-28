package launcher

import (
	"encoding/json"
	"fmt"
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
			valid := c.Valid && (c.Expected.ErrorCode == nil || *c.Expected.ErrorCode == "")
			if (problem == "") != valid {
				t.Fatalf("valid=%v problem=%q", valid, problem)
			}
		})
	}
}

func TestCredentialSizeLimits(t *testing.T) {
	check := func(value any, valid bool) {
		t.Helper()
		raw, err := json.Marshal(value)
		if err != nil {
			t.Fatal(err)
		}
		envelope, _ := json.Marshal(string(raw))
		if _, problem := parseCredentialEnvelope(envelope, credentialKinds); (problem == "") != valid {
			t.Fatalf("valid=%v problem=%q value=%s", valid, problem, raw)
		}
	}
	for _, count := range []int{0, 1, 16, 17} {
		items := make([]map[string]string, count)
		for i := range count {
			items[i] = map[string]string{"item": "ensemble-broker-env/a", "file": fmt.Sprintf("file-%d", i)}
		}
		check(map[string]any{"env": items}, count >= 1 && count <= 16)
	}
	for _, length := range []int{255, 256} {
		check(map[string]any{"env": []map[string]string{{"item": "ensemble-broker-env/a", "file": strings.Repeat("a", length)}}}, length == 255)
	}
}

func TestCredentialMerge(t *testing.T) {
	caller := ` { "env": [{"item":"ensemble-broker-env/caller","file":"caller"}] } `
	environment := `{"github":{"app":"bot","permissions":{"contents":"read"}},"env":[{"item":"ensemble-broker-env/env","file":"env"}]}`
	m, err := ParseManifest(withFields(t, map[string]any{"credentials": caller, "environment_credentials": environment}))
	if err != nil || len(m.Credentials) != 2 {
		t.Fatalf("%s %v", m.Credentials, err)
	}
	if !fixtures.SameJSON(t, m.Credentials["env"], []byte(`[{"item":"ensemble-broker-env/caller","file":"caller"}]`)) {
		t.Fatalf("caller must replace the entire env kind: %s", m.Credentials["env"])
	}
	if !fixtures.SameJSON(t, m.Credentials["github"], []byte(`{"app":"bot","permissions":{"contents":"read"}}`)) {
		t.Fatalf("unmatched environment kind must survive: %s", m.Credentials["github"])
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
