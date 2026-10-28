package launcher

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"testing"

	"ensemble/broker/internal/credential"
	"ensemble/broker/internal/protocol"
)

// fakeStatic is the static store: each Load returns the next of loads (the last again once used up), or err.
type fakeStatic struct {
	loads []map[string]credential.Static
	err   error
	calls int
}

func (f *fakeStatic) Load() (map[string]credential.Static, error) {
	f.calls++
	if f.err != nil {
		return nil, f.err
	}
	if len(f.loads) == 0 {
		return map[string]credential.Static{}, nil
	}
	i := min(f.calls, len(f.loads)) - 1
	return f.loads[i], nil
}

func records(pairs ...any) map[string]credential.Static {
	m := map[string]credential.Static{}
	for i := 0; i < len(pairs); i += 2 {
		m[pairs[i].(string)] = pairs[i+1].(credential.Static)
	}
	return m
}

var expired = credential.Static{Version: 2, ExpiresAt: "1970-01-01T00:00:00Z", Provenance: credential.Explicit}

// staticKinds are a manifest edit and a host change for each static secret a start reads.
var staticKinds = []struct {
	ref   string
	edit  func(map[string]any)
	setup func(*Host)
}{
	{"ensemble-broker-claude/default", nil, func(*Host) {}},
	{"ensemble-broker-claude/work", withClaudeAccount("work"), func(*Host) {}},
	{"ensemble-broker-opencode/strata", func(f map[string]any) {
		f["client"], f["opencode_dir"], f["opencode_providers"] = "opencode", strings.TrimSuffix(f["claude_dir"].(string), "claude")+"opencode", []string{"strata"}
		delete(f, "claude_dir")
	}, func(h *Host) { h.Opencode = &fakeKeys{} }},
	{"ensemble-broker-env/dummy", withSecrets([]any{secret("ensemble-broker-env/dummy", "dummy.txt")}), func(h *Host) { h.Env = &fakeEnv{} }},
}

func TestStartRefusesExpiredStatic(t *testing.T) {
	for _, kind := range staticKinds {
		for _, held := range []bool{true, false} {
			w := newLockWorld()
			h, base, d := w.host(t, "worker_1", 1000)
			if held {
				holdStartLock(t, h.Home)
				w.add("ensemble-other-1", "running", map[string]string{"windroc.lock": "dummy", "windroc.lock.owner": "other"})
			}
			kind.setup(&h)
			h.Static = &fakeStatic{loads: []map[string]credential.Static{records(kind.ref, expired)}}
			raw, err := os.ReadFile(base)
			if err != nil {
				t.Fatal(err)
			}
			var fields map[string]any
			if err := json.Unmarshal(raw, &fields); err != nil {
				t.Fatal(err)
			}
			if kind.edit != nil {
				kind.edit(fields)
			}
			raw, _ = json.Marshal(fields)
			_, err = h.Start(context.Background(), writeManifest(t, raw))
			e := protoErr(t, err)
			login := credential.LoginCommand(kind.ref)
			if e.Code != protocol.LoginRequired || protocol.Exit(e.Code) != 5 || e.Holder != nil || !strings.Contains(e.Message, kind.ref+" expired 1970-01-01") || !strings.Contains(e.Message, login) {
				t.Fatalf("%s held=%v: %+v", kind.ref, held, e)
			}
			for _, c := range d.calls {
				if line := strings.Join(c, " "); c[0] == "create" || c[0] == "run" || c[0] == "volume" || strings.Contains(line, "ensemble-worker_1-") || strings.Contains(line, "windroc.lock") {
					t.Fatalf("%s held=%v: docker %q", kind.ref, held, c)
				}
			}
		}
	}
}

// staticStartHost is a Claude start host with a real state directory and static store fake.
func staticStartHost(t *testing.T, static *fakeStatic) (Host, string, string, *fakeDocker) {
	t.Helper()
	h, d := startHost(&world{labels: goodLabels()}, &fakeCreds{})
	h.Home = t.TempDir()
	h.Env = &fakeEnv{}
	if static != nil {
		h.Static = static
	}
	parent := filepath.Join(h.Home, ".ensemble/docker-state", "worker_1")
	if err := os.MkdirAll(parent, 0o700); err != nil {
		t.Fatal(err)
	}
	path := editedManifest(t, func(f map[string]any) {
		f["claude_dir"] = filepath.Join(parent, "claude")
		f["secrets"] = []any{secret("ensemble-broker-env/dummy", "dummy.txt"), secret("ensemble-broker-env/ssh", "id_ed25519")}
	})
	return h, path, filepath.Join(parent, "0123456789abcdef0123456789abcdef.static.json"), d
}

