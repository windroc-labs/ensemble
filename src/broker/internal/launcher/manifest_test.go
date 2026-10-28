package launcher

import (
	"encoding/json"
	"errors"
	"maps"
	"os"
	"path/filepath"
	"reflect"
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
	m, err := ParseManifest(fixtureBytes(t, "manifest-valid.json"))
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

func TestManifestGitHub(t *testing.T) {
	data, valid := fixtureBytes(t, "manifest-github-app.json"), fixtureBytes(t, "manifest-valid.json")
	if m, err := ParseManifest(data); err != nil || m.GitHubApp != "ensemble-broker-bot" || !maps.Equal(m.GitHubPermissions, fixtureSet) {
		t.Fatalf("%+v %v", m, err)
	}
	if m, err := ParseManifest(valid); err != nil || m.GitHubApp != "" || m.GitHubPermissions != nil {
		t.Fatalf("%+v %v", m, err)
	}
	const set = `"permissions":{"contents":"write"}`
	for _, github := range []string{`[]`, `"ensemble-broker-bot"`, `{"app":null,` + set + `}`, `{"app":"a","app":"b",` + set + `}`, `{"app":"-a",` + set + `}`,
		`{"app":"a"}`, `{"app":"a","permissions":null}`, `{"app":"a","permissions":[]}`, `{"app":"a","permissions":{"contents":1}}`,
		`{"app":"a",` + set + `,"owner":"acme"}`, `{"app":"a",` + set + `,"repo":"acme/x","ref":"main"}`, `{"app":"a",` + set + `,"repo":null}`} {
		doc := strings.Replace(string(valid), `"github": null`, `"github": `+github, 1)
		if _, err := ParseManifest([]byte(doc)); code(t, err) != protocol.ManifestRejected {
			t.Errorf("%s accepted", github)
		}
	}
	doc := strings.Replace(string(valid), `"github": null`, `"github": {"app":"a",`+set+`,"repo":"acme/x"}`, 1)
	if m, err := ParseManifest([]byte(doc)); err != nil || m.GitHubApp != "a" || m.GitHubRepo != "acme/x" {
		t.Fatalf("repo grant: %+v %v", m, err)
	}
	if m, err := ParseManifest(data); err != nil || m.GitHubRepo != "" {
		t.Fatalf("no repo: %+v %v", m, err)
	}
	// The broker names the repo or permission it rejects; ValidRepo and CheckPermissions have the other cases.
	for github, want := range map[string]string{
		`{"app":"a",` + set + `,"repo":"acme/x/y"}`:                   `Manifest rejected: invalid github repo "acme/x/y"`,
		`{"app":"a","permissions":{"contents":"write","foo":"read"}}`: `Manifest rejected: unknown GitHub permission "foo"`,
	} {
		_, err := ParseManifest([]byte(strings.Replace(string(valid), `"github": null`, `"github": `+github, 1)))
		if code(t, err) != protocol.ManifestRejected || err.(*protocol.Error).Message != want {
			t.Errorf("%s: %v, want %q", github, err, want)
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
	if _, err := ParseManifest([]byte(strings.Replace(valid, `"github": null`, `"github": null, `+doc, 1))); code(t, err) != protocol.ManifestRejected {
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
	_, err = ParseManifest(withFields(t, map[string]any{"prefetch": map[string]any{"repo": "o/r"}, "github": github}))
	if code(t, err) != protocol.ManifestRejected || err.(*protocol.Error).Message != "Manifest rejected: prefetch requires github null or a github repo" {
		t.Fatalf("prefetch with github: %v", err)
	}
	// A repo grant, one repository's token, may sit beside prefetch.
	github["repo"] = "acme/out"
	m, err = ParseManifest(withFields(t, map[string]any{"prefetch": map[string]any{"repo": "o/r"}, "github": github}))
	if err != nil || m.Prefetch == nil || m.GitHubApp != "ensemble-broker-bot" || m.GitHubRepo != "acme/out" {
		t.Fatalf("prefetch with a repo grant: %+v %v", m, err)
	}
}

func TestCredentialsParse(t *testing.T) {
	data, err := os.ReadFile(filepath.Join(fixtures.Dir(), "manifest-credentials.json"))
	if err != nil {
		t.Fatal(err)
	}
	m, err := ParseManifest(data)
	want := &CredentialRequest{
		GitHub: []Grant{
			{App: "ensemble-broker-bot", Permissions: map[string]string{"contents": "write", "pull_requests": "write"}, Repo: "windroc-labs/ensemble"},
			{App: "ensemble-broker-bot", Permissions: map[string]string{"contents": "write"}, Repo: "windroc/private_docs"},
		},
		Linear: &Linear{Role: "engineer"},
		Env:    []Secret{{"ensemble-broker-env/mini-ssh", "id_ed25519"}, {"ensemble-broker-env/mini-password", "mini-password"}},
	}
	if err != nil || !reflect.DeepEqual(m.Credentials, want) || !slices.Equal(m.Credentials.Kinds(), []string{"github", "linear", "env"}) {
		t.Fatalf("%+v %v", m.Credentials, err)
	}
	if m, err := ParseManifest(withFields(t, nil)); err != nil || m.Credentials != nil {
		t.Fatalf("absent: %+v %v", m.Credentials, err)
	}
	if m, err := ParseManifest(withFields(t, map[string]any{"credentials": map[string]any{}})); err != nil || m.Credentials == nil || len(m.Credentials.Kinds()) != 0 {
		t.Fatalf("empty: %+v %v", m.Credentials, err)
	}

	// Every rejected value or key below holds leak; no message may repeat it.
	const leak = "lin_api_leak"
	grant := func(fields map[string]any) map[string]any {
		g := map[string]any{"app": "ensemble-broker-bot", "permissions": map[string]any{"contents": "write"}, "repo": "acme/x"}
		for k, v := range fields {
			if v == nil {
				delete(g, k)
				continue
			}
			g[k] = v
		}
		return g
	}
	grants := func(n int) []any {
		var list []any
		for i := range n {
			list = append(list, grant(map[string]any{"repo": "acme/r" + strconv.Itoa(i)}))
		}
		return list
	}
	env := func(n int) []any {
		var list []any
		for i := range n {
			list = append(list, secret("ensemble-broker-env/s"+strconv.Itoa(i), "f"+strconv.Itoa(i)))
		}
		return list
	}
	if m, err := ParseManifest(withFields(t, map[string]any{"credentials": map[string]any{"github": grants(16), "env": env(16)}})); err != nil || len(m.Credentials.GitHub) != 16 || len(m.Credentials.Env) != 16 {
		t.Fatalf("16: %v", err)
	}
	if m, err := ParseManifest(withFields(t, map[string]any{"credentials": map[string]any{"github": []any{grant(map[string]any{"repo": nil})}}})); err != nil || m.Credentials.GitHub[0].Repo != "" {
		t.Fatalf("one grant without repo: %+v %v", m.Credentials, err)
	}
	legacy := map[string]any{"app": "ensemble-broker-bot", "permissions": map[string]any{"contents": "write"}}
	linear := map[string]any{"linear": map[string]any{"role": "engineer"}}
	for name, fields := range map[string]map[string]any{
		"linear beside legacy github": {"credentials": linear, "github": legacy},
		"linear beside prefetch":      {"credentials": linear, "prefetch": map[string]any{"repo": "o/r"}},
		"repo grant beside prefetch":  {"credentials": map[string]any{"github": grants(1)}, "prefetch": map[string]any{"repo": "o/r"}},
		"env beside no secrets":       {"credentials": map[string]any{"env": env(1)}, "secrets": nil},
	} {
		if _, err := ParseManifest(withFields(t, fields)); err != nil {
			t.Errorf("%s: %v", name, err)
		}
	}

	for _, c := range []struct {
		fields map[string]any
		want   string
	}{
		{map[string]any{"credentials": leak}, "credentials: want an object"},
		{map[string]any{"credentials": []any{leak}}, "credentials: want an object"},
		{map[string]any{"credentials": map[string]any{leak: map[string]any{}}}, "credentials: an unknown kind"},
		{map[string]any{"credentials": map[string]any{"github": leak}}, "credentials.github: want 1 to 16 grants"},
		{map[string]any{"credentials": map[string]any{"github": []any{}}}, "credentials.github: want 1 to 16 grants"},
		{map[string]any{"credentials": map[string]any{"github": grants(17)}}, "credentials.github: want 1 to 16 grants"},
		{map[string]any{"credentials": map[string]any{"github": []any{leak}}}, "credentials.github[0]: want an object"},
		{map[string]any{"credentials": map[string]any{"github": []any{grant(map[string]any{leak: "x"})}}}, "credentials.github[0]: an unknown key"},
		{map[string]any{"credentials": map[string]any{"github": []any{grant(map[string]any{"app": nil})}}}, "credentials.github[0]: app missing"},
		{map[string]any{"credentials": map[string]any{"github": []any{grant(map[string]any{"app": leak})}}}, "credentials.github[0]: app not valid"},
		{map[string]any{"credentials": map[string]any{"github": []any{grant(map[string]any{"permissions": nil})}}}, "credentials.github[0]: permissions missing"},
		{map[string]any{"credentials": map[string]any{"github": []any{grant(map[string]any{"permissions": leak})}}}, "credentials.github[0]: permissions not valid"},
		{map[string]any{"credentials": map[string]any{"github": []any{grant(map[string]any{"permissions": map[string]any{}})}}}, "credentials.github[0]: permissions not valid"},
		{map[string]any{"credentials": map[string]any{"github": []any{grant(map[string]any{"permissions": map[string]any{"contents": 1}})}}}, "credentials.github[0]: permissions not valid"},
		{map[string]any{"credentials": map[string]any{"github": []any{grant(map[string]any{"permissions": map[string]any{leak: "read"}})}}}, "credentials.github[0]: an unknown key"},
		{map[string]any{"credentials": map[string]any{"github": []any{grant(map[string]any{"permissions": map[string]any{"contents": leak}})}}}, "credentials.github[0]: contents not valid"},
		{map[string]any{"credentials": map[string]any{"github": []any{grant(map[string]any{"permissions": map[string]any{"metadata": "write"}})}}}, "credentials.github[0]: metadata not valid"},
		{map[string]any{"credentials": map[string]any{"github": []any{grant(map[string]any{"permissions": map[string]any{"workflows": "read"}})}}}, "credentials.github[0]: workflows not valid"},
		{map[string]any{"credentials": map[string]any{"github": []any{grant(map[string]any{"repo": leak})}}}, "credentials.github[0]: repo not valid"},
		{map[string]any{"credentials": map[string]any{"github": []any{grant(map[string]any{"repo": "acme/.."})}}}, "credentials.github[0]: repo not valid"},
		{map[string]any{"credentials": map[string]any{"github": []any{grant(map[string]any{"repo": 1})}}}, "credentials.github[0]: repo not valid"},
		{map[string]any{"credentials": map[string]any{"github": []any{grant(map[string]any{"repo": nil}), grant(map[string]any{"repo": nil})}}}, "credentials.github[0]: repo missing beside another grant"},
		{map[string]any{"credentials": map[string]any{"github": []any{grant(nil), grant(map[string]any{"repo": nil})}}}, "credentials.github[1]: repo missing beside another grant"},
		{map[string]any{"credentials": map[string]any{"github": []any{grant(nil), grant(map[string]any{"permissions": map[string]any{"issues": "read"}})}}}, "credentials.github[1]: repo repeated"},
		{map[string]any{"credentials": map[string]any{"linear": leak}}, "credentials.linear: want an object"},
		{map[string]any{"credentials": map[string]any{"linear": map[string]any{"role": "engineer", "token": leak}}}, "credentials.linear: an unknown key"},
		{map[string]any{"credentials": map[string]any{"linear": map[string]any{}}}, "credentials.linear: role missing"},
		{map[string]any{"credentials": map[string]any{"linear": map[string]any{"role": leak}}}, "credentials.linear: role not valid"},
		{map[string]any{"credentials": map[string]any{"linear": map[string]any{"role": "9" + strings.Repeat("a", 10)}}}, "credentials.linear: role not valid"},
		{map[string]any{"credentials": map[string]any{"env": leak}}, "credentials.env: want 1 to 16 secrets"},
		{map[string]any{"credentials": map[string]any{"env": []any{}}}, "credentials.env: want 1 to 16 secrets"},
		{map[string]any{"credentials": map[string]any{"env": env(17)}}, "credentials.env: want 1 to 16 secrets"},
		{map[string]any{"credentials": map[string]any{"env": []any{leak}}}, "credentials.env[0]: want an object"},
		{map[string]any{"credentials": map[string]any{"env": []any{map[string]any{"item": "ensemble-broker-env/a", "file": "a", leak: "x"}}}}, "credentials.env[0]: an unknown key"},
		{map[string]any{"credentials": map[string]any{"env": []any{map[string]any{"file": "a"}}}}, "credentials.env[0]: item missing"},
		{map[string]any{"credentials": map[string]any{"env": []any{map[string]any{"item": "ensemble-broker-env/a"}}}}, "credentials.env[0]: file missing"},
		{map[string]any{"credentials": map[string]any{"env": []any{secret(leak, "a")}}}, "credentials.env[0]: item not valid"},
		{map[string]any{"credentials": map[string]any{"env": []any{secret("ensemble-broker-env/a", leak+"/a")}}}, "credentials.env[0]: file not valid"},
		{map[string]any{"credentials": map[string]any{"env": []any{secret("ensemble-broker-env/a", leak), secret("ensemble-broker-env/b", leak)}}}, "credentials.env[1]: file repeated"},
		{map[string]any{"credentials": map[string]any{"github": grants(1)}, "github": legacy}, "credentials.github: needs github null"},
		{map[string]any{"credentials": map[string]any{"env": env(1)}, "secrets": []any{}}, "credentials.env: needs no secrets"},
		{map[string]any{"credentials": map[string]any{"github": []any{grant(map[string]any{"repo": nil})}}, "prefetch": map[string]any{"repo": "o/r"}}, "credentials.github[0]: repo missing beside prefetch"},
	} {
		_, err := ParseManifest(withFields(t, c.fields))
		if code(t, err) != protocol.ManifestRejected || err.(*protocol.Error).Message != "Manifest rejected: "+c.want || strings.Contains(err.Error(), leak) {
			t.Errorf("%v: %v, want %q", c.fields, err, c.want)
		}
	}
}
