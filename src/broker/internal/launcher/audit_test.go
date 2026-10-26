package launcher

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"ensemble/broker/internal/audit"
	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/protocol"
)

const generation = "0123456789abcdef0123456789abcdef"

type auditLine struct {
	Command, Event, Session, Generation, Container string
	Image                                          *string
	ImageDigest                                    *string `json:"image_digest"`
	Mounts                                         *[]audit.Mount
	Credentials                                    []struct {
		Kind, Result, Item string
		ExpiresAt          *string `json:"expires_at"`
	}
	Error *string
}

// audited gives h a recorder for command that writes to a log file under a temp home.
func audited(t *testing.T, h *Host, command, c string) (*audit.Recorder, string) {
	t.Helper()
	path := audit.Path(t.TempDir())
	h.Audit = &audit.Recorder{Command: command, Container: c, Log: audit.Log{Path: path}, Now: time.Now}
	return h.Audit, path
}

func readLines(t *testing.T, path string) []auditLine {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var out []auditLine
	for _, l := range bytes.Split(bytes.TrimSuffix(data, []byte("\n")), []byte("\n")) {
		var v auditLine
		if err := json.Unmarshal(l, &v); err != nil {
			t.Fatal(err)
		}
		out = append(out, v)
	}
	return out
}

func kinds(l auditLine) []string {
	var out []string
	for _, c := range l.Credentials {
		out = append(out, strings.TrimSpace(c.Kind+" "+c.Result+" "+c.Item))
	}
	return out
}

// runMounts are the --mount and --tmpfs values of d's docker run --detach, as audit mounts' targets and sources.
func runMounts(d *fakeDocker) []string {
	var out []string
	for _, call := range d.calls {
		if call[0] != "run" || call[1] != "--detach" {
			continue
		}
		for i, arg := range call[:len(call)-1] {
			switch arg {
			case "--tmpfs":
				target, _, _ := strings.Cut(call[i+1], ":")
				out = append(out, "tmpfs  "+target)
			case "--mount":
				fields := map[string]string{}
				for _, f := range strings.Split(call[i+1], ",") {
					k, v, _ := strings.Cut(f, "=")
					fields[k] = v
				}
				out = append(out, fields["type"]+" "+fields["source"]+" "+fields["target"])
			}
		}
	}
	return out
}

func lineMounts(l auditLine) []string {
	var out []string
	for _, m := range *l.Mounts {
		out = append(out, m.Type+" "+m.Source+" "+m.Target)
	}
	return out
}

func TestStartLine(t *testing.T) {
	for _, client := range []string{"claude", "codex"} {
		t.Run(client, func(t *testing.T) {
			h, d := startHost(&world{labels: goodLabels()}, &fakeCreds{})
			manifest := validManifest(t)
			want := []string{"claude-setup-token issued"}
			if client == "codex" {
				h.Codex, h.Versions, manifest = &fakeCodexCreds{}, &fakeVersions{target: "0.163.0"}, codexManifest(t)
				want = []string{"codex-access-token issued"}
			}
			rec, path := audited(t, &h, "start", "")
			_, err := h.Start(context.Background(), manifest)
			rec.Flush(err)
			if err != nil {
				t.Fatal(err)
			}
			lines := readLines(t, path)
			if len(lines) != 1 {
				t.Fatalf("%+v", lines)
			}
			l := lines[0]
			if l.Event != "start" || l.Command != "start" || l.Container != container || l.Session != "worker_1" || l.Generation != generation ||
				l.Image == nil || *l.Image != "ensemble-worker:"+imageKey || l.ImageDigest == nil || *l.ImageDigest != digest || l.Error != nil {
				t.Fatalf("%+v", l)
			}
			if got := lineMounts(l); !slices.Equal(got, runMounts(d)) || len(got) < 3 {
				t.Fatalf("mounts %v, docker run %v", got, runMounts(d))
			}
			for _, m := range *l.Mounts {
				if m.Mode != "rw" || strings.HasPrefix(m.Source, home+"/Library/Logs") {
					t.Fatalf("%+v", m)
				}
			}
			if !slices.Equal(kinds(l), want) {
				t.Fatal(kinds(l))
			}
			if c := l.Credentials[0]; (client == "codex") != (c.ExpiresAt != nil && *c.ExpiresAt == "2026-10-18T12:00:00Z") {
				t.Fatalf("expiry %v", c.ExpiresAt)
			}
		})
	}
}

// reportingTokens adds what a real token store would, so the test sees where the recorder routes it.
type reportingTokens struct {
	*fakeTokens
	rec            *audit.Recorder
	retryContainer string
}

func (r *reportingTokens) Retry(ctx context.Context) ([]protocol.Pending, error) {
	r.retryContainer = r.rec.Container
	return r.fakeTokens.Retry(ctx)
}

func (r *reportingTokens) Issue(ctx context.Context, app string, permissions map[string]string, c string, exec func([]string, []byte) error) ([]protocol.Expiry, error) {
	r.rec.Add(c, audit.Credential{Kind: audit.GitHubToken, Result: audit.Issued, Account: "acme"}, "k")
	return r.fakeTokens.Issue(ctx, app, permissions, c, exec)
}