func TestStartRecordsInjectedVersions(t *testing.T) {
	future := credential.Static{Version: 3, ExpiresAt: "2099-01-01T00:00:00Z", Provenance: credential.Assumed, Notified: []string{"30d"}}
	static := &fakeStatic{loads: []map[string]credential.Static{
		records("ensemble-broker-claude/default", future),
		records("ensemble-broker-claude/default", future, "ensemble-broker-env/dummy", credential.Static{Version: 2, Provenance: credential.Unknown},
			"ensemble-broker-env/ssh", credential.Static{Version: 5, Provenance: credential.Unknown, Pending: true}),
	}}
	h, path, record, _ := staticStartHost(t, static)
	if _, err := h.Start(context.Background(), path); err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(record)
	if err != nil {
		t.Fatal(err)
	}
	var got injectedRecord
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Fatal(err)
	}
	want := injectedRecord{Injected: records(
		"ensemble-broker-claude/default", credential.Static{Version: 3, ExpiresAt: "2099-01-01T00:00:00Z", Provenance: credential.Assumed},
		"ensemble-broker-env/dummy", credential.Static{Version: 2, Provenance: credential.Unknown},
		"ensemble-broker-env/ssh", credential.Static{Version: 5, Provenance: credential.Unknown},
	), Delivered: []string{}}
	if static.calls != 2 || !reflect.DeepEqual(got, want) {
		t.Fatalf("loads %d: %s", static.calls, raw)
	}
	if info, err := os.Stat(record); err != nil || info.Mode().Perm() != 0o600 {
		t.Fatalf("%v %v", info, err)
	}
}

func TestStartUnknownExpiryStarts(t *testing.T) {
	h, path, record, _ := staticStartHost(t, &fakeStatic{})
	if _, err := h.Start(context.Background(), path); err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(record)
	if err != nil || !strings.Contains(string(raw), `"ensemble-broker-claude/default":{"version":0,"provenance":"unknown"}`) {
		t.Fatalf("%s %v", raw, err)
	}
}

func TestStartWithoutStaticStoreRecordsNothing(t *testing.T) {
	h, path, record, _ := staticStartHost(t, nil)
	if _, err := h.Start(context.Background(), path); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(record); !os.IsNotExist(err) {
		t.Fatalf("%v", err)
	}
}

func TestStartStaticStoreFails(t *testing.T) {
	h, path, _, d := staticStartHost(t, &fakeStatic{err: &credential.StoreError{Path: "/x/static-secrets.json", Reason: "invalid content"}})
	_, err := h.Start(context.Background(), path)
	if e := protoErr(t, err); e.Code != protocol.StartFailed || e.Message != "/x/static-secrets.json: invalid content." || hasCall(d, "run") {
		t.Fatalf("%+v %q", e, d.calls)
	}
}

func TestStartInjectedRecordFails(t *testing.T) {
	h, path, record, d := staticStartHost(t, &fakeStatic{})
	if err := os.RemoveAll(filepath.Dir(record)); err != nil {
		t.Fatal(err)
	}
	_, err := h.Start(context.Background(), path)
	e := protoErr(t, err)
	if e.Code != protocol.StartFailed || e.Message != "Recording "+filepath.Base(record)+" failed." || e.Cleanup != protocol.Removed {
		t.Fatalf("%+v", e)
	}
	if !slices.ContainsFunc(d.calls, func(c []string) bool { return c[0] == "rm" || slices.Equal(c[:2], []string{"container", "rm"}) }) {
		t.Fatalf("no rollback: %q", d.calls)
	}
}

func TestStaticRefs(t *testing.T) {
	m := Manifest{Client: "claude", ClaudeAccount: "work", Secrets: []Secret{{Item: "ensemble-broker-env/a", File: "a"}}}
	if got := staticRefs(m); !slices.Equal(got, []string{"ensemble-broker-claude/work", "ensemble-broker-env/a"}) {
		t.Fatalf("%q", got)
	}
	m = Manifest{Client: "opencode", OpencodeProviders: []string{"strata", "lab"}}
	if got := staticRefs(m); !slices.Equal(got, []string{"ensemble-broker-opencode/strata", "ensemble-broker-opencode/lab"}) {
		t.Fatalf("%q", got)
	}
	if got := staticRefs(Manifest{Client: "codex"}); len(got) != 0 {
		t.Fatalf("%q", got)
	}
}
