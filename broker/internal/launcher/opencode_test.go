package launcher

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"testing"

	"ensemble/broker/internal/protocol"
)

const opencodeDir = home + "/.ensemble/docker-state/worker_1/opencode"

// opencodeManifest is manifest-valid.json for client opencode, edit applied to its fields.
func opencodeManifest(t *testing.T, edit func(map[string]any)) string {
	t.Helper()
	return editedManifest(t, func(fields map[string]any) {
		fields["client"], fields["opencode_dir"] = "opencode", opencodeDir
		delete(fields, "claude_dir")
		if edit != nil {
			edit(fields)
		}
	})
}

func withGitHub(fields map[string]any) {
	fields["github"] = map[string]any{"app": "ensemble-bot", "permissions": fixtureSet}
}

// opencodeHost fails any read of the Claude or Codex credentials and of the Codex version policy.
func opencodeHost(w *world, tokens *fakeTokens) (Host, *fakeDocker, *fakeCodexCreds) {
	h, d := githubHost(w, tokens)
	h.Creds = &fakeCreds{ready: errors.New("Claude must not be read"), injectErr: errors.New("Claude must not be injected")}
	codexCreds := &fakeCodexCreds{err: errors.New("Codex must not be read")}
	h.Codex, h.Versions = codexCreds, nil
	return h, d, codexCreds
}

func TestParseManifestOpencode(t *testing.T) {
	data, err := os.ReadFile(opencodeManifest(t, nil))
	if err != nil {
		t.Fatal(err)
	}
	m, err := ParseManifest(data)
	if err != nil || m.Client != "opencode" || m.OpencodeDir != opencodeDir || m.StateDir() != opencodeDir || m.ClaudeDir != "" {
		t.Fatalf("%+v %v", m, err)
	}
	other := func(client string, dirs ...string) func(map[string]any) {
		return func(f map[string]any) {
			f["client"] = client
			for _, dir := range dirs {
				f[dir] = "/x"
			}
		}
	}
	for name, edit := range map[string]func(map[string]any){
		"no opencode_dir":           func(f map[string]any) { delete(f, "opencode_dir") },
		"with claude_dir":           other("opencode", "claude_dir"),
		"with codex_dir":            other("opencode", "codex_dir"),
		"claude with opencode_dir":  other("claude", "claude_dir"),
		"codex with opencode_dir":   other("codex", "codex_dir"),
		"unknown client":            other("other"),
		"opencode_dir not a string": func(f map[string]any) { f["opencode_dir"] = 1 },
		"claude with providers": func(f map[string]any) {
			f["client"], f["claude_dir"], f["opencode_providers"] = "claude", "/x", []string{"strata"}
			delete(f, "opencode_dir")
		},
		"providers not an array": func(f map[string]any) { f["opencode_providers"] = "strata" },
		"provider not a string":  func(f map[string]any) { f["opencode_providers"] = []any{1} },
		"provider ollama":        func(f map[string]any) { f["opencode_providers"] = []string{"ollama"} },
		"provider path":          func(f map[string]any) { f["opencode_providers"] = []string{"../x"} },
		"provider too long":      func(f map[string]any) { f["opencode_providers"] = []string{strings.Repeat("p", 65)} },
		"duplicate provider":     func(f map[string]any) { f["opencode_providers"] = []string{"strata", "strata"} },
		"providers null":         func(f map[string]any) { f["opencode_providers"] = nil },
		"too many providers": func(f map[string]any) {
			var many []string
			for i := range 33 {
				many = append(many, fmt.Sprintf("p%d", i))
			}
			f["opencode_providers"] = many
		},
	} {
		t.Run(name, func(t *testing.T) {
			data, err := os.ReadFile(opencodeManifest(t, edit))
			if err != nil {
				t.Fatal(err)
			}
			if _, err := ParseManifest(data); code(t, err) != protocol.ManifestRejected {
				t.Fatalf("accepted: %v", err)
			}
		})
	}
}