func TestStartLineHoldsItsOwnTokens(t *testing.T) {
	h, _ := startHost(&world{labels: goodLabels()}, &fakeCreds{})
	rec, path := audited(t, &h, "start", "")
	tokens := &reportingTokens{fakeTokens: &fakeTokens{}, rec: rec}
	h.GitHub = tokens
	_, err := h.Start(context.Background(), githubManifest(t))
	rec.Flush(err)
	if err != nil {
		t.Fatal(err)
	}
	if tokens.retryContainer != container {
		t.Fatalf("the recorder knew %q at the backlog retry", tokens.retryContainer)
	}
	if got := kinds(readLines(t, path)[0]); !slices.Equal(got, []string{"claude-setup-token issued", "github-installation-token issued"}) {
		t.Fatal(got)
	}
}

func TestStartFailureLine(t *testing.T) {
	t.Run("login", func(t *testing.T) {
		h, _ := startHost(&world{labels: goodLabels()}, &fakeCreds{ready: protocol.New(protocol.LoginRequired, "log in")})
		rec, path := audited(t, &h, "start", "")
		_, err := h.Start(context.Background(), validManifest(t))
		rec.Flush(err)
		l := readLines(t, path)[0]
		if l.Error == nil || *l.Error != "login_required" || l.Container != container || l.Image == nil || l.ImageDigest != nil || l.Mounts != nil || len(l.Credentials) != 0 {
			t.Fatalf("%+v", l)
		}
	})
	t.Run("rollback", func(t *testing.T) {
		h, _ := githubHost(&world{labels: goodLabels()}, &fakeTokens{issue: protocol.New(protocol.GitHubFailed, "GitHub request failed")})
		rec, path := audited(t, &h, "start", "")
		_, err := h.Start(context.Background(), githubManifest(t))
		rec.Flush(err)
		l := readLines(t, path)[0]
		if l.Error == nil || *l.Error != "github_failed" || !slices.Equal(kinds(l), []string{"claude-setup-token issued", "claude-setup-token withdrawn"}) {
			t.Fatalf("%+v %v", l, kinds(l))
		}
	})
	t.Run("rollback failed", func(t *testing.T) {
		w := &world{labels: goodLabels(), rmErr: errors.New("rm failed")}
		h, _ := githubHost(w, &fakeTokens{issue: protocol.New(protocol.GitHubFailed, "GitHub request failed")})
		rec, path := audited(t, &h, "start", "")
		_, err := h.Start(context.Background(), githubManifest(t))
		rec.Flush(err)
		if got := kinds(readLines(t, path)[0]); !slices.Equal(got, []string{"claude-setup-token issued"}) {
			t.Fatalf("a container the rollback could not remove still holds its credential: %v", got)
		}
	})
	t.Run("unreadable manifest", func(t *testing.T) {
		h, _ := startHost(&world{labels: goodLabels()}, &fakeCreds{})
		rec, path := audited(t, &h, "start", "")
		_, err := h.Start(context.Background(), filepath.Join(t.TempDir(), "absent.json"))
		rec.Flush(err)
		l := readLines(t, path)[0]
		if l.Error == nil || *l.Error != "manifest_rejected" || l.Container != "" || l.Session != "" || l.Image != nil || l.Mounts != nil {
			t.Fatalf("%+v", l)
		}
	})
}

func TestRefreshLineCodex(t *testing.T) {
	h, manifest, _, _, _ := refreshCodexHost(t, true)
	rec, path := audited(t, &h, "refresh", container)
	_, err := h.Refresh(context.Background(), container, manifest)
	rec.Flush(err)
	if err != nil {
		t.Fatal(err)
	}
	l := readLines(t, path)[0]
	if l.Event != "refresh" || l.Session != "worker_1" || l.Image != nil || l.Mounts != nil || !slices.Equal(kinds(l), []string{"codex-access-token renewed"}) ||
		*l.Credentials[0].ExpiresAt != "2026-10-18T12:00:00Z" {
		t.Fatalf("%+v", l)
	}
	data, _ := os.ReadFile(path)
	for _, key := range []string{`"image"`, `"image_digest"`, `"mounts"`} {
		if bytes.Contains(data, []byte(key)) {
			t.Fatalf("refresh line has %s: %s", key, data)
		}
	}
}

