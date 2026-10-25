package launcher

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/protocol"
)

type fakeDocker struct {
	calls   [][]string
	stdin   [][]byte
	ctxErrs []error
	respond func(args []string) ([]byte, error)
}

func (f *fakeDocker) Run(ctx context.Context, args []string, stdin []byte) ([]byte, error) {
	f.calls = append(f.calls, slices.Clone(args))
	f.stdin = append(f.stdin, slices.Clone(stdin))
	f.ctxErrs = append(f.ctxErrs, ctx.Err())
	if f.respond != nil {
		return f.respond(args)
	}
	return nil, nil
}

func (f *fakeDocker) verbs() []string {
	var out []string
	for _, c := range f.calls {
		out = append(out, strings.Join(c[:min(2, len(c))], " "))
	}
	return out
}

func putFile(t *testing.T, path, text string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(text), 0o644); err != nil {
		t.Fatal(err)
	}
}

func inspectJSON(id string, labels map[string]string, created time.Time) []byte {
	data, _ := json.Marshal([]map[string]any{{"Id": id, "Created": created, "Architecture": "arm64", "Config": map[string]any{"Labels": labels}}})
	return data
}

var noImage = &docker.CommandError{Exit: 1, Stderr: "Error response from daemon: No such image: x"}

func TestSnapshotKey(t *testing.T) {
	src, dst := t.TempDir(), t.TempDir()
	putFile(t, filepath.Join(src, "Dockerfile"), "FROM base\n")
	putFile(t, filepath.Join(src, "__pycache__/x.pyc"), "junk")
	putFile(t, filepath.Join(src, "mod.pyc"), "junk")
	if err := os.Symlink("Dockerfile", filepath.Join(src, "link")); err != nil {
		t.Fatal(err)
	}
	if err := snapshotTree(src, dst); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Lstat(filepath.Join(dst, "__pycache__")); !os.IsNotExist(err) {
		t.Fatal("pycache copied")
	}
	a, err := treeDigest(dst)
	if err != nil {
		t.Fatal(err)
	}
	putFile(t, filepath.Join(src, "Dockerfile"), "FROM changed\n")
	if b, _ := treeDigest(dst); a != b {
		t.Fatal("snapshot changed after source write")
	}
	if err := snapshotTree(src, dst); err != nil {
		t.Fatal(err)
	}
	b, _ := treeDigest(dst)
	if a == b {
		t.Fatal("content did not change key")
	}
	if err := os.Chmod(filepath.Join(dst, "Dockerfile"), 0o755); err != nil {
		t.Fatal(err)
	}
	if c, _ := treeDigest(dst); b == c {
		t.Fatal("mode did not change key")
	}
}

type buildEnv struct {
	home, pkg string
	fake      *fakeDocker
	key       string
	built     map[string]map[string]string
	probe     string
	failStage string
	// images answers image ls, KEY standing for the built key, and dangling its untagged listing;
	// containers and used answer container ls and inspect.
	images, dangling, containers, used string
	age                                time.Duration // of every inspected image
	listErr, rmErr                     error
	progress                           strings.Builder
	// held lists the Docker verbs each hold of the image lock spanned; lockFail fails its nth taking (from 1).
	held            [][]string
	locks, lockFail int
	locked          bool
}

