package launcher

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"slices"
	"strings"
	"testing"
	"time"

	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/fixtures"
	"ensemble/broker/internal/protocol"
)

const (
	digest    = "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
	imageKey  = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	container = "ensemble-worker_1-0123456789abcdef0123456789abcdef"
	volume    = "ensemble-ws-worker_1"
	fakeToken = "FAKE-TOKEN"
)

type fakeCreds struct {
	ready     error
	injectErr error
	calls     []string // "ready <account>" and "inject <account>", in turn
}

func (c *fakeCreds) Ready(account string) error {
	c.calls = append(c.calls, "ready "+account)
	return c.ready
}
func (c *fakeCreds) Inject(account string, exec func(argv []string, stdin []byte) error) error {
	c.calls = append(c.calls, "inject "+account)
	if err := exec([]string{"inject"}, []byte(fakeToken)); err != nil {
		return err
	}
	return c.injectErr
}

func goodLabels() map[string]string {
	return map[string]string{"ensemble.key": imageKey, "ensemble.version": "1.2.3", "ensemble.agent-uid": "1001", "ensemble.agent-gid": "1002"}
}

// world is a scripted Docker Desktop: each field steers one kind of call.
type world struct {
	labels          map[string]string
	imageMissing    bool
	volume          string // "" absent, "matching", "mismatched" or "unlabeled"
	createUnlabeled bool   // a racing create leaves the volume unlabeled
	inUse           string // docker ps --filter volume= output
	psErr           error
	chownErr        error
	exists          bool // container already present
	runErr          error
	execErr         func(args []string) error
	versions        []string
	rmErr           error
	rmSilent        bool // rm --force exits 0 without echoing: the container was absent
	unreachable     bool
	proxies         *string              // the proxy listing's output; nil: the one healthy proxy
	proxy           func(map[string]any) // edits the proxy's inspect object
	network         func(map[string]any) // edits the workers network's inspect object
	noNetwork       bool
	egressErr       error  // the proxy listing's error
	nonce           string // the prefetch-start label a volume create set
	otherNonce      bool   // a racing start created the volume with its own prefetch-start label
	volumeRmErr     error
	prefetchErr     error // the prefetch helper's run
}

func (w *world) docker() *fakeDocker {
	probes := 0
	return &fakeDocker{respond: func(args []string) ([]byte, error) {
		if w.unreachable {
			return nil, docker.ErrUnreachable
		}
		switch strings.Join(args[:2], " ") {
		case "ps --all":
			if w.proxies != nil {
				return []byte(*w.proxies), w.egressErr
			}
			return []byte(proxyID + "\n"), w.egressErr
		case "network inspect":
			if w.noNetwork {
				return nil, &docker.CommandError{Exit: 1, Stderr: "Error: No such network: " + args[2]}
			}
			if args[2] == miniOpNetwork {
				return inspectObject(map[string]any{"Name": miniOpNetwork, "Id": miniOpID, "Internal": true}, w.network), nil
			}
			return inspectObject(networkObject(), w.network), nil
		case "image inspect":
			if w.imageMissing {
				return nil, noImage
			}
			return inspectJSON(digest, w.labels, time.Time{}), nil
		case "ps --filter":
			return []byte(w.inUse), w.psErr
		case "volume inspect":
			switch w.volume {
			case "matching":
				nonce := ""
				switch {
				case w.otherNonce:
					nonce = `,"ensemble.prefetch-start":"other"`
				case w.nonce != "":
					nonce = `,"ensemble.prefetch-start":"` + w.nonce + `"`
				}
				return []byte(`{"ensemble.kind":"ws","ensemble.workspace":"` + home + `/src/project"` + nonce + `}` + "\n"), nil
			case "mismatched":
				return []byte(`{"ensemble.kind":"ws","ensemble.workspace":"/elsewhere"}` + "\n"), nil
			case "unlabeled":
				return []byte("null\n"), nil
			}
			return nil, &docker.CommandError{Exit: 1, Stderr: "Error response from daemon: get " + volume + ": no such volume"}
		case "volume create":
			for i, a := range args {
				if a == "--label" && strings.HasPrefix(args[i+1], "ensemble.prefetch-start=") {
					w.nonce = strings.TrimPrefix(args[i+1], "ensemble.prefetch-start=")
				}
			}
			w.volume = "matching"
			if w.createUnlabeled {
				w.volume = "unlabeled"
			}
			return []byte(volume + "\n"), nil
		case "volume rm":
			if w.volumeRmErr != nil {
				return nil, w.volumeRmErr
			}
			w.volume = ""
			return []byte(volume + "\n"), nil
		case "container inspect":
			if args[2] == proxyID {
				return inspectObject(proxyObject(), w.proxy), nil
			}
			if w.exists {
				return []byte("[{}]"), nil
			}
			return nil, &docker.CommandError{Exit: 1, Stderr: "Error: No such container: " + container}
		}
		switch args[0] {
		case "run":
			if slices.Contains(args, "ensemble.prefetch=1") {
				return nil, w.prefetchErr
			}
			if args[1] == "--rm" {
				return nil, w.chownErr
			}
			return []byte("id\n"), w.runErr
		case "rm":
			if w.rmErr != nil || w.rmSilent {
				return nil, w.rmErr
			}
			return []byte(args[len(args)-1] + "\n"), nil
		case "exec":
			if w.execErr != nil {
				if err := w.execErr(args); err != nil {
					return nil, err
				}
			}
			if slices.Contains(args, "--version") {
				v := "2.1.0 (Claude Code)\n"
				if slices.ContainsFunc(args, func(s string) bool { return strings.HasSuffix(s, "/codex") || strings.HasSuffix(s, "/codex.next") }) {
					v = "codex-cli 0.163.0\n"
				}
				if slices.ContainsFunc(args, func(s string) bool { return strings.HasSuffix(s, "/opencode") }) {
					v = "1.19.3\n"
				}
				if probes < len(w.versions) {
					v = w.versions[probes]
				}
				probes++
				if v == "" {
					return nil, &docker.CommandError{Exit: 127, Stderr: "not found"}
				}
				return []byte(v), nil
			}
			return nil, nil
		}
		return nil, errors.New("unexpected " + strings.Join(args, " "))
	}}
}

