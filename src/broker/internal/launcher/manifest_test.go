package launcher

import (
	"encoding/json"
	"errors"
	"maps"
	"os"
	"path/filepath"
	"slices"
	"strconv"
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

func withSecrets(secrets any) func(map[string]any) {
	return func(f map[string]any) { f["secrets"] = secrets }
}

func secret(item, file string) map[string]any { return map[string]any{"item": item, "file": file} }

func TestValidEnvName(t *testing.T) {
	for _, name := range []string{"dummy", "a", "0", "a.b-c_d", strings.Repeat("a", 64)} {
		if !ValidEnvName(name) || !ValidEnvItem("ensemble-env/"+name) {
			t.Errorf("%q rejected", name)
		}
	}
	for _, name := range []string{"", "Dummy", ".a", "-a", "_a", "a/b", "a b", "a\n", strings.Repeat("a", 65), "é"} {
		if ValidEnvName(name) || ValidEnvItem("ensemble-env/"+name) {
			t.Errorf("%q accepted", name)
		}
	}
	for _, item := range []string{"dummy", "ensemble-env", "ensemble-env/", "ensemble-bot", "ensemble-claude/default", "other/dummy", "ensemble-env//dummy"} {
		if ValidEnvItem(item) {
			t.Errorf("item %q accepted", item)
		}
	}
}

func TestParseManifestSecrets(t *testing.T) {
	parse := func(edit func(map[string]any)) (Manifest, error) {
		data, err := os.ReadFile(editedManifest(t, edit))
		if err != nil {
			t.Fatal(err)
		}
		return ParseManifest(data)
	}
	if m, err := parse(nil); err != nil || m.Secrets != nil {
		t.Fatalf("absent: %+v %v", m.Secrets, err)
	}
	if m, err := parse(withSecrets([]any{})); err != nil || len(m.Secrets) != 0 {
		t.Fatalf("empty: %+v %v", m.Secrets, err)
	}
	two := []any{secret("ensemble-env/dummy", "dummy.txt"), secret("ensemble-env/dummy", "copy of dummy")}
	if m, err := parse(withSecrets(two)); err != nil || !slices.Equal(m.Secrets, []Secret{{"ensemble-env/dummy", "dummy.txt"}, {"ensemble-env/dummy", "copy of dummy"}}) {
		t.Fatalf("two: %+v %v", m.Secrets, err)
	}
	many := func(n int) []any {
		var list []any
		for i := range n {
			list = append(list, secret("ensemble-env/s"+strconv.Itoa(i), "f"+strconv.Itoa(i)))
		}
		return list
	}
	if m, err := parse(withSecrets(many(maxSecrets))); err != nil || len(m.Secrets) != maxSecrets {
		t.Fatalf("%d: %v", maxSecrets, err)
	}
	for _, client := range []string{"codex", "opencode"} {
		edit := func(f map[string]any) {
			f["client"], f[client+"_dir"], f["secrets"] = client, "/x", []any{secret("ensemble-env/dummy", "dummy.txt")}
			delete(f, "claude_dir")
		}
		if m, err := parse(edit); err != nil || len(m.Secrets) != 1 {
			t.Fatalf("%s: %v", client, err)
		}
	}
	for name, secrets := range map[string]any{
		"too many":         many(maxSecrets + 1),
		"null":             nil,
		"object":           map[string]any{"item": "ensemble-env/dummy", "file": "dummy.txt"},
		"entry string":     []any{"ensemble-env/dummy"},
		"extra field":      []any{map[string]any{"item": "ensemble-env/dummy", "file": "dummy.txt", "mode": "0644"}},
		"no file":          []any{map[string]any{"item": "ensemble-env/dummy"}},
		"no item":          []any{map[string]any{"file": "dummy.txt"}},
		"item number":      []any{map[string]any{"item": 1, "file": "dummy.txt"}},
		"broker item":      []any{secret("ensemble-bot", "key")},
		"claude item":      []any{secret("ensemble-claude", "token")},
		"linear item":      []any{secret("linear.harness_key", "key")},
		"bare prefix":      []any{secret("ensemble-env/", "x")},
		"uppercase item":   []any{secret("ensemble-env/Dummy", "x")},
		"nested item":      []any{secret("ensemble-env/a/b", "x")},
		"traversal":        []any{secret("ensemble-env/dummy", "../github/token")},
		"slash":            []any{secret("ensemble-env/dummy", "a/b")},
		"empty file":       []any{secret("ensemble-env/dummy", "")},
		"dot":              []any{secret("ensemble-env/dummy", ".")},
		"dot dot":          []any{secret("ensemble-env/dummy", "..")},
		"newline":          []any{secret("ensemble-env/dummy", "a\nb")},
		"delete":           []any{secret("ensemble-env/dummy", "a\x7fb")},
		"c1 control":       []any{secret("ensemble-env/dummy", "x\u009b31m")},
		"long file":        []any{secret("ensemble-env/dummy", strings.Repeat("f", 256))},
		"repeated file":    []any{secret("ensemble-env/a", "same"), secret("ensemble-env/b", "same")},
		"file number":      []any{map[string]any{"item": "ensemble-env/dummy", "file": 1}},
		"duplicate object": []any{secret("ensemble-env/dummy", "dummy.txt"), secret("ensemble-env/dummy", "dummy.txt")},
	} {
		t.Run(name, func(t *testing.T) {
			if m, err := parse(withSecrets(secrets)); code(t, err) != protocol.ManifestRejected {
				t.Fatalf("accepted: %+v", m.Secrets)
			}
		})
	}
	if _, err := parse(withSecrets([]any{secret("ensemble-env/dummy", strings.Repeat("f", 255))})); err != nil {
		t.Fatalf("255-byte file: %v", err)
	}
	doc := `"secrets":[{"item":"ensemble-env/dummy","file":"a","file":"b"}]`
	valid, err := os.ReadFile(filepath.Join(fixtures.Dir(), "manifest-valid.json"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := ParseManifest([]byte(strings.Replace(string(valid), `"github": null`, `"github": null, `+doc, 1))); code(t, err) != protocol.ManifestRejected {
		t.Fatal("duplicate key accepted")
	}
}

// withFields is manifest-valid.json with fields set (nil deletes one).
func withFields(t *testing.T, fields map[string]any) []byte {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(fixtures.Dir(), "manifest-valid.json"))
	if err != nil {
		t.Fatal(err)
	}
	var doc map[string]any
	if err := json.Unmarshal(data, &doc); err != nil {
		t.Fatal(err)
	}
	for k, v := range fields {
		if v == nil {
			delete(doc, k)
			continue
		}
		doc[k] = v
	}
	out, err := json.Marshal(doc)
	if err != nil {
		t.Fatal(err)
	}
	return out
}

func TestManifestGateway(t *testing.T) {
	m, err := ParseManifest(withFields(t, nil))
	if err != nil || m.Gateway != "workers" {
		t.Fatalf("absent gateway: %q %v", m.Gateway, err)
	}
	for _, ok := range []string{"workers", "mini-op", "a", "a_b-9", strings.Repeat("a", 63)} {
		if m, err := ParseManifest(withFields(t, map[string]any{"gateway": ok})); err != nil || m.Gateway != ok {
			t.Errorf("%q: %q %v", ok, m.Gateway, err)
		}
	}
	for _, bad := range []any{"Nope", "", "a.b", "--x", "-a", "a b", "a\nb", strings.Repeat("a", 64), json.RawMessage("null"), 1, []string{"workers"}} {
		if _, err := ParseManifest(withFields(t, map[string]any{"gateway": bad})); code(t, err) != protocol.ManifestRejected {
			t.Errorf("gateway %v accepted", bad)
		}
	}
}

func TestValidRef(t *testing.T) {
	for _, ok := range []string{"main", "release/1.2", "v1.0.0", "feature/x-y_z", "a", "deadbee-x", "abcdef", strings.Repeat("a", 255), "x.locked", "a.b"} {
		if !ValidRef(ok) {
			t.Errorf("%q rejected", ok)
		}
	}
	for _, bad := range []string{"", "--upload-pack=x", "-x", "refs/pull/1/head", "refs/heads/main", "HEAD", "deadbeef", "DEADBEE",
		strings.Repeat("a", 40) + "0", "0123456789abcdef0123456789abcdef01234567", "a b", "a\nb", "a\tb", "a..b", "a.", "x.lock", "a/x.lock/b",
		"a//b", "/a", "a/", ".a", "a/.b", "a~1", "a^", "a:b", "a?", "a*", "a[", "a\\b", "a@{1}", "ü", strings.Repeat("a", 256)} {
		if ValidRef(bad) {
			t.Errorf("%q accepted", bad)
		}
	}
}

func TestManifestPrefetch(t *testing.T) {
	m, err := ParseManifest(withFields(t, nil))
	if err != nil || m.Prefetch != nil {
		t.Fatalf("absent prefetch: %+v %v", m.Prefetch, err)
	}
	m, err = ParseManifest(withFields(t, map[string]any{"prefetch": map[string]any{"repo": "windroc-labs/ensemble", "ref": "release/1.2"}}))
	if err != nil || m.Prefetch == nil || *m.Prefetch != (Prefetch{Repo: "windroc-labs/ensemble", Ref: "release/1.2"}) {
		t.Fatalf("%+v %v", m.Prefetch, err)
	}
	m, err = ParseManifest(withFields(t, map[string]any{"prefetch": map[string]any{"repo": "o/r.x"}}))
	if err != nil || m.Prefetch == nil || *m.Prefetch != (Prefetch{Repo: "o/r.x"}) {
		t.Fatalf("no ref: %+v %v", m.Prefetch, err)
	}
	for name, prefetch := range map[string]any{
		"null":        json.RawMessage("null"),
		"string":      "o/r",
		"empty":       map[string]any{},
		"extra":       map[string]any{"repo": "o/r", "depth": 1},
		"repo number": map[string]any{"repo": 1},
		"no owner":    map[string]any{"repo": "r"},
		"three parts": map[string]any{"repo": "o/r/x"},
		"dot":         map[string]any{"repo": "o/."},
		"dotdot":      map[string]any{"repo": "o/.."},
		"owner dot":   map[string]any{"repo": "./r"},
		"space":       map[string]any{"repo": "o/r x"},
		"empty ref":   map[string]any{"repo": "o/r", "ref": ""},
		"null ref":    map[string]any{"repo": "o/r", "ref": json.RawMessage("null")},
		"flag ref":    map[string]any{"repo": "o/r", "ref": "--upload-pack=x"},
		"pull ref":    map[string]any{"repo": "o/r", "ref": "refs/pull/1/head"},
		"sha ref":     map[string]any{"repo": "o/r", "ref": "0123456789abcdef0123456789abcdef01234567"},
		"short sha":   map[string]any{"repo": "o/r", "ref": "deadbeef"},
		"space ref":   map[string]any{"repo": "o/r", "ref": "a b"},
		"newline ref": map[string]any{"repo": "o/r", "ref": "a\nb"},
		"dotdot ref":  map[string]any{"repo": "o/r", "ref": "a..b"},
		"lock ref":    map[string]any{"repo": "o/r", "ref": "x.lock"},
		"ref number":  map[string]any{"repo": "o/r", "ref": 1},
	} {
		if _, err := ParseManifest(withFields(t, map[string]any{"prefetch": prefetch})); code(t, err) != protocol.ManifestRejected {
			t.Errorf("%s accepted", name)
		}
	}
	github := map[string]any{"app": "ensemble-bot", "permissions": map[string]any{"contents": "write"}}
	_, err = ParseManifest(withFields(t, map[string]any{"prefetch": map[string]any{"repo": "o/r"}, "github": github}))
	if code(t, err) != protocol.ManifestRejected || !strings.Contains(err.Error(), "prefetch requires github null") {
		t.Fatalf("prefetch with github: %v", err)
	}
}