func newBuildEnv(t *testing.T) *buildEnv {
	e := &buildEnv{home: t.TempDir(), pkg: t.TempDir(), built: map[string]map[string]string{}, probe: "1001\n1002\n"}
	putFile(t, filepath.Join(e.home, ".ensemble/docker/worker/Dockerfile"), "FROM base")
	putFile(t, filepath.Join(e.pkg, ".claude-plugin/plugin.json"), `{"version":"1.2.3"}`)
	putFile(t, filepath.Join(e.pkg, "session/src/sample.py"), "session")
	putFile(t, filepath.Join(e.pkg, "utils/src/sample.py"), "utils")
	putFile(t, filepath.Join(e.pkg, "delegate/src/sample.py"), "delegate")
	e.fake = &fakeDocker{respond: func(args []string) ([]byte, error) {
		switch args[0] {
		case "build":
			tag := args[slices.Index(args, "--tag")+1]
			if strings.HasPrefix(tag, e.failStage+":") && e.failStage != "" {
				return nil, &docker.CommandError{Exit: 1, Stderr: "step 3 failed\nRUN false"}
			}
			labels := map[string]string{}
			for i, a := range args {
				if a == "--label" {
					k, v, _ := strings.Cut(args[i+1], "=")
					labels[k] = v
				}
				if a == "--build-arg" {
					k, v, _ := strings.Cut(args[i+1], "=")
					switch k {
					case "ENSEMBLE_VERSION":
						labels["ensemble.version"] = v
					case "AGENT_UID":
						labels["ensemble.agent-uid"] = v
					case "AGENT_GID":
						labels["ensemble.agent-gid"] = v
					}
				}
			}
			e.built[tag] = labels
			if k, ok := strings.CutPrefix(tag, "ensemble-worker:"); ok {
				e.key = k
			}
			return nil, nil
		case "run":
			return []byte(e.probe), nil
		case "image":
			switch args[1] {
			case "ls":
				if slices.Contains(args, "--filter") {
					return []byte(e.dangling), nil
				}
				return []byte(strings.ReplaceAll(e.images, "KEY", e.key)), nil
			case "rm":
				return nil, e.rmErr
			}
			labels, ok := e.built[args[len(args)-1]]
			if !ok {
				return nil, noImage
			}
			return inspectJSON("sha256:"+strings.Repeat("a", 64), labels, time.Now().Add(-e.age)), nil
		case "container":
			if args[1] == "ls" {
				return []byte(e.containers), e.listErr
			}
			return []byte(e.used), nil
		}
		return nil, errors.New("unexpected " + strings.Join(args, " "))
	}}
	return e
}

func (e *buildEnv) host() Host {
	return Host{Home: e.home, Docker: e.fake, Progress: &e.progress, Lock: e.lock}
}

// lock is a non-reentrant image lock: taking it while held fails, as a second process would wait.
func (e *buildEnv) lock() (func(), error) {
	e.locks++
	if e.locked || e.locks == e.lockFail {
		return nil, errors.New("lock busy")
	}
	e.locked = true
	from := len(e.fake.calls)
	return func() {
		e.locked = false
		e.held = append(e.held, e.fake.verbs()[from:])
	}, nil
}

func (e *buildEnv) leasePath(key string) string {
	return filepath.Join(e.home, ".ensemble/docker/images", key)
}