const (
	proxyID        = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
	workersID      = "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
	workersNetwork = "egress-workers"
	workersProxy   = "http://egress-proxy-workers:4750"
	miniOpID       = "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
	miniOpNetwork  = "egress-mini-op"
	miniOpProxy    = "http://egress-proxy-mini-op:4750"
)

// proxyObject is a healthy contract-v1 proxy's docker inspect object, attached to egress-workers.
func proxyObject() map[string]any {
	return map[string]any{
		"Id":   proxyID,
		"Name": "/egress-proxy",
		"Config": map[string]any{"Labels": map[string]any{
			"windroc.egress.contract":              "1",
			"windroc.egress.networks":              "mini-op,workers",
			"windroc.egress.network.workers.name":  workersNetwork,
			"windroc.egress.network.workers.proxy": workersProxy,
			"windroc.egress.network.mini-op.name":  miniOpNetwork,
			"windroc.egress.network.mini-op.proxy": miniOpProxy,
			"com.docker.compose.service":           "proxy",
		}},
		"State": map[string]any{"Running": true, "Health": map[string]any{"Status": "healthy"}},
		"NetworkSettings": map[string]any{"Networks": map[string]any{
			workersNetwork: map[string]any{"NetworkID": workersID},
			miniOpNetwork:  map[string]any{"NetworkID": miniOpID},
		}},
	}
}

func networkObject() map[string]any {
	return map[string]any{"Name": workersNetwork, "Id": workersID, "Internal": true}
}

// inspectObject is docker inspect's one-element array of object, edit applied first.
func inspectObject(object map[string]any, edit func(map[string]any)) []byte {
	if edit != nil {
		edit(object)
	}
	data, err := json.Marshal([]any{object})
	if err != nil {
		panic(err)
	}
	return data
}

