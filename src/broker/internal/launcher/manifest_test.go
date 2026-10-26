package launcher

import (
	"errors"
	"maps"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"ensemble/broker/internal/fixtures"
	"ensemble/broker/internal/protocol"
)

func code(t *testing.T, err error) protocol.Code {
	t.Helper()
	var e *protocol.Error
	if err == nil {
		return ""
	}
	if !errors.As(err, &e) {
		t.Fatalf("not a protocol error: %v", err)
	}
	return e.Code
}

func TestManifestFixtures(t *testing.T) {
	for _, c := range fixtures.Cases(t, "manifest") {
		t.Run(c.File, func(t *testing.T) {
			m, err := ParseManifest(c.Data)
			if c.Valid {
				if err != nil || m.Name == "" || m.Labels == nil {
					t.Fatalf("%+v %v", m, err)
				}
				return
			}
			if code(t, err) != protocol.ManifestRejected {
				t.Fatalf("accepted: %+v %v", m, err)
			}
		})
	}
}

func TestManifestDuplicatesTrailingUTF8(t *testing.T) {
	valid, err := os.ReadFile(filepath.Join(fixtures.Dir(), "manifest-valid.json"))
	if err != nil {
		t.Fatal(err)
	}
	m, err := ParseManifest(valid)
	if err != nil || !m.HasHook || m.HookPath != "/usr/local/bin/ensemble-session" || m.Labels["team"] != "crew" {
		t.Fatalf("%+v %v", m, err)
	}
	base := `"client":"claude","generation":"0123456789abcdef0123456789abcdef","image":"ensemble-worker:` +
		"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" + `","cwd":"/w","workspace":"ensemble-ws-w","claude_dir":"/c","github":null,"labels":{}`
	if _, err := ParseManifest([]byte(`{"name":"w",` + base + `}`)); err != nil {
		t.Fatal(err)
	}
	for name, doc := range map[string]string{
		"duplicate":       `{"name":"w","name":"v",` + base + `}`,
		"duplicate label": `{"name":"w",` + base[:len(base)-2] + `{"a":"1","a":"2"}}`,
		"trailing":        `{"name":"w",` + base + `} {}`,
		"invalid utf8":    `{"name":"w",` + base[:len(base)-len(`"labels":{}`)] + `"labels":{"a":"` + "\xff" + `"}}`,
		"empty":           ``,
		"null label":      `{"name":"w",` + base[:len(base)-2] + `{"a":null}}`,
		"none name":       `{"name":"none",` + base + `}`,
		"nan":             `{"name":"w",` + base + `,"x":NaN}`,
	} {
		if _, err := ParseManifest([]byte(doc)); code(t, err) != protocol.ManifestRejected {
			t.Errorf("%s accepted", name)
		}
	}
}

func TestManifestCleansCWD(t *testing.T) {
	data, err := os.ReadFile(filepath.Join(fixtures.Dir(), "manifest-valid.json"))
	if err != nil {
		t.Fatal(err)
	}
	m, err := ParseManifest([]byte(strings.Replace(string(data), `src/project"`, `src//project/."`, 1)))
	if err != nil || m.CWD != "/Users/example/src/project" {
		t.Fatalf("%q %v", m.CWD, err)
	}
}

func TestManifestLock(t *testing.T) {
	for _, lock := range []string{"a", "0", "a.b-c_d", strings.Repeat("a", 63)} {
		path := editedManifest(t, func(f map[string]any) { f["lock"], f["lock_owner"] = lock, "session 1" })
		data, _ := os.ReadFile(path)
		if _, err := ParseManifest(data); err != nil {
			t.Errorf("%q: %v", lock, err)
		}
	}
	for _, lock := range []any{"", "A B", "A", "-a", "a/b", "a\n", strings.Repeat("a", 64), nil, 1} {
		path := editedManifest(t, func(f map[string]any) { f["lock"], f["lock_owner"] = lock, "session 1" })
		data, _ := os.ReadFile(path)
		if _, err := ParseManifest(data); code(t, err) != protocol.ManifestRejected {
			t.Errorf("accepted lock %v", lock)
		}
	}
	for _, label := range []string{"windroc.lock", "windroc.other", "windroc.egress.contract", "ensemble.lock"} {
		path := editedManifest(t, func(f map[string]any) { f["labels"] = map[string]string{label: "x"} })
		data, _ := os.ReadFile(path)
		if _, err := ParseManifest(data); code(t, err) != protocol.ManifestRejected {
			t.Errorf("accepted reserved label %q", label)
		}
	}
	for name, edit := range map[string]func(map[string]any){
		"missing owner": func(f map[string]any) { f["lock"] = "x" },
		"orphan owner":  func(f map[string]any) { f["lock_owner"] = "session" },
		"empty owner":   func(f map[string]any) { f["lock"], f["lock_owner"] = "x", "" },
		"control owner": func(f map[string]any) { f["lock"], f["lock_owner"] = "x", "a\n" },
	} {
		data, _ := os.ReadFile(editedManifest(t, edit))
		if _, err := ParseManifest(data); code(t, err) != protocol.ManifestRejected {
			t.Errorf("accepted %s", name)
		}
	}
}