func TestBuildCacheAndStages(t *testing.T) {
	e := newBuildEnv(t)
	first, err := e.host().Build(context.Background(), e.pkg, nil, -1)
	if err != nil {
		t.Fatal(err)
	}
	if !first.Rebuilt || first.Image != "ensemble-worker:"+e.key || first.Digest != "sha256:"+strings.Repeat("a", 64) {
		t.Fatalf("%+v", first)
	}
	if got := e.fake.verbs(); !slices.Equal(got, []string{"image inspect", "build --progress=plain", "run --rm", "build --progress=plain", "image inspect", "container ls", "image ls", "image ls"}) {
		t.Fatalf("%q", got)
	}
	stage1 := e.fake.calls[1]
	if want := []string{"build", "--progress=plain", "--no-cache", "--label", "ensemble.key=" + e.key, "--tag", "ensemble-user:" + e.key}; !slices.Equal(stage1[:len(want)], want) || len(stage1) != len(want)+1 {
		t.Fatalf("%q", stage1)
	}
	probe := e.fake.calls[2]
	wantProbe := []string{"run", "--rm", "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--entrypoint", "/bin/sh", "ensemble-user:" + e.key, "-c", "id -u agent && id -g agent"}
	if !slices.Equal(probe, wantProbe) {
		t.Fatalf("%q", probe)
	}
	stage2 := e.fake.calls[3]
	want := []string{"build", "--progress=plain", "--no-cache", "--build-arg", "USER_IMAGE=ensemble-user:" + e.key, "--build-arg", "ENSEMBLE_VERSION=1.2.3", "--build-arg", "AGENT_UID=1001", "--build-arg", "AGENT_GID=1002", "--build-arg", "ENSEMBLE_PLUGIN_DIRS=", "--label", "ensemble.key=" + e.key, "--tag", "ensemble-worker:" + e.key}
	if !slices.Equal(stage2[:len(want)], want) || len(stage2) != len(want)+1 {
		t.Fatalf("%q", stage2)
	}
	for _, c := range e.fake.calls {
		for _, a := range c {
			if a == "--secret" || a == "--ssh" {
				t.Fatalf("forbidden flag in %q", c)
			}
		}
	}
	calls := len(e.fake.calls)
	second, err := e.host().Build(context.Background(), e.pkg, nil, -1)
	if err != nil || second.Rebuilt || second.Image != first.Image || len(e.fake.calls) != calls+1 {
		t.Fatalf("%+v %v %q", second, err, e.fake.verbs())
	}
	putFile(t, filepath.Join(e.pkg, "session/src/sample.py"), "changed")
	third, err := e.host().Build(context.Background(), e.pkg, nil, -1)
	if err != nil || !third.Rebuilt || third.Image == first.Image {
		t.Fatalf("%+v %v", third, err)
	}
	putFile(t, filepath.Join(e.pkg, "delegate/src/sample.py"), "changed")
	if team, err := e.host().Build(context.Background(), e.pkg, nil, -1); err != nil || !team.Rebuilt || team.Image == third.Image {
		t.Fatalf("%+v %v", team, err)
	}
	putFile(t, filepath.Join(e.home, ".ensemble/docker/worker/Dockerfile"), "FROM other")
	fourth, err := e.host().Build(context.Background(), e.pkg, nil, -1)
	if err != nil || !fourth.Rebuilt || fourth.Image == third.Image {
		t.Fatalf("%+v %v", fourth, err)
	}
}

func TestBuildFailures(t *testing.T) {
	code := func(err error) protocol.Code {
		var e *protocol.Error
		if !errors.As(err, &e) {
			t.Fatalf("not a protocol error: %v", err)
		}
		return e.Code
	}
	for name, mutate := range map[string]func(*buildEnv){
		"missing definition": func(e *buildEnv) { os.RemoveAll(filepath.Join(e.home, ".ensemble")) },
		"missing version":    func(e *buildEnv) { os.Remove(filepath.Join(e.pkg, ".claude-plugin/plugin.json")) },
		"bad version":        func(e *buildEnv) { putFile(t, filepath.Join(e.pkg, ".claude-plugin/plugin.json"), `{"version":"v1"}`) },
		"missing session":    func(e *buildEnv) { os.RemoveAll(filepath.Join(e.pkg, "session")) },
		"missing delegate":   func(e *buildEnv) { os.RemoveAll(filepath.Join(e.pkg, "delegate")) },
		"root agent":         func(e *buildEnv) { e.probe = "0\n0\n" },
		"probe garbage":      func(e *buildEnv) { e.probe = "agent\n" },
		"stage one":          func(e *buildEnv) { e.failStage = "ensemble-user" },
		"stage two":          func(e *buildEnv) { e.failStage = "ensemble-worker" },
	} {
		t.Run(name, func(t *testing.T) {
			e := newBuildEnv(t)
			mutate(e)
			_, err := e.host().Build(context.Background(), e.pkg, nil, -1)
			if code(err) != protocol.BuildFailed {
				t.Fatal(err)
			}
			if name == "missing definition" && !strings.Contains(err.(*protocol.Error).Message, "broker/install.sh") {
				t.Fatal(err)
			}
			if name == "stage one" && !strings.Contains(err.(*protocol.Error).Cause, "RUN false") {
				t.Fatalf("cause %q", err.(*protocol.Error).Cause)
			}
		})
	}
	t.Run("label mismatch", func(t *testing.T) {
		e := newBuildEnv(t)
		inner := e.fake.respond
		e.fake.respond = func(args []string) ([]byte, error) {
			if args[0] == "image" && len(e.built) == 2 {
				return inspectJSON("sha256:"+strings.Repeat("b", 64), map[string]string{"ensemble.version": "9.9.9"}, time.Time{}), nil
			}
			return inner(args)
		}
		if _, err := e.host().Build(context.Background(), e.pkg, nil, -1); code(err) != protocol.BuildFailed {
			t.Fatal(err)
		}
	})
	t.Run("unreachable", func(t *testing.T) {
		e := newBuildEnv(t)
		e.fake.respond = func([]string) ([]byte, error) { return nil, docker.ErrUnreachable }
		if _, err := e.host().Build(context.Background(), e.pkg, nil, -1); code(err) != protocol.DockerUnreachable {
			t.Fatal(err)
		}
	})
}