func writeManifest(t *testing.T, data []byte) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "m.json")
	if err := os.WriteFile(path, data, 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func validManifest(t *testing.T) string {
	data, err := os.ReadFile(filepath.Join(fixtures.Dir(), "manifest-valid.json"))
	if err != nil {
		t.Fatal(err)
	}
	return writeManifest(t, data)
}

// editedManifest is manifest-valid.json, edit applied to its fields.
func editedManifest(t *testing.T, edit func(map[string]any)) string {
	t.Helper()
	data, err := os.ReadFile(validManifest(t))
	if err != nil {
		t.Fatal(err)
	}
	var fields map[string]any
	if err := json.Unmarshal(data, &fields); err != nil {
		t.Fatal(err)
	}
	if edit != nil {
		edit(fields)
	}
	if data, err = json.Marshal(fields); err != nil {
		t.Fatal(err)
	}
	return writeManifest(t, data)
}

func withClaudeAccount(account any) func(map[string]any) {
	return func(f map[string]any) { f["claude_account"] = account }
}

func startHost(w *world, creds *fakeCreds) (Host, *fakeDocker) {
	d := w.docker()
	h := mountHost()
	h.Docker, h.Creds, h.Endpoint = d, creds, "unix://"+home+"/.docker/run/docker.sock"
	return h, d
}

func protoErr(t *testing.T, err error) *protocol.Error {
	t.Helper()
	var e *protocol.Error
	if !errors.As(err, &e) {
		t.Fatalf("not a protocol error: %v", err)
	}
	return e
}

func hasCall(d *fakeDocker, verb string) bool {
	return slices.Contains(d.verbs(), verb) || slices.ContainsFunc(d.calls, func(c []string) bool { return c[0] == verb })
}

func TestRuntimeFixtures(t *testing.T) {
	for _, c := range fixtures.Cases(t, "manifest") {
		if c.Expected.Stage != "runtime" {
			continue
		}
		t.Run(c.File, func(t *testing.T) {
			ctx := c.Context
			w := &world{labels: goodLabels()}
			switch ctx.Image {
			case "untrusted":
				w.labels = map[string]string{"ensemble.version": "1.2.3"}
			case "missing_version_label":
				delete(w.labels, "ensemble.version")
			}
			switch ctx.Docker {
			case "unreachable":
				w.unreachable = true
			case "sharing_denied":
				w.runErr = &docker.CommandError{Exit: 125, Stderr: "docker: Error response from daemon: mounts denied: \nThe path /Users/example/src/project is not shared from the host and is not known to Docker.\n\nRun 'docker run --help' for more information"}
				w.rmErr = &docker.CommandError{Exit: 1, Stderr: "Error: No such container: " + container}
			}
			switch ctx.Egress {
			case "stopped":
				w.proxy = func(g map[string]any) { g["State"].(map[string]any)["Running"] = false }
			case "unhealthy":
				w.proxy = func(g map[string]any) { g["State"].(map[string]any)["Health"] = map[string]any{"Status": "unhealthy"} }
			case "missing":
				none := ""
				w.proxies = &none
			case "not_internal":
				w.network = func(n map[string]any) { n["Internal"] = false }
			}
			creds := &fakeCreds{}
			switch ctx.Login {
			case "missing":
				creds.ready = protocol.New(protocol.LoginRequired, "run ensemble-broker login claude")
			case "denied":
				creds.ready = protocol.New(protocol.KeychainFailed, "denied")
			}
			switch ctx.Volume {
			case "matching", "mismatched":
				w.volume = ctx.Volume
			case "in_use":
				w.volume, w.inUse = "matching", "ensemble-other-0123456789abcdef0123456789abcdef\n"
			}
			d := w.docker()
			m, _ := ParseManifest(c.Data)
			h := Host{
				Home:     ctx.Home,
				Endpoint: "unix://" + ctx.Home + "/.docker/run/docker.sock",
				IsDir:    func(p string) bool { return ctx.CWDExists || p != m.CWD },
				Safe: func(string) error {
					if ctx.ClaudeDirHasSymlink {
						return errors.New("symlink component")
					}
					return nil
				},
				Docker:   d,
				Creds:    creds,
				GitHub:   &fakeTokens{},
				Versions: defaultVersions(),
			}
			_, err := h.Start(context.Background(), writeManifest(t, c.Data))
			e := protoErr(t, err)
			if string(e.Code) != *c.Expected.ErrorCode || protocol.Exit(e.Code) != *c.Expected.ExitCode {
				t.Fatalf("got %s (%s) want %s", e.Code, e.Message, *c.Expected.ErrorCode)
			}
			if e.Cleanup != protocol.NotNeeded {
				t.Fatalf("cleanup %s", e.Cleanup)
			}
			if e.Code == protocol.EgressUnavailable && len(creds.calls) != 0 {
				t.Fatalf("credentials touched before refusing: %q", creds.calls)
			}
			if e.Code != protocol.StartFailed && (hasCall(d, "run") || hasCall(d, "volume create")) {
				t.Fatalf("created a container before rejecting: %q", d.verbs())
			}
		})
	}
}

func TestExistingContainerUntouched(t *testing.T) {
	h, d := startHost(&world{labels: goodLabels(), exists: true}, &fakeCreds{})
	_, err := h.Start(context.Background(), validManifest(t))
	e := protoErr(t, err)
	if e.Code != protocol.StartFailed || e.Cleanup != protocol.NotNeeded || !strings.Contains(e.Message, "close it first") || hasCall(d, "run") || hasCall(d, "rm") {
		t.Fatalf("%+v %q", e, d.verbs())
	}
}

func TestRunArguments(t *testing.T) {
	h, d := startHost(&world{labels: goodLabels()}, &fakeCreds{})
	if _, err := h.Start(context.Background(), validManifest(t)); err != nil {
		t.Fatal(err)
	}
	run := detached(d)
	cwd, claude := home+"/src/project", home+"/.ensemble/docker-state/worker_1/claude"
	want := []string{"run", "--detach", "--name", container, "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", "agent",
		"--network", workersNetwork, "--env", "HTTP_PROXY=" + workersProxy, "--env", "HTTPS_PROXY=" + workersProxy,
		"--env", "http_proxy=" + workersProxy, "--env", "https_proxy=" + workersProxy,
		"--env", "NO_PROXY=localhost,127.0.0.1,::1", "--env", "no_proxy=localhost,127.0.0.1,::1",
		"--workdir", cwd, "--env", "CLAUDE_CONFIG_DIR=" + claude,
		"--env", "npm_config_cache=" + cwd + "/.ensemble-cache", "--env", "UV_CACHE_DIR=" + cwd + "/.ensemble-cache", "--env", "PIP_CACHE_DIR=" + cwd + "/.ensemble-cache",
		"--label", "ensemble.session=worker_1", "--label", "ensemble.generation=0123456789abcdef0123456789abcdef", "--label", "ensemble.client=claude", "--label", "team=crew",
		"--mount", "type=volume,source=" + volume + ",target=" + cwd + ",volume-nocopy", "--mount", "type=bind,source=" + claude + ",target=" + claude,
		"--tmpfs", "/run/ensemble/secrets:rw,noexec,nosuid,nodev,size=65536,mode=0700,uid=1001,gid=1002",
		"--entrypoint", "/usr/bin/tini", digest, "--", "sleep", "infinity"}
	if !slices.Equal(run, want) {
		t.Fatalf("run argv\n got %q\nwant %q", run, want)
	}
	for _, c := range d.calls {
		for _, a := range c {
			if strings.Contains(a, "docker.sock") || a == "--privileged" || a == "host" || strings.HasPrefix(a, "--device") {
				t.Fatalf("forbidden %q in %q", a, c)
			}
		}
	}
}

// detached is the worker's docker run call; nil if none ran.
func detached(d *fakeDocker) []string {
	for _, c := range d.calls {
		if len(c) > 1 && c[0] == "run" && c[1] == "--detach" {
			return c
		}
	}
	return nil
}

func TestStartVolume(t *testing.T) {
	create := []string{"volume", "create", "--label", "ensemble.kind=ws", "--label", "ensemble.workspace=" + home + "/src/project", volume}
	chown := []string{"run", "--rm", "--network", "none", "--cap-drop", "ALL", "--cap-add", "CHOWN", "--security-opt", "no-new-privileges", "--user", "0:0",
		"--mount", "type=volume,source=" + volume + ",target=/workspace,volume-nocopy", "--entrypoint", "/bin/chown", digest, "1001:1002", "/workspace"}
	has := func(d *fakeDocker, call []string) bool {
		return slices.ContainsFunc(d.calls, func(c []string) bool { return slices.Equal(c, call) })
	}
	h, d := startHost(&world{labels: goodLabels()}, &fakeCreds{})
	if _, err := h.Start(context.Background(), validManifest(t)); err != nil || !has(d, create) || !has(d, chown) {
		t.Fatalf("absent: %v %q", err, d.calls)
	}
	h, d = startHost(&world{labels: goodLabels(), volume: "matching"}, &fakeCreds{})
	if _, err := h.Start(context.Background(), validManifest(t)); err != nil || hasCall(d, "volume create") || hasCall(d, "run --rm") || detached(d) == nil {
		t.Fatalf("matching: %v %q", err, d.verbs())
	}
	if mount := "type=volume,source=" + volume + ",target=" + home + "/src/project,volume-nocopy"; !slices.Contains(detached(d), mount) {
		t.Fatalf("reused volume mount: %q", detached(d))
	}
	for name, w := range map[string]*world{
		"mismatched":         {volume: "mismatched"},
		"unlabeled":          {volume: "unlabeled"},
		"unlabeled by racer": {createUnlabeled: true},
		"in use":             {volume: "matching", inUse: "ensemble-other-0123456789abcdef0123456789abcdef\n"},
	} {
		w.labels = goodLabels()
		h, d := startHost(w, &fakeCreds{})
		_, err := h.Start(context.Background(), validManifest(t))
		e := protoErr(t, err)
		if e.Code != protocol.MountRejected || e.Cleanup != protocol.NotNeeded || hasCall(d, "run") || hasCall(d, "volume rm") {
			t.Fatalf("%s: %+v %q", name, e, d.verbs())
		}
		if name == "in use" && !strings.Contains(e.Message, "ensemble-other-") {
			t.Fatalf("in use: %q", e.Message)
		}
	}
	boom := &docker.CommandError{Exit: 1, Stderr: "boom"}
	for name, w := range map[string]*world{"chown": {chownErr: boom}, "ps after create": {psErr: boom}} {
		w.labels = goodLabels()
		h, d := startHost(w, &fakeCreds{})
		_, err := h.Start(context.Background(), validManifest(t))
		if e := protoErr(t, err); e.Code != protocol.StartFailed || !has(d, []string{"volume", "rm", volume}) || detached(d) != nil {
			t.Fatalf("%s: %+v %q", name, e, d.calls)
		}
	}
	h, d = startHost(&world{labels: goodLabels(), volume: "matching", psErr: boom}, &fakeCreds{})
	if _, err := h.Start(context.Background(), validManifest(t)); protoErr(t, err).Code != protocol.StartFailed || hasCall(d, "volume rm") {
		t.Fatalf("ps on an existing volume: %v %q", err, d.verbs())
	}
	h, _ = startHost(&world{labels: goodLabels(), unreachable: true}, &fakeCreds{})
	if _, err := h.Start(context.Background(), validManifest(t)); protoErr(t, err).Code != protocol.DockerUnreachable {
		t.Fatal(err)
	}
}

func TestStartOrder(t *testing.T) {
	h, d := startHost(&world{labels: goodLabels()}, &fakeCreds{})
	got, err := h.Start(context.Background(), validManifest(t))
	if err != nil {
		t.Fatal(err)
	}
	want := StartResult{Container: container, Endpoint: "unix://" + home + "/.docker/run/docker.sock", ImageDigest: digest,
		Clients: Clients{Claude: "2.1.0 (Claude Code)", Codex: "codex-cli 0.163.0", Opencode: "1.19.3"}, GitHub: []protocol.Expiry{}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("%+v", got)
	}
	var steps []string
	for i, c := range d.calls {
		step := strings.Join(c[:2], " ")
		if c[0] == "exec" {
			step = c[1] + " " + c[2] + " " + c[len(c)-1]
			if c[1] == "--user" {
				step = c[2] + " " + strings.Join(c[4:min(len(c), 7)], " ")
			}
		}
		if len(d.stdin[i]) > 0 && c[1] != "--interactive" {
			t.Fatalf("stdin on %q", c)
		}
		steps = append(steps, step)
	}
	wantSteps := []string{"ps --all", "container inspect", "network inspect", "image inspect", "container inspect", "volume inspect", "volume create", "volume inspect", "ps --filter", "run --rm", "run --detach",
		"agent /opt/ensemble/venv/bin/python -I /opt/ensemble/lib/bootstrap.py",
		"root /opt/ensemble/venv/bin/python -I /opt/ensemble/lib/bootstrap.py",
		"agent claude --version", "agent /opt/ensemble/clients/bin/codex --version", "agent /opt/ensemble/clients/bin/opencode --version",
		"--interactive --user inject"}
	if !slices.Equal(steps, wantSteps) {
		t.Fatalf("steps\n got %q\nwant %q", steps, wantSteps)
	}
	if inject := d.calls[len(d.calls)-1]; !slices.Equal(inject, []string{"exec", "--interactive", "--user", "agent", container, "inject"}) {
		t.Fatalf("inject %q", inject)
	}
	boot, hook := d.calls[11], d.calls[12]
	if !slices.Equal(boot[len(boot)-3:], []string{"claude", home + "/.ensemble/docker-state/worker_1/claude", home + "/src/project"}) ||
		!slices.Equal(hook[len(hook)-2:], []string{"hook", "/usr/local/bin/ensemble-session"}) {
		t.Fatalf("%q %q", boot, hook)
	}
	noHook, err := os.ReadFile(filepath.Join(fixtures.Dir(), "manifest-no-hook.json"))
	if err != nil {
		t.Fatal(err)
	}
	h, d = startHost(&world{labels: goodLabels()}, &fakeCreds{})
	if _, err := h.Start(context.Background(), writeManifest(t, noHook)); err != nil {
		t.Fatal(err)
	}
	for _, c := range d.calls {
		if slices.Contains(c, "root") {
			t.Fatalf("hook installed without hook_path: %q", c)
		}
	}
}

// A Claude start checks and injects only the manifest's account's setup-token; without one, the default account's.
func TestStartClaudeAccount(t *testing.T) {
	for want, path := range map[string]string{"default": validManifest(t), "work": editedManifest(t, withClaudeAccount("work"))} {
		creds := &fakeCreds{}
		h, _ := startHost(&world{labels: goodLabels()}, creds)
		if _, err := h.Start(context.Background(), path); err != nil {
			t.Fatal(err)
		}
		if !slices.Equal(creds.calls, []string{"ready " + want, "inject " + want}) {
			t.Fatalf("%s: %q", want, creds.calls)
		}
	}
}

func failOn(word string, err error) func([]string) error {
	return func(args []string) error {
		if slices.Contains(args, word) {
			return err
		}
		return nil
	}
}

func TestRollback(t *testing.T) {
	cmdErr := &docker.CommandError{Exit: 1, Stderr: "boom"}
	for name, tc := range map[string]struct {
		w       *world
		creds   *fakeCreds
		code    protocol.Code
		cleanup string
	}{
		"bootstrap":           {&world{execErr: failOn("claude", cmdErr)}, &fakeCreds{}, protocol.StartFailed, protocol.Removed},
		"hook":                {&world{execErr: failOn("hook", cmdErr)}, &fakeCreds{}, protocol.StartFailed, protocol.Removed},
		"clients":             {&world{versions: []string{"2.1.0\n", ""}}, &fakeCreds{}, protocol.StartFailed, protocol.Removed},
		"clients unreachable": {&world{execErr: failOn("--version", docker.ErrUnreachable)}, &fakeCreds{}, protocol.DockerUnreachable, protocol.Removed},
		"inject":              {&world{}, &fakeCreds{injectErr: errors.New("opaque")}, protocol.StartFailed, protocol.Removed},
		"inject unreachable":  {&world{}, &fakeCreds{injectErr: docker.ErrUnreachable}, protocol.DockerUnreachable, protocol.Removed},
		"inject keychain":     {&world{}, &fakeCreds{injectErr: protocol.New(protocol.KeychainFailed, "denied")}, protocol.KeychainFailed, protocol.Removed},
		"rm fails":            {&world{execErr: failOn("claude", cmdErr), rmErr: &docker.CommandError{Exit: 1, Stderr: "delete failed"}}, &fakeCreds{}, protocol.StartFailed, protocol.Failed},
		"run fails absent":    {&world{runErr: cmdErr, rmErr: &docker.CommandError{Exit: 1, Stderr: "No such container: x"}}, &fakeCreds{}, protocol.StartFailed, protocol.NotNeeded},
		"run fails present":   {&world{runErr: cmdErr}, &fakeCreds{}, protocol.StartFailed, protocol.Removed},
		"run fails rm silent": {&world{runErr: cmdErr, rmSilent: true}, &fakeCreds{}, protocol.StartFailed, protocol.NotNeeded},
	} {
		t.Run(name, func(t *testing.T) {
			tc.w.labels = goodLabels()
			h, d := startHost(tc.w, tc.creds)
			_, err := h.Start(context.Background(), validManifest(t))
			e := protoErr(t, err)
			if e.Code != tc.code || e.Cleanup != tc.cleanup || !hasCall(d, "rm") {
				t.Fatalf("%+v %q", e, d.verbs())
			}
			if tc.cleanup == protocol.Failed && (e.CleanupError == "" || e.Container != container) {
				t.Fatalf("%+v", e)
			}
			if strings.HasPrefix(name, "inject") && e.Cause != "" {
				t.Fatalf("inject cause leaked: %q", e.Cause)
			}
		})
	}
}

func TestRollbackAfterCancel(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	w := &world{labels: goodLabels(), execErr: func(args []string) error {
		if slices.Contains(args, "claude") {
			cancel()
			return context.Canceled
		}
		return nil
	}}
	h, d := startHost(w, &fakeCreds{})
	_, err := h.Start(ctx, validManifest(t))
	if e := protoErr(t, err); e.Cleanup != protocol.Removed {
		t.Fatalf("%+v", e)
	}
	last := len(d.calls) - 1
	if d.calls[last][0] != "rm" || d.ctxErrs[last] != nil {
		t.Fatalf("rollback ran on a cancelled context: %q %v", d.calls[last], d.ctxErrs[last])
	}
}

func TestSharingDenied(t *testing.T) {
	w := &world{labels: goodLabels(), runErr: &docker.CommandError{Exit: 125, Stderr: "docker: Error response from daemon: mounts denied: \nThe path /Users/example/src/project is not shared from the host and is not known to Docker.\n\nRun 'docker run --help' for more information"},
		rmErr: &docker.CommandError{Exit: 1, Stderr: "Error: No such container: x"}}
	h, _ := startHost(w, &fakeCreds{})
	_, err := h.Start(context.Background(), validManifest(t))
	e := protoErr(t, err)
	if e.Code != protocol.StartFailed || e.Cleanup != protocol.NotNeeded || !strings.Contains(e.Message, "file sharing") ||
		!strings.Contains(e.Message, "docker-state") || !strings.Contains(e.Cause, "not shared from the host") {
		t.Fatalf("%+v", e)
	}
}

func TestManifestUnreadable(t *testing.T) {
	h, _ := startHost(&world{labels: goodLabels()}, &fakeCreds{})
	if _, err := h.Start(context.Background(), "/nonexistent/m.json"); protoErr(t, err).Code != protocol.ManifestRejected {
		t.Fatal(err)
	}
}

func TestHookUnderUncleanCWD(t *testing.T) {
	m := `{"client":"claude","name":"worker_1","generation":"0123456789abcdef0123456789abcdef","image":"ensemble-worker:` + imageKey +
		`","cwd":"` + home + `/src/project/.","workspace":"ensemble-ws-worker_1","claude_dir":"` + home + `/.ensemble/docker-state/worker_1/claude","hook_path":"` + home + `/src/project/hook","github":null,"labels":{}}`
	h, d := startHost(&world{labels: goodLabels()}, &fakeCreds{})
	_, err := h.Start(context.Background(), writeManifest(t, []byte(m)))
	if protoErr(t, err).Code != protocol.MountRejected || hasCall(d, "run") {
		t.Fatalf("%v %q", err, d.verbs())
	}
}

func TestCauseOnlyWhereNamed(t *testing.T) {
	cmdErr := &docker.CommandError{Exit: 1, Stderr: "boom detail"}
	for name, w := range map[string]*world{
		"bootstrap": {execErr: failOn("claude", cmdErr)},
		"clients":   {execErr: failOn("--version", cmdErr)},
	} {
		w.labels = goodLabels()
		h, _ := startHost(w, &fakeCreds{})
		_, err := h.Start(context.Background(), validManifest(t))
		if e := protoErr(t, err); e.Cause != "" {
			t.Fatalf("%s: cause %q", name, e.Cause)
		}
	}
	h, _ := startHost(&world{labels: goodLabels(), runErr: cmdErr}, &fakeCreds{})
	_, err := h.Start(context.Background(), validManifest(t))
	if e := protoErr(t, err); e.Cause != "boom detail" {
		t.Fatalf("run cause %q", e.Cause)
	}
}

func TestNameConflictSkipsRollback(t *testing.T) {
	w := &world{labels: goodLabels(), runErr: &docker.CommandError{Exit: 125, Stderr: `docker: Error response from daemon: Conflict. The container name "/` + container +
		`" is already in use by container "abc". You have to remove (or rename) that container to be able to reuse that name.` + "\n\nRun 'docker run --help' for more information"}}
	h, d := startHost(w, &fakeCreds{})
	_, err := h.Start(context.Background(), validManifest(t))
	e := protoErr(t, err)
	if e.Code != protocol.StartFailed || e.Cleanup != protocol.NotNeeded || !strings.Contains(e.Message, "close it first") || hasCall(d, "rm") {
		t.Fatalf("%+v %q", e, d.verbs())
	}
}

func TestStartPhaseTimes(t *testing.T) {
	var progress strings.Builder
	h, _ := startHost(&world{labels: goodLabels()}, &fakeCreds{})
	h.Progress = &progress
	if _, err := h.Start(context.Background(), validManifest(t)); err != nil {
		t.Fatal(err)
	}
	line := regexp.MustCompile(`^ensemble-broker: start ([a-z_]+) [0-9]+\.[0-9]{2} s$`)
	var phases []string
	for _, l := range strings.Split(strings.TrimSuffix(progress.String(), "\n"), "\n") {
		m := line.FindStringSubmatch(l)
		if m == nil {
			t.Fatalf("line %q in %q", l, progress.String())
		}
		phases = append(phases, m[1])
	}
	if want := []string{"preflight", "image", "checks", "create", "bootstrap", "clients", "credentials", "total"}; !slices.Equal(phases, want) {
		t.Fatalf("phases %q want %q", phases, want)
	}
	for name, w := range map[string]*world{
		"image":     {labels: goodLabels(), imageMissing: true},
		"run":       {labels: goodLabels(), runErr: &docker.CommandError{Exit: 1, Stderr: "boom"}},
		"bootstrap": {labels: goodLabels(), execErr: failOn("claude", &docker.CommandError{Exit: 1, Stderr: "boom"})},
	} {
		progress.Reset()
		h, _ := startHost(w, &fakeCreds{})
		h.Progress = &progress
		if _, err := h.Start(context.Background(), validManifest(t)); err == nil || progress.Len() != 0 {
			t.Fatalf("%s: %v, progress %q", name, err, progress.String())
		}
	}
}

func TestCodexStartRunsNoClientUpdate(t *testing.T) {
	h, d := startHost(&world{labels: goodLabels()}, &fakeCreds{})
	v := defaultVersions()
	h.Versions, h.Codex = v, &fakeCodexCreds{}
	var progress strings.Builder
	h.Progress = &progress
	got, err := h.Start(context.Background(), codexManifest(t))
	if err != nil || got.Clients.Codex != "codex-cli 0.163.0" || !slices.Equal(v.calls, []string{"verify"}) {
		t.Fatalf("%+v %v %v", got, err, v.calls)
	}
	for _, call := range d.calls {
		joined := strings.Join(call, " ")
		if strings.Contains(joined, "codex-update") || strings.Contains(joined, "codex-probe") || strings.Contains(joined, "/opt/ensemble/runtime") {
			t.Fatalf("client update step: %v", call)
		}
	}
	if strings.Contains(progress.String(), "client_update") || strings.Contains(progress.String(), "update-codex") {
		t.Fatal(progress.String())
	}
	data, err := json.Marshal(got)
	if err != nil {
		t.Fatal(err)
	}
	var fields map[string]any
	if err := json.Unmarshal(data, &fields); err != nil {
		t.Fatal(err)
	}
	if _, present := fields["client_update"]; present {
		t.Fatal(string(data))
	}
}

// TestStartKeepsSecretsOutOfArgvAndEnv starts every credential kind: each fake secret reaches the container only on an
// exec's stdin, never in a Docker argument, so never in argv or the container's --env configuration.
func TestStartKeepsSecretsOutOfArgvAndEnv(t *testing.T) {
	secrets := []string{fakeToken, "ghs_token", "fake-access-only", envValue}
	for _, manifest := range []string{"manifest-valid.json", "manifest-codex.json", "manifest-opencode-providers.json", "manifest-github-app.json",
		"manifest-github-app.json" + withSecretsSuffix} {
		t.Run(manifest, func(t *testing.T) {
			h, d, _, _, _, _, _ := credentialHost(&world{labels: goodLabels()})
			if _, err := h.Start(context.Background(), credentialManifest(t, manifest)); err != nil {
				t.Fatal(err)
			}
			delivered := false
			for i, args := range d.calls {
				for _, s := range secrets {
					if strings.Contains(strings.Join(args, "\x00"), s) {
						t.Fatalf("docker %q carries a secret", args)
					}
					delivered = delivered || strings.Contains(string(d.stdin[i]), s)
				}
			}
			if !delivered {
				t.Fatal("no secret reached the container on stdin")
			}
		})
	}
}
