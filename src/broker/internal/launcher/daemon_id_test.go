package launcher

import (
	"context"
	"encoding/json"
	"os"
	"slices"
	"strings"
	"testing"

	"ensemble/broker/internal/protocol"
)

const (
	remoteEndpoint = "ssh://u@pc"
	localDaemon    = "7b2d3e4f-1a5c-4d6e-8f70-b1c2d3e4f5a6"
	miniPassword   = "ensemble-broker-env/mini-op-password"
)

// twoDaemons is an env start host whose target is the remote world and whose local Docker Desktop is the local
// world, with miniPassword local only.
func twoDaemons(remote, local *world, env *fakeEnv) (Host, *fakeDocker, *fakeDocker) {
	h, rd, _ := envHost(remote, env)
	ld := local.docker()
	h = onTwo(h, rd, ld)
	h.LocalOnly = []string{miniPassword}
	return h, rd, ld
}

func localWorld() *world {
	return &world{labels: goodLabels(), daemon: `"` + localDaemon + `"`}
}

// beyondInfo is d's calls other than the daemon ID query.
func beyondInfo(d *fakeDocker) [][]string {
	return slices.DeleteFunc(slices.Clone(d.calls), func(c []string) bool { return c[0] == "info" })
}

func onDaemon(id string) func(map[string]any) {
	return func(f map[string]any) { f["daemon"] = id }
}

func TestStartRunsOnTheManifestsDaemon(t *testing.T) {
	h, rd, ld := twoDaemons(&world{labels: goodLabels()}, localWorld(), &fakeEnv{})
	result, err := h.Start(context.Background(), editedManifest(t, func(f map[string]any) {
		onDaemon(localDaemon)(f)
		withSecrets([]any{secret(miniPassword, "mini-password")})(f)
	}))
	if err != nil {
		t.Fatal(err)
	}
	if len(rd.calls) != 0 || result.Endpoint != localEndpoint || result.Daemon != localDaemon || !hasCall(ld, "run") {
		t.Fatalf("remote calls %q; reply %s %s", rd.calls, result.Endpoint, result.Daemon)
	}
	h, rd, ld = twoDaemons(&world{labels: goodLabels()}, localWorld(), &fakeEnv{})
	result, err = h.Start(context.Background(), editedManifest(t, withSecrets([]any{secret("ensemble-broker-env/other", "other")})))
	if err != nil {
		t.Fatal(err)
	}
	if len(beyondInfo(ld)) != 0 || !hasCall(rd, "run") || result.Endpoint != remoteEndpoint || result.Daemon != testDaemon {
		t.Fatalf("local calls %q; reply %s %s", ld.calls, result.Endpoint, result.Daemon)
	}
	if n := len(slices.DeleteFunc(slices.Clone(rd.calls), func(c []string) bool { return c[0] != "info" })); n != 1 {
		t.Fatalf("a start identifies its daemon once, got %d info calls", n)
	}
}

func TestStartRefusesALocalOnlySecretOnARemoteDaemon(t *testing.T) {
	env := &fakeEnv{}
	h, rd, ld := twoDaemons(&world{labels: goodLabels()}, localWorld(), env)
	_, err := h.Start(context.Background(), editedManifest(t, withSecrets([]any{secret(miniPassword, "mini-password")})))
	e := protoErr(t, err)
	if e.Code != protocol.ManifestRejected || !strings.Contains(e.Message, miniPassword) || !strings.Contains(e.Message, "remote "+remoteEndpoint) {
		t.Fatalf("%+v", e)
	}
	if len(beyondInfo(rd)) != 0 || len(beyondInfo(ld)) != 0 || len(env.calls) != 0 {
		t.Fatalf("remote %q local %q env %q", rd.calls, ld.calls, env.calls)
	}
}

func TestStartNeverFallsBackFromItsDaemon(t *testing.T) {
	local := localWorld()
	local.unreachable = true
	h, rd, _ := twoDaemons(&world{labels: goodLabels()}, local, &fakeEnv{})
	_, err := h.Start(context.Background(), editedManifest(t, onDaemon(localDaemon)))
	if e := protoErr(t, err); e.Code != protocol.DockerUnreachable || !strings.Contains(e.Message, "host key mismatch") {
		t.Fatalf("%+v", e)
	}
	if len(beyondInfo(rd)) != 0 {
		t.Fatalf("fell back to the remote: %q", rd.calls)
	}
}

func TestLocalDaemonStartGetsLocalProxyHint(t *testing.T) {
	local := localWorld()
	empty := ""
	local.proxies = &empty
	h, _, _ := twoDaemons(&world{labels: goodLabels()}, local, &fakeEnv{})
	_, err := h.Start(context.Background(), editedManifest(t, onDaemon(localDaemon)))
	e := protoErr(t, err)
	if e.Code != "egress_unavailable" || !strings.HasSuffix(e.Message, localEgressHint) {
		t.Fatalf("%+v", e)
	}
}