func TestImageProvenance(t *testing.T) {
	for name, w := range map[string]*world{
		"absent":     {imageMissing: true},
		"no key":     {labels: map[string]string{"ensemble.version": "1", "ensemble.agent-uid": "1", "ensemble.agent-gid": "1"}},
		"wrong key":  {labels: map[string]string{"ensemble.key": strings.Repeat("c", 64), "ensemble.version": "1", "ensemble.agent-uid": "1", "ensemble.agent-gid": "1"}},
		"bad uid":    {labels: map[string]string{"ensemble.key": imageKey, "ensemble.version": "1", "ensemble.agent-uid": "x", "ensemble.agent-gid": "1"}},
		"no version": {labels: map[string]string{"ensemble.key": imageKey, "ensemble.agent-uid": "1", "ensemble.agent-gid": "1"}},
	} {
		t.Run(name, func(t *testing.T) {
			h, d := startHost(w, &fakeCreds{})
			_, err := h.Start(context.Background(), validManifest(t))
			if protoErr(t, err).Code != protocol.ImageRejected || hasCall(d, "run") {
				t.Fatalf("%v %q", err, d.verbs())
			}
		})
	}
}

func TestWorkerImageHasItsScripts(t *testing.T) {
	data, err := assets.ReadFile("assets/Dockerfile")
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{
		`'exec /opt/ensemble/venv/bin/python -I /opt/ensemble/session/src/ensemble/session/hosts/docker/relay.py "$@"' > /opt/ensemble/bin/relay`,
		"chmod 755 /opt/ensemble/bin/codex /opt/ensemble/bin/ensemble-session /opt/ensemble/bin/relay ",
		"install -d -o agent -g agent -m 0700 /run/ensemble /run/ensemble/relay-queue",
		"COPY bootstrap.py codex-update.py opencode-install.py plugin-install.py hook-stub with-secrets workspace-check gh git-credential-ensemble /opt/ensemble/lib/",
		`python3 -I /opt/ensemble/lib/opencode-install.py "$(dpkg --print-architecture)" /opt/ensemble/clients`,
		"ln -s /opt/ensemble/clients/bin/opencode /opt/ensemble/bin/opencode",
		"COPY codex /opt/ensemble/bin/codex",
		"ln -s /opt/ensemble/lib/gh /opt/ensemble/bin/gh",
		"ln -s /opt/ensemble/lib/git-credential-ensemble /opt/ensemble/bin/git-credential-ensemble",
		" /opt/ensemble/lib/workspace-check /opt/ensemble/lib/gh /opt/ensemble/lib/git-credential-ensemble \\\n",
		`printf '%s\n' '[credential]' 'helper = /opt/ensemble/bin/git-credential-ensemble' 'useHttpPath = true' >> /etc/gitconfig`,
		`printf '%s\n' '[core]' 'hooksPath = .githooks' >> /etc/gitconfig`,
		"ENV PATH=/opt/ensemble/bin:",
		`printf '%s\n' 'PATH=/opt/ensemble/bin:$PATH' > /etc/profile.d/ensemble.sh`,
		"COPY delegate/src /opt/ensemble/delegate/src\n",
		"python3 -m venv --system-site-packages /opt/ensemble/venv",
		`printf '%s\n' /opt/ensemble/delegate/src /opt/ensemble/session/src /opt/ensemble/utils/src > "$(/opt/ensemble/venv/bin/python -c 'import sysconfig; print(sysconfig.get_path("purelib"))')/ensemble.pth"`,
		`printf '%s\n' '#!/bin/sh' 'exec /opt/ensemble/venv/bin/python "$@"' > /opt/ensemble/bin/python3`,
		"ln -s /opt/ensemble/delegate/src/ensemble/delegate/team /opt/ensemble/team",
	} {
		if !strings.Contains(string(data), want) {
			t.Errorf("Dockerfile lacks %q", want)
		}
	}
}

