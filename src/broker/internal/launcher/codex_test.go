package launcher

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"slices"
	"strings"
	"testing"

	"ensemble/broker/internal/protocol"
)

type fakeCodexCreds struct {
	err             error
	ready, injected int
}

func (c *fakeCodexCreds) Ready(context.Context) error { c.ready++; return c.err }
func (c *fakeCodexCreds) Inject(_ context.Context, exec func([]string, []byte) error) (string, error) {
	c.injected++
	return "2026-10-18T12:00:00Z", exec([]string{"inject-codex"}, []byte("fake-access-only"))
}

func (c *fakeCodexCreds) Refresh(ctx context.Context, exec func([]string, []byte) error, publish func(string) error) (string, error) {
	if c.err != nil {
		return "", c.err
	}
	expiry, err := c.Inject(ctx, exec)
	if err != nil {
		return "", err
	}
	return expiry, publish(expiry)
}

func codexManifest(t *testing.T) string {
	t.Helper()
	data, err := os.ReadFile(validManifest(t))
	if err != nil {
		t.Fatal(err)
	}
	var fields map[string]any
	if err := json.Unmarshal(data, &fields); err != nil {
		t.Fatal(err)
	}
	fields["client"] = "codex"
	fields["codex_dir"] = strings.Replace(fields["claude_dir"].(string), "/claude", "/codex", 1)
	delete(fields, "claude_dir")
	data, err = json.Marshal(fields)
	if err != nil {
		t.Fatal(err)
	}
	return writeManifest(t, data)
}

func TestCodexStartIsolation(t *testing.T) {
	h, d := startHost(&world{labels: goodLabels(), versions: []string{"claude 2", "codex-cli 0.163.0"}}, &fakeCreds{ready: errors.New("Claude must not be read")})
	creds := &fakeCodexCreds{}
	h.Codex = creds
	h.Versions = &fakeVersions{}
	result, err := h.Start(context.Background(), codexManifest(t))
	if err != nil || result.CredentialExpiresAt == nil || *result.CredentialExpiresAt != "2026-10-18T12:00:00Z" || creds.ready != 1 || creds.injected != 1 {
		t.Fatalf("%+v %v", result, err)
	}
	var run []string
	for _, call := range d.calls {
		if call[0] == "run" && call[1] == "--detach" {
			run = call
		}
	}
	joined := strings.Join(run, " ")
	for _, want := range []string{"CODEX_HOME=", "--label ensemble.client=codex", "/codex:rw,noexec,nosuid,nodev,size=1073741824,mode=0700,uid=1001,gid=1002 ", "/codex/sessions", "--tmpfs /run/ensemble:"} {
		if !strings.Contains(joined, want) {
			t.Errorf("missing %s", want)
		}
	}
	if strings.Contains(joined, "CLAUDE_CONFIG_DIR") || strings.Contains(joined, "/claude") || slices.Contains(run, "--privileged") {
		t.Fatal("unsafe Codex mounts/env")
	}
}

func TestCodexStartPreflightAndVersion(t *testing.T) {
	for _, preflight := range []bool{true, false} {
		h, d := startHost(&world{labels: goodLabels(), versions: []string{"claude 2", "codex-cli 0.162.999"}}, &fakeCreds{})
		creds := &fakeCodexCreds{}
		if preflight {
			creds.err = protocol.New(protocol.LoginRequired, "login codex")
		}
		h.Codex = creds
		h.Versions = &fakeVersions{}
		_, err := h.Start(context.Background(), codexManifest(t))
		if err == nil || creds.injected != 0 {
			t.Fatal("accepted missing login or unsupported client")
		}
		if preflight && hasCall(d, "run") {
			t.Fatal("created container before preflight")
		}
		if !preflight && !hasCall(d, "rm") {
			t.Fatal("failed to roll back incompatible container")
		}
	}
}

func TestClaudeDoesNotReadCodex(t *testing.T) {
	h, _ := startHost(&world{labels: goodLabels()}, &fakeCreds{})
	creds := &fakeCodexCreds{err: errors.New("Codex must not be read")}
	h.Codex = creds
	result, err := h.Start(context.Background(), validManifest(t))
	if err != nil || result.CredentialExpiresAt != nil || creds.ready != 0 || creds.injected != 0 {
		t.Fatalf("%+v %v", result, err)
	}
}

func TestCodexMountBoundary(t *testing.T) {
	for _, unsafe := range []string{"arbitrary", "noncanonical", "sessions-link"} {
		h, d := startHost(&world{labels: goodLabels()}, &fakeCreds{})
		h.Codex = &fakeCodexCreds{}
		data, err := os.ReadFile(codexManifest(t))
		if err != nil {
			t.Fatal(err)
		}
		var fields map[string]any
		if err := json.Unmarshal(data, &fields); err != nil {
			t.Fatal(err)
		}
		switch unsafe {
		case "arbitrary":
			fields["codex_dir"] = "/arbitrary"
		case "noncanonical":
			fields["codex_dir"] = fields["codex_dir"].(string) + "/."
		case "sessions-link":
			h.Safe = func(p string) error {
				if strings.HasSuffix(p, "/sessions") {
					return errors.New("symlink")
				}
				return nil
			}
		}
		data, err = json.Marshal(fields)
		if err != nil {
			t.Fatal(err)
		}
		_, err = h.Start(context.Background(), writeManifest(t, data))
		if code(t, err) != protocol.MountRejected || hasCall(d, "run") {
			t.Fatal("unsafe mount accepted")
		}
	}
}