func TestAnUnconfiguredDaemonFailsEveryCommand(t *testing.T) {
	const unknown = "GONE:DAEMON"
	ctx := context.Background()
	check := func(name string, err error, rd, ld *fakeDocker) {
		t.Helper()
		e := protoErr(t, err)
		if e.Code != protocol.DockerUnreachable || !strings.Contains(e.Message, unknown) ||
			!strings.Contains(e.Message, "local "+localEndpoint+" ("+localDaemon+")") || !strings.Contains(e.Message, "remote "+remoteEndpoint+" ("+testDaemon+")") {
			t.Fatalf("%s: %+v", name, e)
		}
		if len(beyondInfo(rd)) != 0 || len(beyondInfo(ld)) != 0 {
			t.Fatalf("%s: ran docker beyond identifying: remote %q local %q", name, rd.calls, ld.calls)
		}
	}
	h, rd, ld := twoDaemons(&world{labels: goodLabels()}, localWorld(), &fakeEnv{})
	_, err := h.Start(ctx, editedManifest(t, onDaemon(unknown)))
	check("start", err, rd, ld)
	h, rd, ld = twoDaemons(&world{labels: goodLabels()}, localWorld(), &fakeEnv{})
	_, err = h.Find(ctx, unknown, protocol.CloseFailed)
	check("close", err, rd, ld)
	h, rd, ld = twoDaemons(&world{labels: goodLabels()}, localWorld(), &fakeEnv{})
	_, err = h.Find(ctx, unknown, protocol.WorkspaceFailed)
	check("workspace-rm", err, rd, ld)
	h, rd, ld = twoDaemons(&world{labels: goodLabels()}, localWorld(), &fakeEnv{})
	_, err = h.Build(ctx, "/nonexistent", BuildSpec{Daemon: unknown}, -1)
	check("build", err, rd, ld)
}

func TestAnUnreachableDaemonComesBeforeAMiss(t *testing.T) {
	h, _, ld := twoDaemons(&world{labels: goodLabels(), unreachable: true}, localWorld(), &fakeEnv{})
	_, err := h.Find(context.Background(), testDaemon, protocol.CloseFailed)
	if e := protoErr(t, err); e.Code != protocol.DockerUnreachable || e.Message != "Cannot reach Docker target ssh://u@pc: host key mismatch." {
		t.Fatalf("%+v", e)
	}
	if len(beyondInfo(ld)) != 0 {
		t.Fatalf("local calls %q", ld.calls)
	}
}

func TestMaintainUsesTheManifestsDaemon(t *testing.T) {
	for _, onLocal := range []bool{true, false} {
		h, manifest, _, _ := maintainHost(t, true, nil)
		if onLocal {
			data, err := os.ReadFile(manifest)
			if err != nil {
				t.Fatal(err)
			}
			var fields map[string]any
			if err := json.Unmarshal(data, &fields); err != nil {
				t.Fatal(err)
			}
			onDaemon(localDaemon)(fields)
			manifest = writeManifest(t, must(json.Marshal(fields)))
		}
		local := h.Daemons[0].Runner.(*fakeDocker)
		local.respond = idThen(localDaemon, local.respond)
		remote := &fakeDocker{respond: idThen(testDaemon, nil)}
		h = onTwo(h, remote, local)
		if _, err := h.Maintain(context.Background(), container, manifest, true); err != nil {
			t.Fatal(err)
		}
		if used := len(beyondInfo(local)) > 0; used != onLocal || len(beyondInfo(remote)) > 0 == onLocal {
			t.Fatalf("on local %v: local calls %q, remote calls %q", onLocal, local.calls, remote.calls)
		}
	}
}

// idThen answers docker info with id, every other call with next (nil: nothing).
func idThen(id string, next func([]string, []byte) ([]byte, error)) func([]string, []byte) ([]byte, error) {
	return func(args []string, stdin []byte) ([]byte, error) {
		if args[0] == "info" {
			return []byte(`"` + id + `"`), nil
		}
		if next == nil {
			return nil, nil
		}
		return next(args, stdin)
	}
}

func must(data []byte, err error) []byte {
	if err != nil {
		panic(err)
	}
	return data
}

func TestDaemonPatternMatchesSchema(t *testing.T) {
	data, err := os.ReadFile("../../protocol/docker-broker/protocol.schema.json")
	if err != nil {
		t.Fatal(err)
	}
	var schema struct {
		Defs map[string]struct {
			Pattern string `json:"pattern"`
		} `json:"$defs"`
	}
	if err := json.Unmarshal(data, &schema); err != nil {
		t.Fatal(err)
	}
	if got := schema.Defs["daemon"].Pattern; got != daemonPattern.String() {
		t.Fatalf("schema %q, broker %q", got, daemonPattern.String())
	}
	if !ValidDaemon(testDaemon) || ValidDaemon("a b") || ValidDaemon("") {
		t.Fatal("ValidDaemon")
	}
}