func TestBuildContextOutsideTMPDIR(t *testing.T) {
	e := newBuildEnv(t)
	t.Setenv("TMPDIR", t.TempDir())
	if _, err := e.host().Build(context.Background(), e.pkg, nil, -1); err != nil {
		t.Fatal(err)
	}
	base := filepath.Join(e.home, ".ensemble/docker/tmp") + "/"
	for _, i := range []int{1, 3} {
		if dir := e.fake.calls[i][len(e.fake.calls[i])-1]; !strings.HasPrefix(dir, base) {
			t.Fatalf("build context %s outside %s", dir, base)
		}
	}
}

func TestBuildRemovesOlderImages(t *testing.T) {
	old, kept, id := strings.Repeat("1", 64), strings.Repeat("2", 64), func(c string) string { return "sha256:" + strings.Repeat(c, 64) }
	listing := strings.Join([]string{
		"ensemble-worker:KEY " + id("a"), "ensemble-user:KEY " + id("b"),
		"ensemble-worker:" + old + " " + id("c"), "ensemble-user:" + old + " " + id("d"),
		"ensemble-worker:" + kept + " " + id("e"), "ensemble-user:" + kept + " " + id("f"),
		"ensemble-worker:latest " + id("7"), "ubuntu:24.04 " + id("8"), "<none>:<none> " + id("9"), "",
	}, "\n")
	removed := func(e *buildEnv) []string {
		var refs []string
		for _, c := range e.fake.calls {
			if strings.Join(c[:2], " ") == "image rm" {
				refs = append(refs, c[2:]...)
			}
		}
		return refs
	}
	t.Run("keeps the current pair and images containers use", func(t *testing.T) {
		e := newBuildEnv(t)
		e.images, e.containers, e.used = listing, "c1\nc2\n", id("e")+"\n"+id("0")+"\n"
		if _, err := e.host().Build(context.Background(), e.pkg, nil, -1); err != nil {
			t.Fatal(err)
		}
		want := []string{"ensemble-worker:" + old, "ensemble-user:" + old, "ensemble-user:" + kept}
		if got := removed(e); !slices.Equal(got, want) {
			t.Fatalf("%q", got)
		}
		inspect := e.fake.calls[slices.Index(e.fake.verbs(), "container inspect")]
		if !slices.Equal(inspect, []string{"container", "inspect", "--format", "{{.Image}}", "c1", "c2"}) {
			t.Fatalf("%q", inspect)
		}
		if got := e.progress.String(); got != "ensemble-broker: removed ensemble-worker:"+old+"\nensemble-broker: removed ensemble-user:"+old+"\nensemble-broker: removed ensemble-user:"+kept+"\n" {
			t.Fatalf("%q", got)
		}
		calls := len(e.fake.calls)
		if again, err := e.host().Build(context.Background(), e.pkg, nil, -1); err != nil || again.Rebuilt || len(e.fake.calls) != calls+1 {
			t.Fatalf("a reused image pruned: %v %q", err, e.fake.verbs()[calls:])
		}
	})
	t.Run("a failed removal keeps the image", func(t *testing.T) {
		e := newBuildEnv(t)
		e.images, e.rmErr = listing, &docker.CommandError{Exit: 1, Stderr: "Error response from daemon: conflict: image is in use"}
		if _, err := e.host().Build(context.Background(), e.pkg, nil, -1); err != nil {
			t.Fatal(err)
		}
		if !strings.Contains(e.progress.String(), "ensemble-broker: kept ensemble-worker:"+old+": Error response from daemon: conflict: image is in use\n") {
			t.Fatalf("%q", e.progress.String())
		}
	})
	t.Run("unknown containers keep every image", func(t *testing.T) {
		e := newBuildEnv(t)
		e.images, e.listErr = listing, &docker.CommandError{Exit: 1, Stderr: "boom"}
		if _, err := e.host().Build(context.Background(), e.pkg, nil, -1); err != nil {
			t.Fatal(err)
		}
		if got := removed(e); got != nil || e.progress.String() != "ensemble-broker: kept older images: cannot list containers: boom\n" {
			t.Fatalf("%q %q", got, e.progress.String())
		}
	})
	t.Run("keeps the images of keys a build leased within the lease", func(t *testing.T) {
		e := newBuildEnv(t)
		e.images = listing
		stale := time.Now().Add(-imageLease - time.Minute)
		for _, key := range []string{old, kept} {
			putFile(t, e.leasePath(key), "")
		}
		if err := os.Chtimes(e.leasePath(old), stale, stale); err != nil {
			t.Fatal(err)
		}
		if _, err := e.host().Build(context.Background(), e.pkg, nil, -1); err != nil {
			t.Fatal(err)
		}
		if got := removed(e); !slices.Equal(got, []string{"ensemble-worker:" + old, "ensemble-user:" + old}) {
			t.Fatalf("%q", got)
		}
		if _, err := os.Stat(e.leasePath(old)); !os.IsNotExist(err) {
			t.Fatalf("a removed key keeps its lease: %v", err)
		}
		if _, err := os.Stat(e.leasePath(kept)); err != nil {
			t.Fatal(err)
		}
	})
}