func withProviders(providers ...string) func(map[string]any) {
	return func(f map[string]any) { f["opencode_providers"] = append([]string{}, providers...) }
}

func TestParseManifestOpencodeProviders(t *testing.T) {
	for name, tc := range map[string]struct {
		edit func(map[string]any)
		want []string
	}{
		"absent": {},
		"empty":  {edit: withProviders(), want: []string{}},
		"listed": {edit: withProviders("strata", "lab.local_1", "A-2"), want: []string{"strata", "lab.local_1", "A-2"}},
	} {
		data, err := os.ReadFile(opencodeManifest(t, tc.edit))
		if err != nil {
			t.Fatal(err)
		}
		m, err := ParseManifest(data)
		if err != nil || len(m.OpencodeProviders) != len(tc.want) || !slices.Equal(m.OpencodeProviders, tc.want) && len(tc.want) > 0 {
			t.Fatalf("%s: %q %v", name, m.OpencodeProviders, err)
		}
	}
}

// fakeKeys is the providers' API keys: ready and injectErr fail their calls; calls records each with its providers.
type fakeKeys struct {
	ready, injectErr error
	calls            []string
}

func (k *fakeKeys) Ready(providers []string) error {
	k.calls = append(k.calls, "ready "+strings.Join(providers, ","))
	return k.ready
}

func (k *fakeKeys) Inject(providers []string, exec func([]string, []byte) error) error {
	k.calls = append(k.calls, "inject "+strings.Join(providers, ","))
	for _, p := range providers {
		if err := exec([]string{"inject-opencode", p}, []byte(fakeToken)); err != nil {
			return err
		}
	}
	return k.injectErr
}

func TestStartOpencodeProviders(t *testing.T) {
	h, d, _ := opencodeHost(&world{labels: goodLabels()}, &fakeTokens{})
	keys := &fakeKeys{}
	h.Opencode = keys
	if _, err := h.Start(context.Background(), opencodeManifest(t, withProviders("strata", "lab"))); err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(keys.calls, []string{"ready strata,lab", "inject strata,lab"}) {
		t.Fatalf("%q", keys.calls)
	}
	created, injected := -1, -1
	for i, c := range d.calls {
		if c[0] == "run" && created < 0 {
			created = i
		}
		if slices.Contains(c, "inject-opencode") && injected < 0 {
			injected = i
			if !slices.Equal(c, []string{"exec", "--interactive", "--user", "agent", container, "inject-opencode", "strata"}) {
				t.Fatalf("%q", c)
			}
		}
	}
	if created < 0 || injected < created {
		t.Fatalf("injected before create: %q", d.calls)
	}
	for _, edit := range []func(map[string]any){nil, withProviders()} {
		h, _, _ := opencodeHost(&world{labels: goodLabels()}, &fakeTokens{})
		keys := &fakeKeys{}
		h.Opencode = keys
		if _, err := h.Start(context.Background(), opencodeManifest(t, edit)); err != nil || len(keys.calls) != 0 {
			t.Fatalf("no providers: %q %v", keys.calls, err)
		}
	}
}

