package launcher

import (
	"encoding/json"
	"errors"
	"maps"
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
			if c.Valid && c.Expected.Stage != "manifest" {
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
	m, err := ParseManifest(fixtureBytes(t, "manifest-valid.json"))
	if err != nil || !m.HasHook || m.HookPath != "/usr/local/bin/ensemble-session" || m.Labels["team"] != "crew" {
		t.Fatalf("%+v %v", m, err)
	}
	base := `"client":"claude","generation":"0123456789abcdef0123456789abcdef","image":"ensemble-worker:` +
		"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" + `","cwd":"/w","workspace":"ensemble-ws-w","claude_dir":"/c","labels":{}`
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
	data := string(fixtureBytes(t, "manifest-valid.json"))
	m, err := ParseManifest([]byte(strings.Replace(data, `src/project"`, `src//project/."`, 1)))
	if err != nil || m.CWD != "/Users/example/src/project" {
		t.Fatalf("%q %v", m.CWD, err)
	}
}

func TestManifestLock(t *testing.T) {
	lockDoc := func(lock any) []byte { return withFields(t, map[string]any{"lock": lock, "lock_owner": "session 1"}) }
	for _, lock := range []string{"a", "0", "a.b-c_d", strings.Repeat("a", 63)} {
		if _, err := ParseManifest(lockDoc(lock)); err != nil {
			t.Errorf("%q: %v", lock, err)
		}
	}
	for _, lock := range []any{"", "A B", "A", "-a", "a/b", "a\n", strings.Repeat("a", 64), nil, 1} {
		if _, err := ParseManifest(lockDoc(lock)); code(t, err) != protocol.ManifestRejected {
			t.Errorf("accepted lock %v", lock)
		}
	}
	for _, label := range []string{"windroc.lock", "windroc.other", "windroc.egress.contract", "ensemble.lock"} {
		data := withFields(t, map[string]any{"labels": map[string]string{label: "x"}})
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
		data := fixtureBytes(t, "manifest-valid.json", edit)
		if _, err := ParseManifest(data); code(t, err) != protocol.ManifestRejected {
			t.Errorf("accepted %s", name)
		}
	}
}

func TestManifestWorkspace(t *testing.T) {
	data := fixtureBytes(t, "manifest-valid.json")
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

func withGitHubGrant(t *testing.T, fields map[string]any, grant any) {
	t.Helper()
	spec, err := json.Marshal(map[string]any{"github": grant})
	if err != nil {
		t.Fatal(err)
	}
	fields["credentials"] = string(spec)
}

func TestManifestGitHub(t *testing.T) {
	m, err := ParseManifest(fixtureBytes(t, "manifest-github-app.json"))
	if err != nil || m.GitHubApp != "ensemble-broker-bot" || !maps.Equal(m.GitHubPermissions, fixtureSet) {
		t.Fatalf("%+v %v", m, err)
	}
	for _, grant := range []any{nil, []any{}, "app", map[string]any{"app": "a"}, map[string]any{"app": "a", "permissions": nil}, map[string]any{"app": "a", "permissions": fixtureSet, "repo": "a/b/c"}} {
		_, err := ParseManifest(fixtureBytes(t, "manifest-valid.json", func(f map[string]any) { withGitHubGrant(t, f, grant) }))
		if code(t, err) != protocol.ManifestRejected || !strings.Contains(err.Error(), "github") {
			t.Fatalf("accepted %v: %v", grant, err)
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
		return ParseManifest(fixtureBytes(t, "manifest-valid.json", edit))
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
		// uppercase stands for the name rule; TestValidClaudeAccount has its other cases.
		"uppercase": withClaudeAccount("Work"), "number": withClaudeAccount(1), "null": withClaudeAccount(nil),
		"array": withClaudeAccount([]string{"work"}), "codex": codex, "opencode": opencode,
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
		if !ValidEnvName(name) || !ValidEnvItem("ensemble-broker-env/"+name) {
			t.Errorf("%q rejected", name)
		}
	}
	for _, name := range []string{"", "Dummy", ".a", "-a", "_a", "a/b", "a b", "a\n", strings.Repeat("a", 65), "é"} {
		if ValidEnvName(name) || ValidEnvItem("ensemble-broker-env/"+name) {
			t.Errorf("%q accepted", name)
		}
	}
	for _, item := range []string{"dummy", "ensemble-broker-env", "ensemble-broker-env/", "ensemble-broker-bot", "ensemble-broker-claude/default", "other/dummy", "ensemble-broker-env//dummy", "ensemble-env/dummy"} {
		if ValidEnvItem(item) {
			t.Errorf("item %q accepted", item)
		}
	}
}

func TestParseManifestSecrets(t *testing.T) {
	parse := func(edit func(map[string]any)) (Manifest, error) {
		return ParseManifest(fixtureBytes(t, "manifest-valid.json", edit))
	}
	if m, err := parse(nil); err != nil || m.Secrets != nil {
		t.Fatalf("absent: %+v %v", m.Secrets, err)
	}
	if m, err := parse(withSecrets([]any{})); err != nil || len(m.Secrets) != 0 {
		t.Fatalf("empty: %+v %v", m.Secrets, err)
	}
	two := []any{secret("ensemble-broker-env/dummy", "dummy.txt"), secret("ensemble-broker-env/dummy", "copy of dummy")}
	if m, err := parse(withSecrets(two)); err != nil || !slices.Equal(m.Secrets, []Secret{{"ensemble-broker-env/dummy", "dummy.txt"}, {"ensemble-broker-env/dummy", "copy of dummy"}}) {
		t.Fatalf("two: %+v %v", m.Secrets, err)
	}
	many := func(n int) []any {
		var list []any
		for i := range n {
			list = append(list, secret("ensemble-broker-env/s"+strconv.Itoa(i), "f"+strconv.Itoa(i)))
		}
		return list
	}
	if m, err := parse(withSecrets(many(maxSecrets))); err != nil || len(m.Secrets) != maxSecrets {
		t.Fatalf("%d: %v", maxSecrets, err)
	}
	for _, client := range []string{"codex", "opencode"} {
		edit := func(f map[string]any) {
			f["client"], f[client+"_dir"], f["secrets"] = client, "/x", []any{secret("ensemble-broker-env/dummy", "dummy.txt")}
			delete(f, "claude_dir")
		}
		if m, err := parse(edit); err != nil || len(m.Secrets) != 1 {
			t.Fatalf("%s: %v", client, err)
		}
	}
	// One invalid item stands for the item rule, whose other cases TestValidEnvName has.
	for name, secrets := range map[string]any{
		"too many":         many(maxSecrets + 1),
		"null":             nil,
		"object":           map[string]any{"item": "ensemble-broker-env/dummy", "file": "dummy.txt"},
		"entry string":     []any{"ensemble-broker-env/dummy"},
		"extra field":      []any{map[string]any{"item": "ensemble-broker-env/dummy", "file": "dummy.txt", "mode": "0644"}},
		"no file":          []any{map[string]any{"item": "ensemble-broker-env/dummy"}},
		"no item":          []any{map[string]any{"file": "dummy.txt"}},
		"item number":      []any{map[string]any{"item": 1, "file": "dummy.txt"}},
		"old env prefix":   []any{secret("ensemble-env/dummy", "dummy.txt")}, // protocol 30 renamed it
		"traversal":        []any{secret("ensemble-broker-env/dummy", "../github/token")},
		"slash":            []any{secret("ensemble-broker-env/dummy", "a/b")},
		"empty file":       []any{secret("ensemble-broker-env/dummy", "")},
		"dot":              []any{secret("ensemble-broker-env/dummy", ".")},
		"dot dot":          []any{secret("ensemble-broker-env/dummy", "..")},
		"newline":          []any{secret("ensemble-broker-env/dummy", "a\nb")},
		"delete":           []any{secret("ensemble-broker-env/dummy", "a\x7fb")},
		"c1 control":       []any{secret("ensemble-broker-env/dummy", "x\u009b31m")},
		"long file":        []any{secret("ensemble-broker-env/dummy", strings.Repeat("f", 256))},
		"repeated file":    []any{secret("ensemble-broker-env/a", "same"), secret("ensemble-broker-env/b", "same")},
		"file number":      []any{map[string]any{"item": "ensemble-broker-env/dummy", "file": 1}},
		"duplicate object": []any{secret("ensemble-broker-env/dummy", "dummy.txt"), secret("ensemble-broker-env/dummy", "dummy.txt")},
	} {
		t.Run(name, func(t *testing.T) {
			if m, err := parse(withSecrets(secrets)); code(t, err) != protocol.ManifestRejected {
				t.Fatalf("accepted: %+v", m.Secrets)
			}
		})
	}
	if _, err := parse(withSecrets([]any{secret("ensemble-broker-env/dummy", strings.Repeat("f", 255))})); err != nil {
		t.Fatalf("255-byte file: %v", err)
	}
	doc := `"secrets":[{"item":"ensemble-broker-env/dummy","file":"a","file":"b"}]`
	valid := string(fixtureBytes(t, "manifest-valid.json"))
	if _, err := ParseManifest([]byte(strings.Replace(valid, `"labels":`, doc+`, "labels":`, 1))); code(t, err) != protocol.ManifestRejected {
		t.Fatal("duplicate key accepted")
	}
}

// withFields is manifest-valid.json with fields set.
func withFields(t *testing.T, fields map[string]any) []byte {
	t.Helper()
	return fixtureBytes(t, "manifest-valid.json", func(doc map[string]any) { maps.Copy(doc, fields) })
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
	// One bad repo and one bad ref stand for their rules, whose other cases TestValidRepo (internal/github) and
	// TestValidRef have.
	for name, prefetch := range map[string]any{
		"null":        json.RawMessage("null"),
		"string":      "o/r",
		"empty":       map[string]any{},
		"extra":       map[string]any{"repo": "o/r", "depth": 1},
		"repo number": map[string]any{"repo": 1},
		"no owner":    map[string]any{"repo": "r"},
		"null ref":    map[string]any{"repo": "o/r", "ref": json.RawMessage("null")},
		"flag ref":    map[string]any{"repo": "o/r", "ref": "--upload-pack=x"},
		"ref number":  map[string]any{"repo": "o/r", "ref": 1},
	} {
		if _, err := ParseManifest(withFields(t, map[string]any{"prefetch": prefetch})); code(t, err) != protocol.ManifestRejected {
			t.Errorf("%s accepted", name)
		}
	}
	// A Linear keychain item is never an environment secret, prefetch or not.
	_, err = ParseManifest(withFields(t, map[string]any{"prefetch": map[string]any{"repo": "o/r"},
		"secrets": []any{map[string]any{"item": "linear.harness_key", "file": "linear"}}}))
	if code(t, err) != protocol.ManifestRejected {
		t.Fatalf("prefetch with a Linear item: %v", err)
	}
	github := map[string]any{"app": "ensemble-broker-bot", "permissions": map[string]any{"contents": "write"}}
	_, err = ParseManifest(withFields(t, map[string]any{"prefetch": map[string]any{"repo": "o/r"}, "credentials": githubSpec(t, github)}))
	if code(t, err) != protocol.ManifestRejected || err.(*protocol.Error).Message != "Manifest rejected: prefetch requires a github credential with repo or no github credential" {
		t.Fatalf("prefetch with github: %v", err)
	}
	// A repo grant, one repository's token, may sit beside prefetch.
	github["repo"] = "acme/out"
	m, err = ParseManifest(withFields(t, map[string]any{"prefetch": map[string]any{"repo": "o/r"}, "credentials": githubSpec(t, github)}))
	if err != nil || m.Prefetch == nil || m.GitHubApp != "ensemble-broker-bot" || m.GitHubRepo != "acme/out" {
		t.Fatalf("prefetch with a repo grant: %+v %v", m, err)
	}
}

func githubSpec(t *testing.T, grant any) string {
	t.Helper()
	f := map[string]any{}
	withGitHubGrant(t, f, grant)
	return f["credentials"].(string)
}
