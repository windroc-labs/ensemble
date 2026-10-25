package launcher

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"slices"
	"strings"
	"testing"

	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/protocol"
)

const envValue = "FAKE-ENV-VALUE"

// fakeEnv is the environment secrets: ready fails Ready; failOn fails Inject of that item; calls records each call.
type fakeEnv struct {
	ready  error
	failOn map[string]error
	calls  []string
}

func (e *fakeEnv) Ready(items []string) error {
	e.calls = append(e.calls, "ready "+strings.Join(items, ","))
	return e.ready
}

func (e *fakeEnv) Inject(s Secret, exec func([]string, []byte) error) error {
	e.calls = append(e.calls, "inject "+s.Item+" "+s.File)
	if err := e.failOn[s.Item]; err != nil {
		return err
	}
	return exec([]string{"inject-env", s.File}, []byte(envValue+"-"+s.Item))
}

var dummySecrets = []any{secret("ensemble-env/dummy", "dummy.txt"), secret("ensemble-env/ssh", "id_ed25519")}

// envHost is a Claude start host whose environment secrets are env.
func envHost(w *world, env *fakeEnv) (Host, *fakeDocker, *fakeCreds) {
	creds := &fakeCreds{}
	h, d := startHost(w, creds)
	h.Env = env
	return h, d, creds
}

func TestStartRejectsSecretManifest(t *testing.T) {
	for name, secrets := range map[string]any{
		"broker item": []any{secret("ensemble-bot", "key")},
		"traversal":   []any{secret("ensemble-env/dummy", "../github/token")},
		"repeated":    []any{secret("ensemble-env/a", "x"), secret("ensemble-env/b", "x")},
	} {
		env := &fakeEnv{}
		h, d, creds := envHost(&world{labels: goodLabels()}, env)
		_, err := h.Start(context.Background(), editedManifest(t, withSecrets(secrets)))
		if code(t, err) != protocol.ManifestRejected || len(d.calls) != 0 || len(env.calls) != 0 || len(creds.calls) != 0 {
			t.Fatalf("%s: %v, docker %q, env %q", name, err, d.calls, env.calls)
		}
	}
}

func TestStartEnvNotReady(t *testing.T) {
	missing := protocol.New(protocol.LoginRequired, "No keychain item ensemble-env/ssh: run ensemble-broker login env ssh.")
	env := &fakeEnv{ready: missing}
	h, d, _ := envHost(&world{labels: goodLabels()}, env)
	_, err := h.Start(context.Background(), editedManifest(t, withSecrets(dummySecrets)))
	if e := protoErr(t, err); e != missing || hasCall(d, "run") || !slices.Equal(env.calls, []string{"ready ensemble-env/dummy,ensemble-env/ssh"}) {
		t.Fatalf("%+v, created %v, env %q", e, hasCall(d, "run"), env.calls)
	}
}

// secretsTmpfs is the --tmpfs value of d's docker run --detach for the secrets directory.
func secretsTmpfs(t *testing.T, d *fakeDocker) string {
	t.Helper()
	for _, call := range d.calls {
		for i, arg := range call {
			if call[0] == "run" && arg == "--tmpfs" && strings.HasPrefix(call[i+1], docker.SecretsDir+":") {
				return call[i+1]
			}
		}
	}
	t.Fatalf("no secrets tmpfs in %q", d.calls)
	return ""
}

func TestStartEnvSecrets(t *testing.T) {
	env := &fakeEnv{}
	h, d, creds := envHost(&world{labels: goodLabels()}, env)
	if _, err := h.Start(context.Background(), editedManifest(t, withSecrets(dummySecrets))); err != nil {
		t.Fatal(err)
	}
	if want := []string{"ready ensemble-env/dummy,ensemble-env/ssh", "inject ensemble-env/dummy dummy.txt", "inject ensemble-env/ssh id_ed25519"}; !slices.Equal(env.calls, want) {
		t.Fatalf("%q", env.calls)
	}
	created, claude, injected := -1, -1, []int{}
	for i, c := range d.calls {
		if c[0] == "run" && c[1] == "--detach" {
			created = i
		}
		if slices.Contains(c, "inject") {
			claude = i
		}
		if slices.Contains(c, "inject-env") {
			injected = append(injected, i)
			if !slices.Equal(c[:5], []string{"exec", "--interactive", "--user", "agent", container}) {
				t.Fatalf("%q", c)
			}
		}
		for _, arg := range c {
			if strings.Contains(arg, envValue) {
				t.Fatalf("value in docker argument %q", c)
			}
		}
	}
	if len(injected) != 2 || created < 0 || claude < created || injected[0] < claude || !slices.Equal(creds.calls, []string{"ready default", "inject default"}) {
		t.Fatalf("order: create %d, claude %d, env %v: %q", created, claude, injected, d.calls)
	}
	if got, want := secretsTmpfs(t, d), docker.SecretsDir+":rw,noexec,nosuid,nodev,size=102400,mode=0700,uid=1001,gid=1002"; got != want {
		t.Fatalf("tmpfs %q, want %q", got, want)
	}
	for _, edit := range []func(map[string]any){nil, withSecrets([]any{})} {
		env := &fakeEnv{}
		h, d, _ := envHost(&world{labels: goodLabels()}, env)
		if _, err := h.Start(context.Background(), editedManifest(t, edit)); err != nil || len(env.calls) != 0 {
			t.Fatalf("no secrets: %q %v", env.calls, err)
		}
		if got := secretsTmpfs(t, d); !strings.Contains(got, ",size=65536,") {
			t.Fatalf("tmpfs %q", got)
		}
	}
}