func TestBuildRefreshesAnOldImage(t *testing.T) {
	const week = 7 * 24 * time.Hour
	e := newBuildEnv(t)
	if _, err := e.host().Build(context.Background(), e.pkg, nil, -1); err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		name    string
		age     time.Duration
		maxAge  time.Duration
		rebuilt bool
	}{
		{"young", 6 * 24 * time.Hour, week, false},
		{"old", 8 * 24 * time.Hour, week, true},
		{"any age", 400 * 24 * time.Hour, -1, false},
		{"forced", time.Minute, 0, true},
		{"built in the future", -time.Hour, week, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			e.age = tc.age
			calls := len(e.fake.calls)
			got, err := e.host().Build(context.Background(), e.pkg, nil, tc.maxAge)
			if err != nil || got.Rebuilt != tc.rebuilt || got.Image != "ensemble-worker:"+e.key {
				t.Fatalf("%+v %v", got, err)
			}
			if !tc.rebuilt {
				if len(e.fake.calls) != calls+1 {
					t.Fatalf("%q", e.fake.verbs()[calls:])
				}
				return
			}
			if stage1 := e.fake.calls[calls+1]; stage1[0] != "build" || stage1[2] != "--no-cache" {
				t.Fatalf("a refresh may reuse cached clients: %q", stage1)
			}
		})
	}
}

func TestBuildRemovesUntaggedImages(t *testing.T) {
	id := func(c string) string { return "sha256:" + strings.Repeat(c, 64) }
	e := newBuildEnv(t)
	e.dangling, e.containers, e.used = id("3")+"\n"+id("4")+"\n", "c1\n", id("4")+"\n"
	if _, err := e.host().Build(context.Background(), e.pkg, nil, -1); err != nil {
		t.Fatal(err)
	}
	listing := e.fake.calls[len(e.fake.calls)-2]
	if !slices.Equal(listing, []string{"image", "ls", "--quiet", "--no-trunc", "--filter", "dangling=true", "--filter", "label=ensemble.key"}) {
		t.Fatalf("%q", listing)
	}
	if rm := e.fake.calls[len(e.fake.calls)-1]; !slices.Equal(rm, []string{"image", "rm", id("3")}) {
		t.Fatalf("%q", rm)
	}
	if got := e.progress.String(); got != "ensemble-broker: removed "+id("3")+"\n" {
		t.Fatalf("%q", got)
	}
}