func TestManifestWorkspace(t *testing.T) {
	data, err := os.ReadFile(filepath.Join(fixtures.Dir(), "manifest-valid.json"))
	if err != nil {
		t.Fatal(err)
	}
	with := func(workspace string) (Manifest, error) {
		return ParseManifest([]byte(strings.Replace(string(data), `"ensemble-ws-worker_1"`, workspace, 1)))
	}
	if m, err := with(`"ensemble-ws-worker_1"`); err != nil || m.Workspace != "ensemble-ws-worker_1" {
		t.Fatalf("%+v %v", m, err)
	}
	for _, workspace := range []string{`"ensemble-ws-other"`, `"ensemble-ws-worker_1x"`, `"ensemble-crew-worker_1"`, `"ensemble-run-TASK-633-0123abcd"`,
		`"volume"`, `""`, `1`, `null`} {
		if _, err := with(workspace); code(t, err) != protocol.ManifestRejected {
			t.Errorf("%s accepted", workspace)
		}
	}
}

func TestManifestGitHub(t *testing.T) {
	data, err := os.ReadFile(filepath.Join(fixtures.Dir(), "manifest-github-app.json"))
	if err != nil {
		t.Fatal(err)
	}
	if m, err := ParseManifest(data); err != nil || m.GitHubApp != "ensemble-bot" || !maps.Equal(m.GitHubPermissions, fixtureSet) {
		t.Fatalf("%+v %v", m, err)
	}
	valid, err := os.ReadFile(filepath.Join(fixtures.Dir(), "manifest-valid.json"))
	if err != nil {
		t.Fatal(err)
	}
	if m, err := ParseManifest(valid); err != nil || m.GitHubApp != "" || m.GitHubPermissions != nil {
		t.Fatalf("%+v %v", m, err)
	}
	const set = `"permissions":{"contents":"write"}`
	for _, github := range []string{`[]`, `"ensemble-bot"`, `{"app":null,` + set + `}`, `{"app":"a","app":"b",` + set + `}`, `{"app":"-a",` + set + `}`,
		`{"app":"a"}`, `{"app":"a","permissions":null}`, `{"app":"a","permissions":[]}`, `{"app":"a","permissions":{"contents":1}}`,
		`{"app":"a",` + set + `,"repo":"acme/x"}`} {
		doc := strings.Replace(string(valid), `"github": null`, `"github": `+github, 1)
		if _, err := ParseManifest([]byte(doc)); code(t, err) != protocol.ManifestRejected {
			t.Errorf("%s accepted", github)
		}
	}
	// The broker names the entry it rejects, so the start fails with a clear error.
	for permissions, want := range map[string]string{
		`{}`:                                "Manifest rejected: no GitHub permissions",
		`{"contents":"write","foo":"read"}`: `Manifest rejected: unknown GitHub permission "foo"`,
		`{"contents":"admin"}`:              `Manifest rejected: GitHub permission contents: level "admin", want read or write`,
		`{"workflows":"read"}`:              `Manifest rejected: GitHub permission workflows: level "read", want write`,
	} {
		doc := strings.Replace(string(valid), `"github": null`, `"github": {"app":"a","permissions":`+permissions+`}`, 1)
		_, err := ParseManifest([]byte(doc))
		if code(t, err) != protocol.ManifestRejected || err.(*protocol.Error).Message != want {
			t.Errorf("%s: %v, want %q", permissions, err, want)
		}
	}
}

func TestValidClaudeAccount(t *testing.T) {
	for _, name := range []string{"default", "work", "a", "0", "a.b-c_d", strings.Repeat("a", 64)} {
		if !ValidClaudeAccount(name) {
			t.Errorf("%q rejected", name)
		}
	}
	for _, name := range []string{"", "Work", "setup-token", ".a", "-a", "_a", "a/b", "a b", "a\n", strings.Repeat("a", 65), "é"} {
		if ValidClaudeAccount(name) {
			t.Errorf("%q accepted", name)
		}
	}
}

func TestManifestClaudeAccount(t *testing.T) {
	parse := func(edit func(map[string]any)) (Manifest, error) {
		data, err := os.ReadFile(editedManifest(t, edit))
		if err != nil {
			t.Fatal(err)
		}
		return ParseManifest(data)
	}
	for want, edit := range map[string]func(map[string]any){"default": nil, "work": withClaudeAccount("work"), "a.b-c_d": withClaudeAccount("a.b-c_d")} {
		if m, err := parse(edit); err != nil || m.ClaudeAccount != want {
			t.Fatalf("%s: %q %v", want, m.ClaudeAccount, err)
		}
	}
	codex := func(f map[string]any) {
		f["client"], f["codex_dir"], f["claude_account"] = "codex", "/c", "work"
		delete(f, "claude_dir")
	}
	opencode := func(f map[string]any) {
		f["client"], f["opencode_dir"], f["claude_account"] = "opencode", "/o", "default"
		delete(f, "claude_dir")
	}
	for name, edit := range map[string]func(map[string]any){
		"uppercase": withClaudeAccount("Work"), "setup-token": withClaudeAccount("setup-token"), "empty": withClaudeAccount(""),
		"path": withClaudeAccount("../a"), "too long": withClaudeAccount(strings.Repeat("a", 65)), "control": withClaudeAccount("a\n"),
		"number": withClaudeAccount(1), "null": withClaudeAccount(nil), "array": withClaudeAccount([]string{"work"}),
		"codex": codex, "opencode": opencode,
	} {
		m, err := parse(edit)
		if code(t, err) != protocol.ManifestRejected {
			t.Fatalf("%s accepted: %+v", name, m)
		}
		if want := map[bool]string{true: "claude_account requires client claude", false: "invalid claude_account"}[name == "codex" || name == "opencode"]; !strings.Contains(err.Error(), want) {
			t.Fatalf("%s: %v", name, err)
		}
	}
}
