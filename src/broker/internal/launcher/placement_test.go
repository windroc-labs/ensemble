package launcher

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const (
	remoteEndpoint = "ssh://u@pc"
	localDaemon    = "7b2d3e4f-1a5c-4d6e-8f70-b1c2d3e4f5a6"
	miniPassword   = "ensemble-broker-env/mini-op-password"
)

// twoDaemons is an env start host whose target is the remote world and whose local Docker Desktop is the local
// world, with miniPassword its one local secret.
func twoDaemons(remote, local *world, env *fakeEnv) (Host, *fakeDocker, *fakeDocker) {
	h, rd, _ := envHost(remote, env)
	ld := local.docker()
	return onTwo(h, rd, ld), rd, ld
}

func localWorld() *world {
	return &world{labels: goodLabels(), daemon: `"` + localDaemon + `"`}
}

func TestStartLocalSecretRunsOnLocalDaemon(t *testing.T) {
	env := &fakeEnv{}
	h, rd, ld := twoDaemons(&world{labels: goodLabels(), unreachable: true}, localWorld(), env)
	manifest := editedManifest(t, withSecrets([]any{secret("ensemble-broker-env/other", "other"), secret(miniPassword, "mini-password")}))
	result, err := h.Start(context.Background(), manifest)
	if err != nil {
		t.Fatal(err)
	}
	if len(rd.calls) != 0 {
		t.Fatalf("docker calls on the target: %q", rd.calls)
	}
	if result.Endpoint != localEndpoint || result.Daemon != localDaemon {
		t.Fatalf("reply %s %s, want the local daemon", result.Endpoint, result.Daemon)
	}
	if !hasCall(ld, "run") || len(startCommands(t, ld)) != 3 {
		t.Fatalf("local daemon calls: %q", ld.calls)
	}
}

func TestStartWithoutLocalSecretRunsOnTarget(t *testing.T) {
	h, rd, ld := twoDaemons(&world{labels: goodLabels()}, localWorld(), &fakeEnv{})
	manifest := editedManifest(t, withSecrets([]any{secret("ensemble-broker-env/other", "other")}))
	result, err := h.Start(context.Background(), manifest)
	if err != nil {
		t.Fatal(err)
	}
	if len(ld.calls) != 0 || !hasCall(rd, "run") || result.Endpoint != remoteEndpoint || result.Daemon != testDaemon {
		t.Fatalf("local calls %q; reply %s %s", ld.calls, result.Endpoint, result.Daemon)
	}
}

func TestLocalPlacementNeverFallsBack(t *testing.T) {
	local := localWorld()
	local.unreachable = true
	h, rd, _ := twoDaemons(&world{labels: goodLabels()}, local, &fakeEnv{})
	_, err := h.Start(context.Background(), editedManifest(t, withSecrets([]any{secret(miniPassword, "mini-password")})))
	if e := protoErr(t, err); e.Code != "docker_unreachable" {
		t.Fatalf("%+v", e)
	}
	if len(rd.calls) != 0 {
		t.Fatalf("fell back to the target: %q", rd.calls)
	}
}

func TestLocalPlacedStartGetsLocalProxyHint(t *testing.T) {
	local := localWorld()
	empty := ""
	local.proxies = &empty
	h, _, _ := twoDaemons(&world{labels: goodLabels()}, local, &fakeEnv{})
	_, err := h.Start(context.Background(), editedManifest(t, withSecrets([]any{secret(miniPassword, "mini-password")})))
	e := protoErr(t, err)
	if e.Code != "egress_unavailable" || !strings.HasSuffix(e.Message, localEgressHint) {
		t.Fatalf("%+v", e)
	}
}

func TestPlacement(t *testing.T) {
	remote := Daemon{Name: DaemonRemote, Endpoint: remoteEndpoint, Remote: true}
	local := Daemon{Name: DaemonLocal, Endpoint: localEndpoint}
	for _, tc := range []struct {
		name    string
		daemons []Daemon
		items   []string
		want    string
	}{
		{"local secret beside a remote target", []Daemon{remote, local}, []string{"ensemble-broker-env/other", miniPassword}, DaemonLocal},
		{"no local secret", []Daemon{remote, local}, []string{"ensemble-broker-env/other"}, DaemonRemote},
		{"no secrets", []Daemon{remote, local}, nil, DaemonRemote},
		{"local target only", []Daemon{local}, []string{miniPassword}, DaemonLocal},
	} {
		if got := Placement(tc.daemons, []string{miniPassword})(tc.items); got.Name != tc.want {
			t.Errorf("%s: placed on %s, want %s", tc.name, got.Name, tc.want)
		}
	}
	if got := (Host{Daemons: []Daemon{remote, local}}).placed([]string{miniPassword}); got.Name != DaemonRemote {
		t.Errorf("no placement rule: placed on %s, want the default", got.Name)
	}
}

func TestMaintainPlacesByManifest(t *testing.T) {
	for _, local := range []bool{true, false} {
		h, manifest, _, _ := maintainHost(t, true, nil)
		if local {
			data, err := os.ReadFile(manifest)
			if err != nil {
				t.Fatal(err)
			}
			var fields map[string]any
			if err := json.Unmarshal(data, &fields); err != nil {
				t.Fatal(err)
			}
			withSecrets([]any{secret(miniPassword, "mini-password")})(fields)
			manifest = writeManifest(t, must(json.Marshal(fields)))
		}
		onLocal := h.Daemons[0].Runner.(*fakeDocker)
		remote := &fakeDocker{}
		h = onTwo(h, remote, onLocal)
		if _, err := h.Maintain(context.Background(), container, manifest, true); err != nil {
			t.Fatal(err)
		}
		if used := len(onLocal.calls) > 0; used != local || len(remote.calls) > 0 == local {
			t.Fatalf("local secret %v: local calls %q, remote calls %q", local, onLocal.calls, remote.calls)
		}
	}
}

func must(data []byte, err error) []byte {
	if err != nil {
		panic(err)
	}
	return data
}

func TestBuildSecretPlacesOnLocalDaemon(t *testing.T) {
	e := newBuildEnv(t)
	h := e.lookupHost()
	remote := &fakeDocker{respond: func([]string, []byte) ([]byte, error) { return nil, unreachableErr }}
	h = onTwo(h, remote, e.fake)
	if _, err := h.Build(context.Background(), e.pkg, BuildSpec{Secrets: []string{miniPassword}}, -1); err != nil {
		t.Fatal(err)
	}
	if len(remote.calls) != 0 {
		t.Fatalf("built on the target: %q", remote.calls)
	}
	records, err := os.ReadDir(e.imagesDir())
	if err != nil || len(records) == 0 {
		t.Fatalf("no image records under the local daemon's ID: %v", err)
	}
	for _, r := range records {
		if data, _ := os.ReadFile(filepath.Join(e.imagesDir(), r.Name())); strings.Contains(string(data), miniPassword) {
			t.Fatalf("record %s names a secret", r.Name())
		}
	}
	if _, err := h.Build(context.Background(), e.pkg, BuildSpec{Secrets: []string{"ensemble-broker-env/other"}}, -1); protoErr(t, err).Code != "docker_unreachable" {
		t.Fatalf("a build without a local secret did not go to the target: %v", err)
	}
}