func TestStartOpencodeProvidersFail(t *testing.T) {
	missing := protocol.New(protocol.LoginRequired, "No API key for OpenCode provider lab: run ensemble-broker login opencode lab.")
	h, d, _ := opencodeHost(&world{labels: goodLabels()}, &fakeTokens{})
	h.Opencode = &fakeKeys{ready: missing}
	_, err := h.Start(context.Background(), opencodeManifest(t, withProviders("strata", "lab")))
	if e := protoErr(t, err); e != missing || hasCall(d, "run") {
		t.Fatalf("%+v, created: %v", e, hasCall(d, "run"))
	}
	for name, injectErr := range map[string]error{
		"protocol": protocol.New(protocol.KeychainFailed, "Keychain access to the OpenCode provider lab API key failed."),
		"opaque":   errors.New("writing an OpenCode provider API key into the container failed"),
	} {
		tokens := &fakeTokens{}
		h, d, _ := opencodeHost(&world{labels: goodLabels()}, tokens)
		h.Opencode = &fakeKeys{injectErr: injectErr}
		_, err := h.Start(context.Background(), opencodeManifest(t, func(f map[string]any) { withGitHub(f); withProviders("lab")(f) }))
		e := protoErr(t, err)
		if e.Cleanup != protocol.Removed || !hasCall(d, "rm") || slices.ContainsFunc(tokens.calls, func(c string) bool { return strings.HasPrefix(c, "issue") }) {
			t.Fatalf("%s: %+v %q", name, e, tokens.calls)
		}
		if want := "Injecting the OpenCode provider API keys failed."; name == "opaque" && (e.Code != protocol.StartFailed || e.Message != want) ||
			name == "protocol" && e.Code != protocol.KeychainFailed {
			t.Fatalf("%s: %+v", name, e)
		}
	}
}

func TestCheckMountsOpencode(t *testing.T) {
	m := baseManifest()
	m.Client, m.ClaudeDir, m.OpencodeDir = "opencode", "", home+"/.ensemble/docker-state/w/opencode"
	if err := mountHost().checkMounts(m); err != nil {
		t.Fatal(err)
	}
	symlinked := mountHost()
	symlinked.Safe = func(string) error { return errors.New("symlink component") }
	wrong, inside := m, m
	wrong.OpencodeDir = home + "/.ensemble/docker-state/w/claude"
	inside.CWD = m.OpencodeDir + "/work"
	for name, tc := range map[string]struct {
		host   Host
		m      Manifest
		reason string
	}{
		"wrong dir":  {mountHost(), wrong, "opencode_dir must be " + m.OpencodeDir},
		"symlinked":  {symlinked, m, "opencode_dir " + m.OpencodeDir + " must be an existing directory without symlinks"},
		"cwd inside": {mountHost(), inside, "overlaps opencode_dir"},
	} {
		t.Run(name, func(t *testing.T) {
			e := protoErr(t, tc.host.checkMounts(tc.m))
			if e.Code != protocol.MountRejected || !strings.Contains(e.Message, tc.reason) {
				t.Fatalf("%+v", e)
			}
		})
	}
}

func TestStartOpencode(t *testing.T) {
	for _, github := range []bool{false, true} {
		tokens := &fakeTokens{}
		h, d, codexCreds := opencodeHost(&world{labels: goodLabels()}, tokens)
		var edit func(map[string]any)
		if github {
			edit = withGitHub
		}
		got, err := h.Start(context.Background(), opencodeManifest(t, edit))
		if err != nil {
			t.Fatal(err)
		}
		want := StartResult{Container: container, Endpoint: "unix://" + home + "/.docker/run/docker.sock", ImageDigest: digest,
			Clients: Clients{Claude: "2.1.0 (Claude Code)", Codex: "codex-cli 0.163.0", Opencode: "1.19.3"}, GitHub: []protocol.Expiry{}}
		if github {
			id := botID
			want.GitHub, want.GitHubUserID = expiry, &id
		}
		gotJSON, _ := json.Marshal(got)
		wantJSON, _ := json.Marshal(want)
		if string(gotJSON) != string(wantJSON) || codexCreds.ready != 0 || codexCreds.injected != 0 {
			t.Fatalf("got %s\nwant %s", gotJSON, wantJSON)
		}
		if issued := slices.Contains(tokens.calls, "issue ensemble-bot "+container); issued != github {
			t.Fatalf("github %v: %q", github, tokens.calls)
		}
		run := strings.Join(detached(d), " ")
		for _, needle := range []string{
			"--env XDG_DATA_HOME=" + opencodeDir, "--env OPENCODE_DISABLE_AUTOUPDATE=1", "--env OPENCODE_DISABLE_LSP_DOWNLOAD=1",
			"--label ensemble.client=opencode", "--mount type=bind,source=" + opencodeDir + ",target=" + opencodeDir,
			"--tmpfs /run/ensemble/secrets:rw,noexec,nosuid,nodev,size=65536,mode=0700,uid=1001,gid=1002",
		} {
			if !strings.Contains(run, needle) {
				t.Errorf("run lacks %q: %s", needle, run)
			}
		}
		if strings.Contains(run, "CLAUDE_CONFIG_DIR") || strings.Contains(run, "CODEX_HOME") || strings.Contains(run, "/claude") {
			t.Fatalf("other client's home in %s", run)
		}
		var hooked bool
		for _, c := range d.calls {
			if c[0] != "exec" {
				continue
			}
			if slices.Contains(c, Bootstrap) && !slices.Contains(c, "hook") {
				t.Fatalf("client bootstrap ran: %q", c)
			}
			if slices.Contains(c, "inject") || slices.Contains(c, "inject-codex") {
				t.Fatalf("client credential injected: %q", c)
			}
			hooked = hooked || slices.Contains(c, "hook")
		}
		if !hooked {
			t.Fatal("hook stub not installed")
		}
	}
}

