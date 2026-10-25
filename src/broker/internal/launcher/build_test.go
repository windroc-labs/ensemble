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
	// pluginCheck answers the in-image plugin check's run, pluginCheckErr fails it.
	pluginCheck    string
	pluginCheckErr error
	// bases maps base image references to their IDs, all carrying baseLabels; baseCheck answers the base check's
	// run (default agent 1000 1000), baseCheckErr fails it, and tagErr fails docker tag.
	bases                map[string]string
	baseLabels           map[string]string
	baseCheck            string
	baseCheckErr, tagErr error
	failStage            string
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
	e := &buildEnv{home: t.TempDir(), pkg: t.TempDir(), built: map[string]map[string]string{}, probe: "1001\n1002\n", baseCheck: "agent 1000 1000\n"}
	putFile(t, filepath.Join(e.home, ".ensemble/docker/worker/Dockerfile"), "FROM base")
	putFile(t, filepath.Join(e.pkg, ".claude-plugin/plugin.json"), `{"version":"1.2.3"}`)
	putFile(t, filepath.Join(e.pkg, "src/ensemble/session/sample.py"), "session")
	putFile(t, filepath.Join(e.pkg, "src/ensemble/utils/sample.py"), "utils")
	putFile(t, filepath.Join(e.pkg, "src/ensemble/delegate/sample.py"), "delegate")
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
		case "tag":
			return nil, e.tagErr
		case "run":
			if slices.Contains(args, "-s") {
				return []byte(e.baseCheck), e.baseCheckErr
			}
			if slices.Contains(args, "node") {
				return []byte(e.pluginCheck), e.pluginCheckErr
			}
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
			if id, ok := e.bases[args[len(args)-1]]; ok {
				return inspectJSON(id, e.baseLabels, time.Now()), nil
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
	first, err := e.host().Build(context.Background(), e.pkg, BuildSpec{}, -1)
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
	second, err := e.host().Build(context.Background(), e.pkg, BuildSpec{}, -1)
	if err != nil || second.Rebuilt || second.Image != first.Image || len(e.fake.calls) != calls+1 {
		t.Fatalf("%+v %v %q", second, err, e.fake.verbs())
	}
	putFile(t, filepath.Join(e.pkg, "src/ensemble/session/sample.py"), "changed")
	third, err := e.host().Build(context.Background(), e.pkg, BuildSpec{}, -1)
	if err != nil || !third.Rebuilt || third.Image == first.Image {
		t.Fatalf("%+v %v", third, err)
	}
	putFile(t, filepath.Join(e.pkg, "src/ensemble/delegate/sample.py"), "changed")
	if team, err := e.host().Build(context.Background(), e.pkg, BuildSpec{}, -1); err != nil || !team.Rebuilt || team.Image == third.Image {
		t.Fatalf("%+v %v", team, err)
	}
	putFile(t, filepath.Join(e.home, ".ensemble/docker/worker/Dockerfile"), "FROM other")
	fourth, err := e.host().Build(context.Background(), e.pkg, BuildSpec{}, -1)
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
		"missing session":    func(e *buildEnv) { os.RemoveAll(filepath.Join(e.pkg, "src/ensemble/session")) },
		"missing delegate":   func(e *buildEnv) { os.RemoveAll(filepath.Join(e.pkg, "src/ensemble/delegate")) },
		"root agent":         func(e *buildEnv) { e.probe = "0\n0\n" },
		"probe garbage":      func(e *buildEnv) { e.probe = "agent\n" },
		"stage one":          func(e *buildEnv) { e.failStage = "ensemble-user" },
		"stage two":          func(e *buildEnv) { e.failStage = "ensemble-worker" },
	} {
		t.Run(name, func(t *testing.T) {
			e := newBuildEnv(t)
			mutate(e)
			_, err := e.host().Build(context.Background(), e.pkg, BuildSpec{}, -1)
			if code(err) != protocol.BuildFailed {
				t.Fatal(err)
			}
			if name == "missing definition" && !strings.Contains(err.(*protocol.Error).Message, "src/broker/install.sh") {
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
		if _, err := e.host().Build(context.Background(), e.pkg, BuildSpec{}, -1); code(err) != protocol.BuildFailed {
			t.Fatal(err)
		}
	})
	t.Run("unreachable", func(t *testing.T) {
		e := newBuildEnv(t)
		e.fake.respond = func([]string) ([]byte, error) { return nil, docker.ErrUnreachable }
		if _, err := e.host().Build(context.Background(), e.pkg, BuildSpec{}, -1); code(err) != protocol.DockerUnreachable {
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
		`'exec /opt/ensemble/venv/bin/python -I /opt/ensemble/src/ensemble/session/hosts/docker/relay.py "$@"' > /opt/ensemble/bin/relay`,
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
		"COPY src/ensemble/session /opt/ensemble/src/ensemble/session\n",
		"COPY src/ensemble/utils /opt/ensemble/src/ensemble/utils\n",
		"COPY src/ensemble/delegate /opt/ensemble/src/ensemble/delegate\n",
		"python3 -m venv --system-site-packages /opt/ensemble/venv",
		`printf '%s\n' /opt/ensemble/src > "$(/opt/ensemble/venv/bin/python -c 'import sysconfig; print(sysconfig.get_path("purelib"))')/ensemble.pth"`,
		`printf '%s\n' '#!/bin/sh' 'exec /opt/ensemble/venv/bin/python "$@"' > /opt/ensemble/bin/python3`,
		"ln -s /opt/ensemble/src/ensemble/delegate/team /opt/ensemble/team",
	} {
		if !strings.Contains(string(data), want) {
			t.Errorf("Dockerfile lacks %q", want)
		}
	}
}

func TestBuildContextOutsideTMPDIR(t *testing.T) {
	e := newBuildEnv(t)
	t.Setenv("TMPDIR", t.TempDir())
	if _, err := e.host().Build(context.Background(), e.pkg, BuildSpec{}, -1); err != nil {
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
		if _, err := e.host().Build(context.Background(), e.pkg, BuildSpec{}, -1); err != nil {
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
		if again, err := e.host().Build(context.Background(), e.pkg, BuildSpec{}, -1); err != nil || again.Rebuilt || len(e.fake.calls) != calls+1 {
			t.Fatalf("a reused image pruned: %v %q", err, e.fake.verbs()[calls:])
		}
	})
	t.Run("a failed removal keeps the image", func(t *testing.T) {
		e := newBuildEnv(t)
		e.images, e.rmErr = listing, &docker.CommandError{Exit: 1, Stderr: "Error response from daemon: conflict: image is in use"}
		if _, err := e.host().Build(context.Background(), e.pkg, BuildSpec{}, -1); err != nil {
			t.Fatal(err)
		}
		if !strings.Contains(e.progress.String(), "ensemble-broker: kept ensemble-worker:"+old+": Error response from daemon: conflict: image is in use\n") {
			t.Fatalf("%q", e.progress.String())
		}
	})
	t.Run("unknown containers keep every image", func(t *testing.T) {
		e := newBuildEnv(t)
		e.images, e.listErr = listing, &docker.CommandError{Exit: 1, Stderr: "boom"}
		if _, err := e.host().Build(context.Background(), e.pkg, BuildSpec{}, -1); err != nil {
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
		if _, err := e.host().Build(context.Background(), e.pkg, BuildSpec{}, -1); err != nil {
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
	if _, err := e.host().Build(context.Background(), e.pkg, BuildSpec{}, -1); err != nil {
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
			got, err := e.host().Build(context.Background(), e.pkg, BuildSpec{}, tc.maxAge)
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
	if _, err := e.host().Build(context.Background(), e.pkg, BuildSpec{}, -1); err != nil {
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
	first, err := e.host().Build(context.Background(), e.pkg, BuildSpec{}, -1)
	if err != nil {
		t.Fatal(err)
	}
	e.age, e.failStage = 8*24*time.Hour, "ensemble-user"
	e.progress.Reset()
	got, err := e.host().Build(context.Background(), e.pkg, BuildSpec{}, 7*24*time.Hour)
	if err != nil || got != (BuildResult{Image: first.Image, Digest: first.Digest}) {
		t.Fatalf("%+v %v", got, err)
	}
	if want := "ensemble-broker: kept " + first.Image + ": refreshing it failed: Building the worker image definition failed. (RUN false)\n"; e.progress.String() != want {
		t.Fatalf("%q", e.progress.String())
	}
	if _, err := e.host().Build(context.Background(), e.pkg, BuildSpec{}, 0); err == nil {
		t.Fatal("a forced rebuild kept the image")
	}
	inner := e.fake.respond
	e.fake.respond = func(args []string) ([]byte, error) {
		if args[0] == "build" {
			return nil, docker.ErrUnreachable
		}
		return inner(args)
	}
	if _, err := e.host().Build(context.Background(), e.pkg, BuildSpec{}, 7*24*time.Hour); err == nil || err.(*protocol.Error).Code != protocol.DockerUnreachable {
		t.Fatalf("unreachable Docker kept the image: %v", err)
	}
}

// otherPackage is a package whose build key differs from e.pkg's.
func otherPackage(t *testing.T) string {
	pkg := t.TempDir()
	putFile(t, filepath.Join(pkg, ".claude-plugin/plugin.json"), `{"version":"1.2.3"}`)
	for _, rel := range []string{"src/ensemble/session/sample.py", "src/ensemble/utils/sample.py", "src/ensemble/delegate/sample.py"} {
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
				first, firstErr = e.host().Build(ctx, other, BuildSpec{}, -1)
			}
		})
		second, err := e.host().Build(ctx, e.pkg, BuildSpec{}, -1)
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
		built, err := e.host().Build(ctx, e.pkg, BuildSpec{}, -1)
		if err != nil {
			t.Fatal(err)
		}
		stale := time.Now().Add(-imageLease - time.Minute)
		if err := os.Chtimes(e.leasePath(strings.TrimPrefix(built.Image, "ensemble-worker:")), stale, stale); err != nil {
			t.Fatal(err)
		}
		if reused, err := e.host().Build(ctx, e.pkg, BuildSpec{}, -1); err != nil || reused.Rebuilt {
			t.Fatalf("%+v %v", reused, err)
		}
		if other, err := e.host().Build(ctx, otherPackage(t), BuildSpec{}, -1); err != nil || !other.Rebuilt {
			t.Fatalf("%+v %v", other, err)
		}
		if !e.has(built) {
			t.Fatalf("the reused pair pruned: %v", e.built)
		}
	})
	t.Run("a pair no build leased within the lease", func(t *testing.T) {
		e := newBuildEnv(t)
		e.share(func() {})
		built, err := e.host().Build(ctx, e.pkg, BuildSpec{}, -1)
		if err != nil {
			t.Fatal(err)
		}
		lease := e.leasePath(strings.TrimPrefix(built.Image, "ensemble-worker:"))
		stale := time.Now().Add(-imageLease - time.Minute)
		if err := os.Chtimes(lease, stale, stale); err != nil {
			t.Fatal(err)
		}
		other, err := e.host().Build(ctx, otherPackage(t), BuildSpec{}, -1)
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
	built, err := e.host().Build(ctx, e.pkg, BuildSpec{}, -1)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := e.host().Build(ctx, e.pkg, BuildSpec{}, -1); err != nil {
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
		_, err := e.host().Build(ctx, e.pkg, BuildSpec{}, -1)
		var pe *protocol.Error
		if !errors.As(err, &pe) || pe.Code != protocol.BuildFailed || pe.Message != "Taking the image lock ~/.ensemble/docker/images.lock failed." || len(e.fake.calls) != 0 {
			t.Fatalf("%v %q", err, e.fake.verbs())
		}
	})
	t.Run("a prune that cannot take it keeps every image", func(t *testing.T) {
		e := newBuildEnv(t)
		e.images, e.lockFail = "ensemble-worker:"+strings.Repeat("1", 64)+" sha256:"+strings.Repeat("c", 64)+"\n", 2
		if got, err := e.host().Build(ctx, e.pkg, BuildSpec{}, -1); err != nil || !got.Rebuilt {
			t.Fatalf("%+v %v", got, err)
		}
		if slices.Contains(e.fake.verbs(), "image rm") || e.progress.String() != "ensemble-broker: kept older images: cannot take the image lock: lock busy\n" {
			t.Fatalf("%q %q", e.fake.verbs(), e.progress.String())
		}
	})
}

func TestImagePluginCheck(t *testing.T) {
	script, err := checks.ReadFile("checks/plugin-check.js")
	if err != nil {
		t.Fatal(err)
	}
	spec := BuildSpec{ImagePlugins: []string{"beta", "alpha"}}
	t.Run("passes", func(t *testing.T) {
		e := newBuildEnv(t)
		if _, err := e.host().Build(context.Background(), e.pkg, spec, -1); err != nil {
			t.Fatal(err)
		}
		i := slices.IndexFunc(e.fake.calls, func(c []string) bool { return slices.Contains(c, "node") })
		if i < 0 {
			t.Fatalf("no plugin check: %q", e.fake.verbs())
		}
		userImage := e.fake.calls[1][slices.Index(e.fake.calls[1], "--tag")+1]
		want := []string{"run", "--rm", "-i", "--pull", "never", "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
			"--user", "agent", "--env", "PATH=/home/agent/.npm-global/bin:/usr/local/bin:/usr/bin:/bin", "--entrypoint", "node", userImage, "-", "/opt/windroc/plugins", "alpha", "beta"}
		if !slices.Equal(e.fake.calls[i], want) || string(e.fake.stdin[i]) != string(script) {
			t.Fatalf("%q", e.fake.calls[i])
		}
		if slices.IndexFunc(e.fake.calls, func(c []string) bool { return slices.Contains(c, "--build-arg") }) < i {
			t.Fatal("ensemble layer built before the plugin check")
		}
	})
	for name, tc := range map[string]struct {
		out, message string
		err          error
	}{
		"missing":    {out: "alpha missing\n", message: "In-image plugin alpha: no /opt/windroc/plugins/alpha/.claude-plugin/plugin.json."},
		"unreadable": {out: "alpha unreadable\n", message: "In-image plugin alpha: agent cannot read all of /opt/windroc/plugins/alpha/."},
		"several": {out: "alpha misnamed\nbeta missing\n", message: `In-image plugin alpha: /opt/windroc/plugins/alpha/.claude-plugin/plugin.json must name it "alpha".` +
			" In-image plugin beta: no /opt/windroc/plugins/beta/.claude-plugin/plugin.json."},
		"unrequested":   {out: "gamma missing\n", message: "Checking in-image plugins gave unexpected output."},
		"unknown":       {out: "alpha broken\n", message: "Checking in-image plugins gave unexpected output."},
		"too many":      {out: "alpha missing\nbeta missing\nalpha missing\n", message: "Checking in-image plugins gave unexpected output."},
		"docker failed": {err: &docker.CommandError{Exit: 125, Stderr: "unable to find user agent"}, message: "Checking in-image plugins failed."},
	} {
		t.Run(name, func(t *testing.T) {
			e := newBuildEnv(t)
			e.pluginCheck, e.pluginCheckErr = tc.out, tc.err
			_, err := e.host().Build(context.Background(), e.pkg, spec, -1)
			var pe *protocol.Error
			if !errors.As(err, &pe) || pe.Code != protocol.BuildFailed || pe.Message != tc.message {
				t.Fatalf("%v", err)
			}
			if tc.err != nil && !strings.Contains(pe.Cause, "unable to find user agent") {
				t.Fatalf("cause %q", pe.Cause)
			}
			if packageBuild(e.fake.calls) != nil {
				t.Fatal("ensemble layer built after a failed check")
			}
		})
	}
}

const baseRef = "dummy-env:test"

var baseID = "sha256:" + strings.Repeat("c", 64)

// newBaseEnv is newBuildEnv with baseRef local and no worker image definition.
func newBaseEnv(t *testing.T) *buildEnv {
	e := newBuildEnv(t)
	e.bases = map[string]string{baseRef: baseID}
	if err := os.RemoveAll(filepath.Join(e.home, ".ensemble/docker/worker")); err != nil {
		t.Fatal(err)
	}
	return e
}

// buildFailure is err's message and cause, failing unless it is build_failed.
func buildFailure(t *testing.T, err error) (string, string) {
	t.Helper()
	var pe *protocol.Error
	if !errors.As(err, &pe) || pe.Code != protocol.BuildFailed {
		t.Fatalf("%v", err)
	}
	return pe.Message, pe.Cause
}

// noPull fails on a pull, a call that may pull, or a stage-1 build: a base image is neither pulled nor rebuilt.
func noPull(t *testing.T, calls [][]string) {
	t.Helper()
	for _, c := range calls {
		if c[0] == "pull" || (c[0] == "build" && strings.HasPrefix(c[slices.Index(c, "--tag")+1], "ensemble-user:")) {
			t.Fatalf("pulled or rebuilt: %q", c)
		}
		if i := slices.Index(c, "--pull"); i >= 0 && c[i+1] != "never" {
			t.Fatalf("may pull: %q", c)
		}
	}
}

func TestBaseImageAbsent(t *testing.T) {
	e := newBaseEnv(t)
	_, err := e.host().Build(context.Background(), e.pkg, BuildSpec{BaseImage: "nope:none"}, -1)
	if message, _ := buildFailure(t, err); message != "Base image nope:none is not a local image; build or load it first (ensemble-broker never pulls it)." {
		t.Fatal(message)
	}
	if got := e.fake.verbs(); !slices.Equal(got, []string{"image inspect"}) || e.fake.calls[0][2] != "nope:none" {
		t.Fatalf("%q", e.fake.calls)
	}
	e = newBaseEnv(t)
	_, err = e.host().Build(context.Background(), e.pkg, BuildSpec{BaseImage: "-x"}, -1)
	if message, _ := buildFailure(t, err); !strings.Contains(message, "-x") || len(e.fake.calls) != 0 {
		t.Fatalf("%s %q", message, e.fake.calls)
	}
}

func TestBaseImageReservedLabels(t *testing.T) {
	many := map[string]string{}
	for _, k := range []string{"windroc.a", "windroc.b", "windroc.c", "ensemble.d", "ensemble.e", "windroc.f", "windroc.g", "other"} {
		many[k] = "x"
	}
	for name, tc := range map[string]struct {
		labels  map[string]string
		message string
	}{
		"lock":    {map[string]string{"windroc.lock": "x", "org.opencontainers.image.title": "t"}, "Base image dummy-env:test carries reserved labels windroc.lock; remove every windroc. and ensemble. label."},
		"several": {map[string]string{"windroc.b": "", "ensemble.a": "", "maintainer": "m"}, "Base image dummy-env:test carries reserved labels ensemble.a, windroc.b; remove every windroc. and ensemble. label."},
		"many":    {many, "Base image dummy-env:test carries reserved labels ensemble.d, ensemble.e, windroc.a, windroc.b, windroc.c and 2 more; remove every windroc. and ensemble. label."},
		"invalid": {map[string]string{"windroc.\x1b[31m": "", "ensemble." + strings.Repeat("a", 130): ""}, "Base image dummy-env:test carries reserved labels (invalid label), (invalid label); remove every windroc. and ensemble. label."},
	} {
		t.Run(name, func(t *testing.T) {
			e := newBaseEnv(t)
			e.baseLabels = tc.labels
			_, err := e.host().Build(context.Background(), e.pkg, BuildSpec{BaseImage: baseRef}, -1)
			if message, _ := buildFailure(t, err); message != tc.message {
				t.Fatal(message)
			}
			if got := e.fake.verbs(); !slices.Equal(got, []string{"image inspect"}) {
				t.Fatalf("%q", got)
			}
		})
	}
}

func TestBaseImageRequirements(t *testing.T) {
	script, err := checks.ReadFile("checks/base-check")
	if err != nil {
		t.Fatal(err)
	}
	unexpected := "Checking base image dummy-env:test gave unexpected output."
	for name, tc := range map[string]struct {
		out, message string
		err          error
	}{
		"tools":         {out: "missing tmux\nmissing claude\nagent 1000 1000\n", message: "Base image dummy-env:test lacks tmux, claude (Claude Code); see docs/docker-host.md#base-images."},
		"agent":         {out: "missing agent\n", message: "Base image dummy-env:test lacks a non-root user agent; see docs/docker-host.md#base-images."},
		"any order":     {out: "missing agent\nmissing git\nmissing apt-get\n", message: "Base image dummy-env:test lacks apt-get, git, a non-root user agent; see docs/docker-host.md#base-images."},
		"all":           {out: "missing apt-get\nmissing dpkg\nmissing node\nmissing npm\nmissing tmux\nmissing git\nmissing claude\nmissing agent\n", message: "Base image dummy-env:test lacks apt-get, dpkg, node, npm, tmux, git, claude (Claude Code), a non-root user agent; see docs/docker-host.md#base-images."},
		"root ids":      {out: "agent 0 0\n", message: unexpected},
		"short ids":     {out: "agent 1000\n", message: unexpected},
		"nothing":       {out: "", message: unexpected},
		"unknown item":  {out: "missing python3\nagent 1 1\n", message: unexpected},
		"agent twice":   {out: "missing agent\nagent 1 1\n", message: unexpected},
		"repeated":      {out: "missing git\nmissing git\nagent 1 1\n", message: unexpected},
		"too long":      {out: strings.Repeat("missing git\n", 9) + "agent 1 1\n", message: unexpected},
		"docker failed": {err: &docker.CommandError{Exit: 127, Stderr: "exec: \"/bin/sh\": stat /bin/sh: no such file"}, message: "Checking base image dummy-env:test failed."},
	} {
		t.Run(name, func(t *testing.T) {
			e := newBaseEnv(t)
			e.baseCheck, e.baseCheckErr = tc.out, tc.err
			_, err := e.host().Build(context.Background(), e.pkg, BuildSpec{BaseImage: baseRef}, -1)
			message, cause := buildFailure(t, err)
			if message != tc.message {
				t.Fatal(message)
			}
			if tc.err != nil && !strings.Contains(cause, "/bin/sh") {
				t.Fatalf("cause %q", cause)
			}
			i := slices.IndexFunc(e.fake.calls, func(c []string) bool { return slices.Contains(c, "-s") })
			want := []string{"run", "--rm", "-i", "--pull", "never", "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
				"--user", "65534:65534", "--entrypoint", "/bin/sh", baseID, "-s", "/home/agent/.npm-global/bin:/usr/local/bin:/usr/bin:/bin"}
			if i < 0 || !slices.Equal(e.fake.calls[i], want) || string(e.fake.stdin[i]) != string(script) {
				t.Fatalf("%q", e.fake.calls)
			}
			for _, c := range e.fake.calls {
				if c[0] == "tag" || c[0] == "build" {
					t.Fatalf("%q after a failed check", c)
				}
			}
		})
	}
}

func TestBaseImageStages(t *testing.T) {
	e := newBaseEnv(t)
	result, err := e.host().Build(context.Background(), e.pkg, BuildSpec{BaseImage: baseRef, ImagePlugins: []string{"dummy-plugin"}}, -1)
	if err != nil || !result.Rebuilt || result.Image != "ensemble-worker:"+e.key {
		t.Fatalf("%+v %v", result, err)
	}
	if got := e.fake.verbs(); !slices.Equal(got, []string{"image inspect", "image inspect", "run --rm", "tag " + baseID, "run --rm", "build --progress=plain", "image inspect", "container ls", "image ls", "image ls"}) {
		t.Fatalf("%q", got)
	}
	userImage := "ensemble-user:" + e.key
	if tag := e.fake.calls[3]; !slices.Equal(tag, []string{"tag", baseID, userImage}) {
		t.Fatalf("%q", tag)
	}
	if check := e.fake.calls[4]; !slices.Contains(check, "node") || !slices.Contains(check, userImage) {
		t.Fatalf("%q", check)
	}
	layer := e.fake.calls[5]
	for _, arg := range []string{"USER_IMAGE=" + userImage, "AGENT_UID=1000", "AGENT_GID=1000", "ENSEMBLE_PLUGIN_DIRS=/opt/windroc/plugins/dummy-plugin", "--no-cache"} {
		if !slices.Contains(layer, arg) {
			t.Fatalf("%s absent: %q", arg, layer)
		}
	}
	noPull(t, e.fake.calls)
	t.Run("tag fails", func(t *testing.T) {
		e := newBaseEnv(t)
		e.tagErr = &docker.CommandError{Exit: 1, Stderr: "Error response from daemon: No such image: " + baseID}
		_, err := e.host().Build(context.Background(), e.pkg, BuildSpec{BaseImage: baseRef}, -1)
		if message, cause := buildFailure(t, err); message != "Tagging base image dummy-env:test failed." || !strings.Contains(cause, "No such image") {
			t.Fatalf("%s %s", message, cause)
		}
	})
}

func TestBaseImageKeyFollowsItsID(t *testing.T) {
	e := newBaseEnv(t)
	e.bases["alias:1"] = baseID
	build := func(ref string) BuildResult {
		t.Helper()
		result, err := e.host().Build(context.Background(), e.pkg, BuildSpec{BaseImage: ref}, -1)
		if err != nil {
			t.Fatal(err)
		}
		return result
	}
	first := build(baseRef)
	e.bases[baseRef] = "sha256:" + strings.Repeat("d", 64)
	rebuilt := build(baseRef)
	if !rebuilt.Rebuilt || rebuilt.Image == first.Image {
		t.Fatalf("rebuilt base kept %+v", rebuilt)
	}
	if alias := build("alias:1"); alias.Image != first.Image || alias.Rebuilt {
		t.Fatalf("same ID, other tag: %+v vs %+v", alias, first)
	}
	putFile(t, filepath.Join(e.home, ".ensemble/docker/worker/Dockerfile"), "FROM base")
	if defaultPath, err := e.host().Build(context.Background(), e.pkg, BuildSpec{}, -1); err != nil || defaultPath.Image == first.Image {
		t.Fatalf("%+v %v", defaultPath, err)
	}
}
