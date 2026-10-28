package launcher

import (
	"context"
	"errors"
	"slices"
	"strings"
	"testing"

	"ensemble/broker/internal/credential"
	"ensemble/broker/internal/protocol"
)

type fakeCodexCreds struct {
	locked                    bool
	err, injectErr, finishErr error
	ready, injected           int
	log                       *[]string // every call, when set
}

func (c *fakeCodexCreds) note(call string) {
	if c.log != nil {
		*c.log = append(*c.log, call)
	}
}

func (c *fakeCodexCreds) Ready(context.Context) error { c.note("codex ready"); c.ready++; return c.err }
func (c *fakeCodexCreds) Inject(_ context.Context) (CodexInjection, error) {
	c.note("codex inject")
	c.injected++
	if c.injectErr != nil {
		return CodexInjection{}, c.injectErr
	}
	c.locked = true
	return CodexInjection{
		Command: credential.Command{Argv: []string{"inject-codex"}, Stdin: []byte("fake-access-only")},
		Expiry:  "2026-10-18T12:00:00Z",
		Finish:  func() error { c.locked = false; return c.finishErr },
	}, nil
}

func TestStartKeepsCodexLockThroughDelivery(t *testing.T) {
	for _, stage := range []string{"success", "prepare env", "prepare github", "deliver", "finish"} {
		t.Run(stage, func(t *testing.T) {
			creds := &fakeCodexCreds{}
			env := &fakeEnv{}
			tokens := &fakeTokens{}
			boom := errors.New("opaque")
			w := &world{labels: goodLabels()}
			switch stage {
			case "prepare env":
				env.failOn = map[string]error{"ensemble-broker-env/dummy": boom}
			case "prepare github":
				tokens.issue = boom
			case "deliver":
				w.execErr = failOn(credentialBatch, boom)
			case "finish":
				creds.finishErr = boom
			}
			h, d := startHost(w, &fakeCreds{})
			h.Codex, h.Versions, h.Env, h.GitHub = creds, &fakeVersions{}, env, tokens
			respond := d.respond
			d.respond = func(args []string, stdin []byte) ([]byte, error) {
				if slices.Contains(args, credentialBatch) && !creds.locked {
					t.Fatal("Codex account unlocked before actual delivery")
				}
				return respond(args, stdin)
			}
			_, err := h.Start(context.Background(), codexManifest(t, withSecrets(dummySecrets), withGitHub))
			if (err == nil) != (stage == "success") || creds.locked {
				t.Fatalf("error=%v locked=%v", err, creds.locked)
			}
			if stage == "finish" && protoErr(t, err).Message != "Injecting Codex credentials failed." {
				t.Fatal(err)
			}
		})
	}
}

func (c *fakeCodexCreds) Refresh(ctx context.Context, exec func([]string, []byte) error, publish func(string) error) (string, error) {
	c.note("codex refresh")
	if c.err != nil {
		return "", c.err
	}
	login, err := c.Inject(ctx)
	if err != nil {
		return "", err
	}
	defer login.Finish()
	if err := exec(login.Command.Argv, login.Command.Stdin); err != nil {
		return "", err
	}
	return login.Expiry, publish(login.Expiry)
}

func codexManifest(t *testing.T, edits ...func(map[string]any)) string {
	t.Helper()
	return fixtureManifest(t, "manifest-codex.json", edits...)
}

func TestCodexStartIsolation(t *testing.T) {
	h, d := startHost(&world{labels: labelsWithVersion("codex", "0.163.0")}, &fakeCreds{ready: errors.New("Claude must not be read")})
	creds, v := &fakeCodexCreds{}, &fakeVersions{}
	h.Codex, h.Versions = creds, v
	result, err := h.Start(context.Background(), codexManifest(t))
	if err != nil || result.Deadline == nil || result.Deadline.At != "2026-10-18T12:00:00Z" || creds.ready != 1 || creds.injected != 1 ||
		result.Clients.Codex != "codex-cli 0.163.0" || !slices.Equal(v.calls, []string{"verify"}) {
		t.Fatalf("%+v %v %v", result, err, v.calls)
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
		h, d := startHost(&world{labels: labelsWithVersion("codex", "0.162.999")}, &fakeCreds{})
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
	if err != nil || result.Deadline != nil || creds.ready != 0 || creds.injected != 0 {
		t.Fatalf("%+v %v", result, err)
	}
}

func TestCodexMountBoundary(t *testing.T) {
	for _, unsafe := range []string{"arbitrary", "noncanonical", "sessions-link"} {
		h, d := startHost(&world{labels: goodLabels()}, &fakeCreds{})
		h.Codex = &fakeCodexCreds{}
		path := codexManifest(t, func(fields map[string]any) {
			switch unsafe {
			case "arbitrary":
				fields["codex_dir"] = "/arbitrary"
			case "noncanonical":
				fields["codex_dir"] = fields["codex_dir"].(string) + "/."
			}
		})
		if unsafe == "sessions-link" {
			h.Safe = func(p string) error {
				if strings.HasSuffix(p, "/sessions") {
					return errors.New("symlink")
				}
				return nil
			}
		}
		_, err := h.Start(context.Background(), path)
		if code(t, err) != protocol.MountRejected || hasCall(d, "run") {
			t.Fatal("unsafe mount accepted")
		}
	}
}

// TestCodexStartRollbackMakesNoCodexCall: a start injects the login once; a start that fails in or after Inject
// rolls back with no further Codex call, and one that fails its preflight injects nothing.
func TestCodexStartRollbackMakesNoCodexCall(t *testing.T) {
	newHost := func() (Host, *fakeCodexCreds, *fakeEnv, *[]string) {
		h, _ := startHost(&world{labels: labelsWithVersion("codex", "0.163.0")}, &fakeCreds{})
		var log []string
		creds, env := &fakeCodexCreds{log: &log}, &fakeEnv{failOn: map[string]error{}}
		h.Codex, h.Versions, h.Env = creds, &fakeVersions{}, env
		return h, creds, env, &log
	}
	h, _, _, log := newHost()
	if _, err := h.Start(context.Background(), codexManifest(t)); err != nil || !slices.Equal(*log, []string{"codex ready", "codex inject"}) {
		t.Fatalf("calls %q: %v", *log, err)
	}
	for name, fail := range map[string]func(*fakeCodexCreds, *fakeEnv){
		"after inject": func(_ *fakeCodexCreds, env *fakeEnv) {
			env.failOn["ensemble-broker-env/dummy"] = errors.New("env write failed")
		},
		"in inject": func(creds *fakeCodexCreds, _ *fakeEnv) { creds.injectErr = errors.New("writer failed") },
	} {
		h, creds, env, log := newHost()
		fail(creds, env)
		if _, err := h.Start(context.Background(), codexManifest(t, withSecrets(dummySecrets))); err == nil {
			t.Fatalf("%s: start succeeded", name)
		}
		if !slices.Equal(*log, []string{"codex ready", "codex inject"}) {
			t.Fatalf("%s: calls %q", name, *log)
		}
	}
	h, creds, _, log := newHost()
	creds.err = protocol.New(protocol.LoginRequired, "login codex")
	if _, err := h.Start(context.Background(), codexManifest(t)); err == nil || !slices.Equal(*log, []string{"codex ready"}) {
		t.Fatalf("preflight failure: calls %q: %v", *log, err)
	}
}