func TestStartProbesOpencode(t *testing.T) {
	h, d := startHost(&world{labels: goodLabels()}, &fakeCreds{})
	if _, err := h.Start(context.Background(), validManifest(t)); err != nil {
		t.Fatal(err)
	}
	if !slices.ContainsFunc(d.calls, func(c []string) bool {
		return slices.Equal(c, []string{"exec", "--user", "agent", container, "/opt/ensemble/clients/bin/opencode", "--version"})
	}) {
		t.Fatalf("no opencode probe: %q", d.calls)
	}
	h, d = startHost(&world{labels: goodLabels(), versions: []string{"2.1.0\n", "codex-cli 0.163.0\n", ""}}, &fakeCreds{})
	_, err := h.Start(context.Background(), validManifest(t))
	if e := protoErr(t, err); e.Code != protocol.StartFailed || e.Message != "Cannot run the image's clients." || e.Cleanup != protocol.Removed {
		t.Fatalf("%+v", e)
	}
	if slices.ContainsFunc(d.calls, func(c []string) bool { return slices.Contains(c, "inject") }) {
		t.Fatal("injected after a failed probe")
	}
}

func TestOpencodeImageBelowMinimum(t *testing.T) {
	for _, version := range []string{"1.19.2\n", "1.19\n", "v1.19.3\n", "opencode 1.19.3\n"} {
		t.Run(strings.TrimSpace(version), func(t *testing.T) {
			tokens := &fakeTokens{}
			h, d, _ := opencodeHost(&world{labels: goodLabels(), versions: []string{"2.1.0\n", "codex-cli 0.163.0\n", version}}, tokens)
			rec, path := audited(t, &h, "start", "")
			_, err := h.Start(context.Background(), opencodeManifest(t, withGitHub))
			rec.Flush(err)
			e := protoErr(t, err)
			want := "Image OpenCode version is invalid or below the minimum; rebuild the worker image with OpenCode 1.19.3 or newer."
			if e.Code != protocol.StartFailed || e.Message != want || e.Cleanup != protocol.Removed || !hasCall(d, "rm") {
				t.Fatalf("%+v", e)
			}
			if slices.ContainsFunc(tokens.calls, func(c string) bool { return strings.HasPrefix(c, "issue") }) {
				t.Fatalf("issued GitHub tokens: %q", tokens.calls)
			}
			if l := readLines(t, path)[0]; len(l.Credentials) != 0 {
				t.Fatalf("client credential in audit: %+v", l.Credentials)
			}
		})
	}
	h, _ := startHost(&world{labels: goodLabels(), versions: []string{"2.1.0\n", "codex-cli 0.163.0\n", "1.0.0\n"}}, &fakeCreds{})
	got, err := h.Start(context.Background(), validManifest(t))
	if err != nil || got.Clients.Opencode != "1.0.0" {
		t.Fatalf("a Claude start checked the OpenCode minimum: %+v %v", got, err)
	}
}