func TestAFailedRefreshKeepsTheImage(t *testing.T) {
	e := newBuildEnv(t)
	first, err := e.host().Build(context.Background(), e.pkg, nil, -1)
	if err != nil {
		t.Fatal(err)
	}
	e.age, e.failStage = 8*24*time.Hour, "ensemble-user"
	e.progress.Reset()
	got, err := e.host().Build(context.Background(), e.pkg, nil, 7*24*time.Hour)
	if err != nil || got != (BuildResult{Image: first.Image, Digest: first.Digest}) {
		t.Fatalf("%+v %v", got, err)
	}
	if want := "ensemble-broker: kept " + first.Image + ": refreshing it failed: Building the worker image definition failed. (RUN false)\n"; e.progress.String() != want {
		t.Fatalf("%q", e.progress.String())
	}
	if _, err := e.host().Build(context.Background(), e.pkg, nil, 0); err == nil {
		t.Fatal("a forced rebuild kept the image")
	}
	inner := e.fake.respond
	e.fake.respond = func(args []string) ([]byte, error) {
		if args[0] == "build" {
			return nil, docker.ErrUnreachable
		}
		return inner(args)
	}
	if _, err := e.host().Build(context.Background(), e.pkg, nil, 7*24*time.Hour); err == nil || err.(*protocol.Error).Code != protocol.DockerUnreachable {
		t.Fatalf("unreachable Docker kept the image: %v", err)
	}
}

// otherPackage is a package whose build key differs from e.pkg's.
func otherPackage(t *testing.T) string {
	pkg := t.TempDir()
	putFile(t, filepath.Join(pkg, ".claude-plugin/plugin.json"), `{"version":"1.2.3"}`)
	for _, rel := range []string{"session/src/sample.py", "utils/src/sample.py", "delegate/src/sample.py"} {
		putFile(t, filepath.Join(pkg, rel), "other")
	}
	return pkg
}

// share answers image ls and rm from e.built, so a prune removes what a build tagged;
// tagged runs once Docker tags each worker image, before its build inspects it.
func (e *buildEnv) share(tagged func()) {
	inner := e.fake.respond
	e.fake.respond = func(args []string) ([]byte, error) {
		switch {
		case args[0] == "image" && args[1] == "ls" && !slices.Contains(args, "--filter"):
			var lines []string
			for tag := range e.built {
				lines = append(lines, tag+" sha256:"+strings.Repeat("a", 64))
			}
			slices.Sort(lines)
			return []byte(strings.Join(lines, "\n")), nil
		case args[0] == "image" && args[1] == "rm":
			delete(e.built, args[2])
			return nil, nil
		}
		out, err := inner(args)
		if args[0] == "build" && strings.HasPrefix(args[slices.Index(args, "--tag")+1], "ensemble-worker:") {
			tagged()
		}
		return out, err
	}
}

func (e *buildEnv) has(result BuildResult) bool {
	_, worker := e.built[result.Image]
	_, user := e.built[strings.Replace(result.Image, "ensemble-worker:", "ensemble-user:", 1)]
	return worker && user
}