func TestCloseLine(t *testing.T) {
	claudeEnv := `["PATH=/usr/bin","CLAUDE_CONFIG_DIR=/Users/example/.ensemble/docker-state/worker_1/claude"]`
	codexEnv := `["CODEX_HOME=/Users/example/.ensemble/docker-state/worker_1/codex"]`
	config := func(env, labels string) string { return `{"env":` + env + `,"labels":` + labels + `}` }
	for name, tc := range map[string]struct {
		config  string
		inspect error
		rm      string
		want    []string
	}{
		"claude":                   {config(claudeEnv, `{"ensemble.client":"claude"}`), nil, container, []string{"claude-setup-token withdrawn"}},
		"codex":                    {config(codexEnv, `{"ensemble.client":"codex","team":"crew"}`), nil, container, []string{"codex-access-token withdrawn"}},
		"label beats image env":    {config(codexEnv, `{"ensemble.client":"opencode"}`), nil, container, nil},
		"label without env":        {config(`null`, `{"ensemble.client":"claude"}`), nil, container, []string{"claude-setup-token withdrawn"}},
		"unknown label":            {config(claudeEnv, `{"ensemble.client":"other"}`), nil, container, nil},
		"older broker: claude env": {config(claudeEnv, `null`), nil, container, []string{"claude-setup-token withdrawn"}},
		"older broker: codex env":  {config(codexEnv, `{"team":"crew"}`), nil, container, []string{"codex-access-token withdrawn"}},
		"absent":                   {"", &docker.CommandError{Exit: 1, Stderr: "No such container"}, "", nil},
		"inspect fails":            {"", errors.New("inspect"), container, nil},
		"older broker: no client":  {config(`["PATH=/usr/bin"]`, `null`), nil, container, nil},
		"malformed inspect output": {`["PATH=/usr/bin"]`, nil, container, nil},
		"env secrets": {config(claudeEnv, `{"ensemble.client":"claude","ensemble.secrets":"ensemble-env/dummy,ensemble-env/ssh"}`), nil, container,
			[]string{"claude-setup-token withdrawn", "env-secret withdrawn ensemble-env/dummy", "env-secret withdrawn ensemble-env/ssh"}},
		"env secrets, invalid entries": {config(`null`, `{"ensemble.client":"codex","ensemble.secrets":"ensemble-env/dummy,,ensemble-bot,ensemble-env/Bad"}`), nil, container,
			[]string{"codex-access-token withdrawn", "env-secret withdrawn ensemble-env/dummy"}},
		"env secrets, absent": {config(`null`, `{"ensemble.secrets":"ensemble-env/dummy"}`), nil, "", nil},
		"env secrets, no client": {config(`null`, `{"ensemble.client":"opencode","ensemble.secrets":"ensemble-env/dummy"}`), nil, container,
			[]string{"env-secret withdrawn ensemble-env/dummy"}},
	} {
		t.Run(name, func(t *testing.T) {
			d := &fakeDocker{respond: func(args []string) ([]byte, error) {
				if args[0] == "container" {
					if !slices.Equal(args, []string{"container", "inspect", "--format", `{"env":{{json .Config.Env}},"labels":{{json .Config.Labels}}}`, container}) {
						t.Errorf("%q", args)
					}
					return []byte(tc.config + "\n"), tc.inspect
				}
				return []byte(tc.rm + "\n"), nil
			}}
			h := Host{Docker: d, GitHub: &fakeTokens{}}
			rec, path := audited(t, &h, "close", container)
			got, err := h.Close(context.Background(), container)
			rec.Flush(err)
			if err != nil || got.Removed != (tc.rm != "") {
				t.Fatalf("%+v %v", got, err)
			}
			l := readLines(t, path)[0]
			if l.Event != "close" || l.Session != "worker_1" || l.Generation != generation || !slices.Equal(kinds(l), tc.want) {
				t.Fatalf("%+v %v", l, kinds(l))
			}
		})
	}
}

func TestNoSecretsInLog(t *testing.T) {
	path := audit.Path(t.TempDir())
	for _, client := range []string{"claude", "codex"} {
		h, _ := githubHost(&world{labels: goodLabels()}, &fakeTokens{})
		manifest := githubManifest(t)
		if client == "codex" {
			h.Codex, h.Versions = &fakeCodexCreds{}, &fakeVersions{target: "0.163.0"}
			raw, _ := os.ReadFile(codexManifest(t))
			var m map[string]any
			_ = json.Unmarshal(raw, &m)
			m["github"] = map[string]any{"app": "ensemble-bot", "permissions": fixtureSet}
			raw, _ = json.Marshal(m)
			manifest = writeManifest(t, raw)
		}
		h.Audit = &audit.Recorder{Command: "start", Log: audit.Log{Path: path}}
		_, err := h.Start(context.Background(), manifest)
		h.Audit.Flush(err)
		if err != nil {
			t.Fatal(err)
		}
		h.Audit = &audit.Recorder{Command: "close", Container: container, Log: audit.Log{Path: path}}
		_, err = h.Close(context.Background(), container)
		h.Audit.Flush(err)
	}
	data, err := os.ReadFile(path)
	if err != nil || bytes.Count(data, []byte("\n")) != 4 {
		t.Fatalf("%v %s", err, data)
	}
	for _, secret := range []string{fakeToken, "fake-access-only", "ghs_token"} {
		if bytes.Contains(data, []byte(secret)) {
			t.Fatalf("log holds %q", secret)
		}
	}
	info, err := os.Stat(path)
	if err != nil || info.Mode().Perm() != 0o600 {
		t.Fatalf("%v %v", info.Mode(), err)
	}
}