func TestOpencodeAuditHasNoClientCredential(t *testing.T) {
	h, _, _ := opencodeHost(&world{labels: goodLabels()}, &fakeTokens{})
	rec, path := audited(t, &h, "start", "")
	_, err := h.Start(context.Background(), opencodeManifest(t, withGitHub))
	rec.Flush(err)
	if err != nil {
		t.Fatal(err)
	}
	if l := readLines(t, path)[0]; l.Event != "start" || len(l.Credentials) != 0 || l.Error != nil {
		t.Fatalf("%+v", l)
	}
	h, _, _ = opencodeHost(&world{labels: goodLabels()}, &fakeTokens{issue: protocol.New(protocol.GitHubFailed, "GitHub request failed")})
	rec, path = audited(t, &h, "start", "")
	_, err = h.Start(context.Background(), opencodeManifest(t, withGitHub))
	rec.Flush(err)
	if e := protoErr(t, err); e.Cleanup != protocol.Removed {
		t.Fatalf("%+v", e)
	}
	if l := readLines(t, path)[0]; len(l.Credentials) != 0 {
		t.Fatalf("rollback withdrew a client credential: %+v", l.Credentials)
	}
}

func TestRefreshOpencode(t *testing.T) {
	for _, github := range []bool{false, true} {
		tokens := &fakeTokens{}
		h, _, codexCreds := opencodeHost(&world{labels: goodLabels()}, tokens)
		h.Home = t.TempDir()
		dir := filepath.Join(h.Home, ".ensemble/docker-state/worker_1/opencode")
		manifest := opencodeManifest(t, func(f map[string]any) {
			f["opencode_dir"] = dir
			if github {
				withGitHub(f)
			}
		})
		rec, path := audited(t, &h, "refresh", container)
		got, err := h.Refresh(context.Background(), container, manifest)
		rec.Flush(err)
		if !github {
			if e := protoErr(t, err); e.Code != protocol.ManifestRejected || len(tokens.calls) != 0 {
				t.Fatalf("%+v %q", e, tokens.calls)
			}
			continue
		}
		if err != nil || got.CredentialExpiresAt != nil || !slices.Equal(got.GitHub, expiry) || codexCreds.injected != 0 ||
			!slices.Equal(tokens.calls, []string{"ready ensemble-bot", "refresh ensemble-bot " + container}) {
			t.Fatalf("%+v %v %q", got, err, tokens.calls)
		}
		if l := readLines(t, path)[0]; len(l.Credentials) != 0 {
			t.Fatalf("%+v", l.Credentials)
		}
	}
}

func TestCheckOpencode(t *testing.T) {
	for version, ok := range map[string]bool{
		"1.19.3": true, "1.19.10": true, "1.20.0": true, "2.0.0": true,
		"1.19.2": false, "1.9.3": false, "0.99.99": false, "1.19": false, "v1.19.3": false, "1.19.3-beta.1": false, "": false, "1.19.3 ": false,
	} {
		err := checkOpencode(version)
		if (err == nil) != ok {
			t.Errorf("%q: %v", version, err)
		}
		if err != nil && code(t, err) != protocol.StartFailed {
			t.Errorf("%q: %v", version, err)
		}
	}
}

func TestPinnedOpencodeMeetsMinimum(t *testing.T) {
	script, err := assets.ReadFile("assets/opencode-install.py")
	if err != nil {
		t.Fatal(err)
	}
	pinned := regexp.MustCompile(`(?m)^VERSION = "([^"]+)"$`).FindSubmatch(script)
	if pinned == nil || string(pinned[1]) != opencodeMinVersion {
		t.Fatalf("pinned %q, minimum %s", pinned, opencodeMinVersion)
	}
}