func TestConcurrentPackagesKeepEachOthersImages(t *testing.T) {
	ctx := context.Background()
	t.Run("a rebuild pruning while another package's rebuild runs", func(t *testing.T) {
		e := newBuildEnv(t)
		other := otherPackage(t)
		var first BuildResult
		var firstErr error
		ran := false
		e.share(func() {
			if !ran {
				ran = true
				first, firstErr = e.host().Build(ctx, other, nil, -1)
			}
		})
		second, err := e.host().Build(ctx, e.pkg, nil, -1)
		if err != nil || firstErr != nil || !first.Rebuilt || !second.Rebuilt || first.Image == second.Image {
			t.Fatalf("%+v %v %+v %v", first, firstErr, second, err)
		}
		if !e.has(first) || !e.has(second) {
			t.Fatalf("a pair pruned: %v", e.built)
		}
	})
	t.Run("a reused image another package's rebuild then prunes", func(t *testing.T) {
		e := newBuildEnv(t)
		e.share(func() {})
		built, err := e.host().Build(ctx, e.pkg, nil, -1)
		if err != nil {
			t.Fatal(err)
		}
		stale := time.Now().Add(-imageLease - time.Minute)
		if err := os.Chtimes(e.leasePath(strings.TrimPrefix(built.Image, "ensemble-worker:")), stale, stale); err != nil {
			t.Fatal(err)
		}
		if reused, err := e.host().Build(ctx, e.pkg, nil, -1); err != nil || reused.Rebuilt {
			t.Fatalf("%+v %v", reused, err)
		}
		if other, err := e.host().Build(ctx, otherPackage(t), nil, -1); err != nil || !other.Rebuilt {
			t.Fatalf("%+v %v", other, err)
		}
		if !e.has(built) {
			t.Fatalf("the reused pair pruned: %v", e.built)
		}
	})
	t.Run("a pair no build leased within the lease", func(t *testing.T) {
		e := newBuildEnv(t)
		e.share(func() {})
		built, err := e.host().Build(ctx, e.pkg, nil, -1)
		if err != nil {
			t.Fatal(err)
		}
		lease := e.leasePath(strings.TrimPrefix(built.Image, "ensemble-worker:"))
		stale := time.Now().Add(-imageLease - time.Minute)
		if err := os.Chtimes(lease, stale, stale); err != nil {
			t.Fatal(err)
		}
		other, err := e.host().Build(ctx, otherPackage(t), nil, -1)
		if err != nil || e.has(built) || !e.has(other) {
			t.Fatalf("%+v %v %v", other, err, e.built)
		}
		if _, err := os.Stat(lease); !os.IsNotExist(err) {
			t.Fatalf("a removed key keeps its lease: %v", err)
		}
	})
}

func TestBuildHoldsTheImageLock(t *testing.T) {
	ctx := context.Background()
	e := newBuildEnv(t)
	e.images = "ensemble-worker:" + strings.Repeat("1", 64) + " sha256:" + strings.Repeat("c", 64) + "\n"
	built, err := e.host().Build(ctx, e.pkg, nil, -1)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := e.host().Build(ctx, e.pkg, nil, -1); err != nil {
		t.Fatal(err)
	}
	want := [][]string{{"image inspect"}, {"container ls", "image ls", "image rm", "image ls"}, {"image inspect"}}
	if !slices.EqualFunc(e.held, want, slices.Equal) {
		t.Fatalf("%q", e.held)
	}
	info, err := os.Stat(e.leasePath(strings.TrimPrefix(built.Image, "ensemble-worker:")))
	if err != nil || time.Since(info.ModTime()) > time.Minute {
		t.Fatalf("%v %v", info, err)
	}
	t.Run("a build that cannot take it", func(t *testing.T) {
		e := newBuildEnv(t)
		e.lockFail = 1
		_, err := e.host().Build(ctx, e.pkg, nil, -1)
		var pe *protocol.Error
		if !errors.As(err, &pe) || pe.Code != protocol.BuildFailed || pe.Message != "Taking the image lock ~/.ensemble/docker/images.lock failed." || len(e.fake.calls) != 0 {
			t.Fatalf("%v %q", err, e.fake.verbs())
		}
	})
	t.Run("a prune that cannot take it keeps every image", func(t *testing.T) {
		e := newBuildEnv(t)
		e.images, e.lockFail = "ensemble-worker:"+strings.Repeat("1", 64)+" sha256:"+strings.Repeat("c", 64)+"\n", 2
		if got, err := e.host().Build(ctx, e.pkg, nil, -1); err != nil || !got.Rebuilt {
			t.Fatalf("%+v %v", got, err)
		}
		if slices.Contains(e.fake.verbs(), "image rm") || e.progress.String() != "ensemble-broker: kept older images: cannot take the image lock: lock busy\n" {
			t.Fatalf("%q %q", e.fake.verbs(), e.progress.String())
		}
	})
}