// envCredentials are the env-secret credentials of the audit log at path, as "<item> <result>", and whether the
// log holds the value.
func envCredentials(t *testing.T, path string) ([]string, bool) {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var line struct {
		Credentials []map[string]any
	}
	if err := json.Unmarshal(data, &line); err != nil {
		t.Fatal(err)
	}
	var out []string
	for _, c := range line.Credentials {
		if c["kind"] == "env-secret" {
			out = append(out, c["item"].(string)+" "+c["result"].(string))
			if c["expires_at"] != nil {
				t.Fatalf("expires_at %v", c["expires_at"])
			}
		}
	}
	return out, strings.Contains(string(data), envValue)
}

func TestStartEnvSecretsAudit(t *testing.T) {
	h, _, _ := envHost(&world{labels: goodLabels()}, &fakeEnv{})
	rec, path := audited(t, &h, "start", "")
	_, err := h.Start(context.Background(), editedManifest(t, withSecrets(dummySecrets)))
	rec.Flush(err)
	got, leaked := envCredentials(t, path)
	if err != nil || leaked || !slices.Equal(got, []string{"ensemble-env/dummy issued", "ensemble-env/ssh issued"}) {
		t.Fatalf("%v: %q leaked=%v", err, got, leaked)
	}
}

func TestStartEnvInjectFails(t *testing.T) {
	for name, tc := range map[string]struct {
		w        *world
		injectEr error
		code     protocol.Code
		cleanup  string
		want     []string
	}{
		"protocol": {w: &world{labels: goodLabels()}, injectEr: protocol.New(protocol.StartFailed, "Writing ensemble-env/ssh into the container failed."),
			code: protocol.StartFailed, cleanup: protocol.Removed, want: []string{"ensemble-env/dummy issued", "ensemble-env/dummy withdrawn"}},
		"opaque": {w: &world{labels: goodLabels()}, injectEr: errors.New("exec failed"),
			code: protocol.StartFailed, cleanup: protocol.Removed, want: []string{"ensemble-env/dummy issued", "ensemble-env/dummy withdrawn"}},
		"rollback fails": {w: &world{labels: goodLabels(), rmErr: &docker.CommandError{Exit: 1, Stderr: "busy"}},
			injectEr: protocol.New(protocol.KeychainFailed, "Keychain access to ensemble-env/ssh failed."),
			code:     protocol.KeychainFailed, cleanup: protocol.Failed, want: []string{"ensemble-env/dummy issued"}},
	} {
		t.Run(name, func(t *testing.T) {
			env := &fakeEnv{failOn: map[string]error{"ensemble-env/ssh": tc.injectEr}}
			h, d, _ := envHost(tc.w, env)
			rec, path := audited(t, &h, "start", "")
			_, err := h.Start(context.Background(), editedManifest(t, func(f map[string]any) { withGitHub(f); withSecrets(dummySecrets)(f) }))
			rec.Flush(err)
			e := protoErr(t, err)
			if e.Code != tc.code || e.Cleanup != tc.cleanup || !hasCall(d, "rm") {
				t.Fatalf("%+v", e)
			}
			if name == "opaque" && e.Message != "Writing ensemble-env/ssh into the container failed." {
				t.Fatalf("message %q", e.Message)
			}
			got, leaked := envCredentials(t, path)
			if leaked || !slices.Equal(got, tc.want) {
				t.Fatalf("%q leaked=%v", got, leaked)
			}
		})
	}
}
