package launcher

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"syscall"
	"testing"
	"time"

	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/fixtures"
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
		out, err := f.respond(args)
		if slices.Contains(args, credentialBatch) && out == nil && err == nil {
			var commands []startCommand
			if err := json.Unmarshal(stdin, &commands); err != nil {
				return nil, err
			}
			for i, command := range commands {
				if _, err := f.respond(append([]string{"exec", "--interactive", "--user", "agent", container}, command.Argv...)); err != nil {
					return json.Marshal(map[string]int{"completed": i})
				}
			}
			return json.Marshal(map[string]int{"completed": len(commands)})
		}
		return out, err
	}
	return nil, nil
}

// removed is each image rm call's reference; it clears the record, so the next one sees only later removals.
func (f *fakeDocker) removed() []string {
	var refs []string
	for _, c := range f.calls {
		if strings.Join(c[:2], " ") == "image rm" {
			refs = append(refs, c[2])
		}
	}
	f.calls, f.stdin, f.ctxErrs = nil, nil, nil
	return refs
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
	data, _ := json.Marshal([]map[string]any{{"Id": id, "Created": created, "Config": map[string]any{"Labels": labels}}})
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

func TestPrepareUmask(t *testing.T) {
	e := newBuildEnv(t)
	h := e.host()
	e.bases = map[string]string{"local:base": digest}
	private := filepath.Join(e.pkg, "src/ensemble/session/private")
	putFile(t, filepath.Join(private, "read.py"), "readable source")
	putFile(t, filepath.Join(private, "run"), "executable source")
	for path, mode := range map[string]os.FileMode{private: 0o700, filepath.Join(private, "read.py"): 0o600, filepath.Join(private, "run"): 0o700} {
		if err := os.Chmod(path, mode); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.Symlink("read.py", filepath.Join(private, "link")); err != nil {
		t.Fatal(err)
	}
	previous := syscall.Umask(0o022)
	defer syscall.Umask(previous)
	for _, spec := range []BuildSpec{{}, {BaseImage: "local:base", ImagePlugins: []string{"dummy"}}} {
		var key string
		for _, mask := range []int{0o022, 0o077} {
			syscall.Umask(mask)
			tmp, err := docker.TempDir(e.home, "ensemble-build-")
			if err != nil {
				t.Fatal(err)
			}
			p, err := h.prepare(context.Background(), e.pkg, spec, tmp)
			if err != nil {
				t.Fatal(err)
			}
			if key != "" && key != p.key {
				t.Errorf("umask %03o changed image key: %s != %s", mask, p.key, key)
			}
			key = p.key
			err = filepath.WalkDir(filepath.Join(tmp, "package"), func(path string, entry os.DirEntry, err error) error {
				if err != nil {
					return err
				}
				info, err := entry.Info()
				if err != nil || info.Mode()&os.ModeSymlink != 0 {
					return err
				}
				want := os.FileMode(0o644)
				if info.IsDir() || entry.Name() == "run" {
					want = 0o755
				}
				if info.Mode().Perm() != want {
					t.Errorf("umask %03o: %s mode %o, want %o", mask, path, info.Mode().Perm(), want)
				}
				return nil
			})
			if err != nil {
				t.Fatal(err)
			}
			if actual := syscall.Umask(mask); actual != mask {
				t.Errorf("prepare changed caller umask to %03o", actual)
			}
			for _, rel := range []string{"base-image", "image-plugins"} {
				if spec.BaseImage != "" {
					if info, err := os.Stat(filepath.Join(tmp, rel)); err != nil || info.Mode().Perm() != 0o644 {
						t.Errorf("%s must have stable 0644 mode: %v %v", rel, info, err)
					}
				}
			}
			if info, err := os.Stat(tmp); err != nil || info.Mode().Perm() != 0o700 {
				t.Fatalf("staging root must stay private: %v %v", info, err)
			}
		}
	}
	if info, err := os.Stat(private); err != nil || info.Mode().Perm() != 0o700 {
		t.Fatalf("source permissions changed: %v %v", info, err)
	}
}

func TestSnapshotSymlinkKeyUnderUmask(t *testing.T) {
	src, dst := t.TempDir(), t.TempDir()
	putFile(t, filepath.Join(src, "file"), "content")
	if err := os.Mkdir(filepath.Join(src, "dir"), 0o755); err != nil {
		t.Fatal(err)
	}
	links := map[string]string{"file-link": "file", "dir-link": "dir", "dangling": "missing"}
	for name, target := range links {
		if err := os.Symlink(target, filepath.Join(src, name)); err != nil {
			t.Fatal(err)
		}
	}
	previous := syscall.Umask(0o022)
	defer syscall.Umask(previous)
	var key string
	for _, mask := range []int{0o022, 0o077} {
		syscall.Umask(mask)
		if err := snapshotTree(src, dst); err != nil {
			t.Fatal(err)
		}
		got, err := treeDigest(dst)
		if err != nil {
			t.Fatal(err)
		}
		if key != "" && got != key {
			t.Errorf("umask %03o changed snapshot key: %s != %s", mask, got, key)
		}
		key = got
		for name, target := range links {
			if got, err := os.Readlink(filepath.Join(dst, name)); err != nil || got != target {
				t.Fatalf("%s target = %q, %v; want %q", name, got, err, target)
			}
		}
	}
	if err := os.Remove(filepath.Join(src, "dangling")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink("other-missing", filepath.Join(src, "dangling")); err != nil {
		t.Fatal(err)
	}
	if err := snapshotTree(src, dst); err != nil {
		t.Fatal(err)
	}
	if got, err := treeDigest(dst); err != nil || got == key {
		t.Fatalf("changed symlink target must change key: %s, %v", got, err)
	}
}

func TestSnapshotPreservesModesUnderUmask(t *testing.T) {
	src, dst := t.TempDir(), filepath.Join(t.TempDir(), "snapshot")
	putFile(t, filepath.Join(src, "Dockerfile"), "FROM base")
	putFile(t, filepath.Join(src, "private/config"), "private definition input")
	for path, mode := range map[string]os.FileMode{src: 0o755, filepath.Join(src, "private"): 0o750, filepath.Join(src, "private/config"): 0o640} {
		if err := os.Chmod(path, mode); err != nil {
			t.Fatal(err)
		}
	}
	previous := syscall.Umask(0o077)
	defer syscall.Umask(previous)
	if err := snapshotTree(src, dst); err != nil {
		t.Fatal(err)
	}
	for _, rel := range []string{".", "Dockerfile", "private", "private/config"} {
		before, _ := os.Stat(filepath.Join(src, rel))
		after, err := os.Stat(filepath.Join(dst, rel))
		if err != nil || before.Mode().Perm() != after.Mode().Perm() {
			t.Errorf("%s mode not preserved: %v %v", rel, after, err)
		}
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
	// bases maps base image references to their IDs, all carrying baseLabels; baseAgent and baseTools answer the base
	// check's agent run (default agent 1000 1000) and tools run, baseCheckErr fails the agent run, and tagErr docker tag.
	bases                map[string]string
	baseLabels           map[string]string
	baseAgent, baseTools string
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
	releases        *fakeReleases
	// ids, when set, gives each image build its own ID, the tag's inspected ID; nil: every image is sha256:a….
	ids map[string]string
}

// fakeReleases answers Latest from latest, failing with err; it records each package asked.
type fakeReleases struct {
	latest map[string]string
	err    error
	asked  []string
}

func (f *fakeReleases) Latest(_ context.Context, pkg string) (string, error) {
	f.asked = append(f.asked, pkg)
	return f.latest[pkg], f.err
}

func newReleases() *fakeReleases {
	return &fakeReleases{latest: map[string]string{"@anthropic-ai/claude-code": "2.1.7", "@openai/codex": "0.170.0", "opencode-ai": "1.20.1"}}
}

// clientArgs maps the ensemble layer's client build args to the labels its Dockerfile sets.
var clientArgs = map[string]string{"CLAUDE_VERSION": "ensemble.claude-version", "CODEX_VERSION": "ensemble.codex-version", "OPENCODE_VERSION": "ensemble.opencode-version"}

func newBuildEnv(t *testing.T) *buildEnv {
	e := &buildEnv{home: t.TempDir(), pkg: t.TempDir(), built: map[string]map[string]string{}, probe: "1001\n1002\n", baseAgent: "agent 1000 1000\n", releases: newReleases()}
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
					if label, ok := clientArgs[k]; ok {
						labels[label] = v
					}
				}
			}
			e.built[tag] = labels
			if e.ids != nil {
				e.ids[tag] = fmt.Sprintf("sha256:%064x", len(e.fake.calls))
			}
			if k, ok := strings.CutPrefix(tag, "ensemble-worker:"); ok {
				e.key = k
			}
			return nil, nil
		case "tag":
			return nil, e.tagErr
		case "run":
			if i := slices.Index(args, "-s"); i >= 0 {
				if args[i+1] == "agent" {
					return []byte(e.baseAgent), e.baseCheckErr
				}
				return []byte(e.baseTools), nil
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
			id, ok := e.ids[args[len(args)-1]]
			if !ok {
				id = "sha256:" + strings.Repeat("a", 64)
			}
			return inspectJSON(id, labels, time.Now().Add(-e.age)), nil
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
	return Host{Home: e.home, Docker: e.fake, Progress: &e.progress, Lock: e.lock, Releases: e.releases}
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
	if got := e.fake.verbs(); !slices.Equal(got, []string{"image inspect", "build --progress=plain", "run --rm", "image inspect", "build --progress=plain", "image inspect", "container ls", "image ls", "image ls"}) {
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
	stage2 := e.fake.calls[4]
	want := []string{"build", "--progress=plain", "--no-cache", "--build-arg", "USER_IMAGE=ensemble-user:" + e.key, "--build-arg", "ENSEMBLE_VERSION=1.2.3", "--build-arg", "AGENT_UID=1001", "--build-arg", "AGENT_GID=1002", "--build-arg", "ENSEMBLE_PLUGIN_DIRS=",
		"--build-arg", "CLAUDE_VERSION=2.1.7", "--build-arg", "CODEX_VERSION=0.170.0", "--build-arg", "OPENCODE_VERSION=1.20.1", "--label", "ensemble.key=" + e.key, "--tag", "ensemble-worker:" + e.key}
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
			if code(t, err) != protocol.BuildFailed {
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
		if _, err := e.host().Build(context.Background(), e.pkg, BuildSpec{}, -1); code(t, err) != protocol.BuildFailed {
			t.Fatal(err)
		}
	})
	t.Run("client label mismatch", func(t *testing.T) {
		e := newBuildEnv(t)
		inner := e.fake.respond
		e.fake.respond = func(args []string) ([]byte, error) {
			out, err := inner(args)
			if args[0] == "build" && slices.Contains(args, "--build-arg") {
				e.built["ensemble-worker:"+e.key]["ensemble.codex-version"] = "0.169.0"
			}
			return out, err
		}
		_, err := e.host().Build(context.Background(), e.pkg, BuildSpec{}, -1)
		if message, _ := buildFailure(t, err); message != "Built image labels do not match the package." {
			t.Fatal(message)
		}
	})
	t.Run("unreachable", func(t *testing.T) {
		e := newBuildEnv(t)
		e.fake.respond = func([]string) ([]byte, error) { return nil, docker.ErrUnreachable }
		if _, err := e.host().Build(context.Background(), e.pkg, BuildSpec{}, -1); code(t, err) != protocol.DockerUnreachable {
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
		"chmod 755 /opt/ensemble/bin/ensemble-session /opt/ensemble/bin/python3 ",
		"install -d -o agent -g agent -m 0700 /run/ensemble\n",
		"COPY bootstrap.py credential-batch.py codex-probe.py opencode-install.py plugin-install.py hook-stub with-secrets workspace-check gh git-credential-ensemble /opt/ensemble/lib/",
		`python3 -I /opt/ensemble/lib/opencode-install.py "$(dpkg --print-architecture)" /opt/ensemble/clients`,
		"ln -s /opt/ensemble/clients/bin/opencode /opt/ensemble/bin/opencode",
		"python3 -I /opt/ensemble/lib/codex-probe.py /opt/ensemble/clients/bin/codex ",
		"ln -s /opt/ensemble/clients/bin/codex /opt/ensemble/bin/codex",
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
	for _, gone := range []string{"/opt/ensemble/runtime", "COPY codex ", "codex-update", "relay"} {
		if strings.Contains(string(data), gone) {
			t.Errorf("Dockerfile still has %q", gone)
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
	for _, i := range []int{1, 4} {
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
	t.Run("keeps the current pair and images containers use", func(t *testing.T) {
		e := newBuildEnv(t)
		e.images, e.containers, e.used = listing, "c1\nc2\n", id("e")+"\n"+id("0")+"\n"
		if _, err := e.host().Build(context.Background(), e.pkg, BuildSpec{}, -1); err != nil {
			t.Fatal(err)
		}
		inspect := e.fake.calls[slices.Index(e.fake.verbs(), "container inspect")]
		if !slices.Equal(inspect, []string{"container", "inspect", "--format", "{{.Image}}", "c1", "c2"}) {
			t.Fatalf("%q", inspect)
		}
		want := []string{"ensemble-worker:" + old, "ensemble-user:" + old, "ensemble-user:" + kept}
		if got := e.fake.removed(); !slices.Equal(got, want) {
			t.Fatalf("%q", got)
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
		if got := e.fake.removed(); got != nil ||
			e.progress.String() != "ensemble-broker: kept older images: cannot list containers: boom\n" {
			t.Fatalf("%q %q", got, e.progress.String())
		}
	})
	t.Run("keeps the images of keys a build leased within the lease", func(t *testing.T) {
		e := newBuildEnv(t)
		e.images = listing
		stale := time.Now().Add(-variantLease - time.Minute)
		for _, key := range []string{old, kept} {
			putFile(t, e.leasePath(key), "")
			putFile(t, e.leasePath(key)+".json", "{}")
			putFile(t, e.leasePath(key)+".replaced", "[]")
		}
		for _, path := range []string{e.leasePath(old), e.leasePath(old) + ".json"} {
			if err := os.Chtimes(path, stale, stale); err != nil {
				t.Fatal(err)
			}
		}
		// kept's record holds it past imageLease, within variantLease.
		recent := time.Now().Add(-imageLease - time.Minute)
		if err := os.Chtimes(e.leasePath(kept), recent, recent); err != nil {
			t.Fatal(err)
		}
		if _, err := e.host().Build(context.Background(), e.pkg, BuildSpec{}, -1); err != nil {
			t.Fatal(err)
		}
		if got := e.fake.removed(); !slices.Equal(got, []string{"ensemble-worker:" + old, "ensemble-user:" + old}) {
			t.Fatalf("%q", got)
		}
		for _, suffix := range []string{"", ".json", ".replaced"} {
			if _, err := os.Stat(e.leasePath(old) + suffix); !os.IsNotExist(err) {
				t.Fatalf("a removed key keeps its %q file: %v", suffix, err)
			}
			if _, err := os.Stat(e.leasePath(kept) + suffix); err != nil {
				t.Fatal(err)
			}
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
		stale := time.Now().Add(-variantLease - time.Minute)
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
		stale := time.Now().Add(-variantLease - time.Minute)
		for _, path := range []string{lease, lease + ".json"} {
			if err := os.Chtimes(path, stale, stale); err != nil {
				t.Fatal(err)
			}
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
	want := [][]string{{"image inspect"}, {"image inspect"}, {"container ls", "image ls", "image rm", "image ls"}, {"image inspect"}}
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
	t.Run("a rebuild that cannot take it to record the replaced image", func(t *testing.T) {
		e := newBuildEnv(t)
		e.lockFail = 2
		_, err := e.host().Build(ctx, e.pkg, BuildSpec{}, -1)
		if message, _ := buildFailure(t, err); message != "Taking the image lock ~/.ensemble/docker/images.lock failed." || packageBuild(e.fake.calls) != nil {
			t.Fatalf("%s %q", message, e.fake.verbs())
		}
	})
	t.Run("a prune that cannot take it keeps every image", func(t *testing.T) {
		e := newBuildEnv(t)
		e.images, e.lockFail = "ensemble-worker:"+strings.Repeat("1", 64)+" sha256:"+strings.Repeat("c", 64)+"\n", 3
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
	lacks := func(items string) string {
		return "Base image dummy-env:test lacks " + items + "; add what it lacks to the image, then retry."
	}
	for name, tc := range map[string]struct {
		agent, tools, message string
		toolsUser             string // the tools run's --user; "": no tools run
		err                   error
	}{
		"tools":          {agent: "agent 1000 1000\n", tools: "missing tmux\nmissing npm\n", toolsUser: "1000:1000", message: lacks("npm, tmux")},
		"agent":          {agent: "missing agent\n", toolsUser: "65534:65534", message: lacks("a non-root user agent")},
		"any order":      {agent: "missing agent\n", tools: "missing git\nmissing apt-get\n", toolsUser: "65534:65534", message: lacks("apt-get, git, a non-root user agent")},
		"all":            {agent: "missing agent\n", tools: "missing apt-get\nmissing dpkg\nmissing node\nmissing npm\nmissing tmux\nmissing git\n", toolsUser: "65534:65534", message: lacks("apt-get, dpkg, node, npm, tmux, git, a non-root user agent")},
		"no claude need": {agent: "agent 1 2\n", tools: "missing claude\n", toolsUser: "1:2", message: unexpected},
		"root ids":       {agent: "agent 0 0\n", message: unexpected},
		"short ids":      {agent: "agent 1000\n", message: unexpected},
		"no agent line":  {agent: "", message: unexpected},
		"two lines":      {agent: "missing agent\nagent 1 1\n", message: unexpected},
		"unknown tool":   {agent: "agent 1 2\n", tools: "missing python3\n", toolsUser: "1:2", message: unexpected},
		"agent as tool":  {agent: "agent 1 2\n", tools: "missing agent\n", toolsUser: "1:2", message: unexpected},
		"repeated":       {agent: "agent 1 2\n", tools: "missing git\nmissing git\n", toolsUser: "1:2", message: unexpected},
		"garbage":        {agent: "agent 1 2\n", tools: "tmux\n", toolsUser: "1:2", message: unexpected},
		"docker failed":  {err: &docker.CommandError{Exit: 127, Stderr: "exec: \"/bin/sh\": stat /bin/sh: no such file"}, message: "Checking base image dummy-env:test failed."},
	} {
		t.Run(name, func(t *testing.T) {
			e := newBaseEnv(t)
			e.baseAgent, e.baseTools, e.baseCheckErr = tc.agent, tc.tools, tc.err
			_, err := e.host().Build(context.Background(), e.pkg, BuildSpec{BaseImage: baseRef}, -1)
			message, cause := buildFailure(t, err)
			if message != tc.message {
				t.Fatal(message)
			}
			if tc.err != nil && !strings.Contains(cause, "/bin/sh") {
				t.Fatalf("cause %q", cause)
			}
			run := func(user, mode, path string) []string {
				return []string{"run", "--rm", "-i", "--pull", "never", "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
					"--user", user, "--entrypoint", "/bin/sh", baseID, "-s", mode, path}
			}
			want := [][]string{run("65534:65534", "agent", "/usr/bin:/bin")}
			if tc.toolsUser != "" {
				want = append(want, run(tc.toolsUser, "tools", "/home/agent/.npm-global/bin:/usr/local/bin:/usr/bin:/bin"))
			}
			var got [][]string
			for i, c := range e.fake.calls {
				if slices.Contains(c, "-s") {
					got = append(got, c)
					if string(e.fake.stdin[i]) != string(script) {
						t.Fatal("base check not on stdin")
					}
				}
				if c[0] == "tag" || c[0] == "build" {
					t.Fatalf("%q after a failed check", c)
				}
			}
			if !slices.EqualFunc(got, want, slices.Equal) {
				t.Fatalf("%q want %q", got, want)
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
	if got := e.fake.verbs(); !slices.Equal(got, []string{"image inspect", "image inspect", "run --rm", "run --rm", "tag " + baseID, "run --rm", "image inspect", "build --progress=plain", "image inspect", "container ls", "image ls", "image ls"}) {
		t.Fatalf("%q", got)
	}
	userImage := "ensemble-user:" + e.key
	if tools := e.fake.calls[3]; !slices.Contains(tools, "tools") || tools[slices.Index(tools, "--user")+1] != "1000:1000" {
		t.Fatalf("%q", tools)
	}
	if tag := e.fake.calls[4]; !slices.Equal(tag, []string{"tag", baseID, userImage}) {
		t.Fatalf("%q", tag)
	}
	if check := e.fake.calls[5]; !slices.Contains(check, "node") || !slices.Contains(check, userImage) {
		t.Fatalf("%q", check)
	}
	layer := e.fake.calls[7]
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

func TestBuildReusesCurrentImageWithoutLookup(t *testing.T) {
	e := newBuildEnv(t)
	first, err := e.host().Build(context.Background(), e.pkg, BuildSpec{}, -1)
	if err != nil {
		t.Fatal(err)
	}
	e.releases = &fakeReleases{err: errors.New("no lookup expected")}
	calls := len(e.fake.calls)
	for _, maxAge := range []time.Duration{-1, 7 * 24 * time.Hour} {
		got, err := e.host().Build(context.Background(), e.pkg, BuildSpec{}, maxAge)
		if err != nil || got != (BuildResult{Image: first.Image, Digest: first.Digest}) {
			t.Fatalf("%+v %v", got, err)
		}
	}
	if e.releases.asked != nil || len(e.fake.calls) != calls+2 {
		t.Fatalf("%q %q", e.releases.asked, e.fake.verbs()[calls:])
	}
}

func TestFirstBuildLooksUpLatest(t *testing.T) {
	e := newBuildEnv(t)
	result, err := e.host().Build(context.Background(), e.pkg, BuildSpec{}, -1)
	if err != nil || !result.Rebuilt {
		t.Fatalf("%+v %v", result, err)
	}
	if want := []string{"@anthropic-ai/claude-code", "@openai/codex", "opencode-ai"}; !slices.Equal(e.releases.asked, want) {
		t.Fatalf("%q", e.releases.asked)
	}
	layer := packageBuild(e.fake.calls)
	for _, arg := range []string{"CLAUDE_VERSION=2.1.7", "CODEX_VERSION=0.170.0", "OPENCODE_VERSION=1.20.1"} {
		if i := slices.Index(layer, arg); i < 1 || layer[i-1] != "--build-arg" {
			t.Fatalf("%s absent: %q", arg, layer)
		}
	}
	t.Run("a lookup failure fails it", func(t *testing.T) {
		e := newBuildEnv(t)
		e.releases.err = errors.New("opencode-ai registry unavailable")
		_, err := e.host().Build(context.Background(), e.pkg, BuildSpec{}, -1)
		if message, cause := buildFailure(t, err); message != "Cannot look up the latest client releases; check the network and retry." || cause != "" {
			t.Fatalf("%q %q", message, cause)
		}
		if slices.ContainsFunc(e.fake.calls, func(c []string) bool { return c[0] == "build" }) {
			t.Fatalf("built without versions: %q", e.fake.verbs())
		}
	})
	t.Run("a release below its minimum fails it", func(t *testing.T) {
		e := newBuildEnv(t)
		e.releases.latest["@anthropic-ai/claude-code"] = "2.0.9"
		_, err := e.host().Build(context.Background(), e.pkg, BuildSpec{}, -1)
		if message, _ := buildFailure(t, err); message != "Cannot look up the latest client releases; check the network and retry." {
			t.Fatal(message)
		}
	})
}

func TestBuildRebuildsAnImageWithoutClientLabels(t *testing.T) {
	e := newBuildEnv(t)
	first, err := e.host().Build(context.Background(), e.pkg, BuildSpec{}, -1)
	if err != nil {
		t.Fatal(err)
	}
	for _, label := range clientArgs {
		delete(e.built[first.Image], label)
	}
	e.releases.asked = nil
	again, err := e.host().Build(context.Background(), e.pkg, BuildSpec{}, -1)
	if err != nil || !again.Rebuilt || len(e.releases.asked) != 3 {
		t.Fatalf("%+v %v %q", again, err, e.releases.asked)
	}
}

func TestRefreshKeepsImageWhenLookupFails(t *testing.T) {
	e := newBuildEnv(t)
	first, err := e.host().Build(context.Background(), e.pkg, BuildSpec{}, -1)
	if err != nil {
		t.Fatal(err)
	}
	e.age, e.releases.err = 8*24*time.Hour, errors.New("@openai/codex registry unavailable")
	e.progress.Reset()
	calls := len(e.fake.calls)
	got, err := e.host().Build(context.Background(), e.pkg, BuildSpec{}, 7*24*time.Hour)
	if err != nil || got != (BuildResult{Image: first.Image, Digest: first.Digest}) {
		t.Fatalf("%+v %v", got, err)
	}
	if want := "ensemble-broker: kept " + first.Image + ": refreshing it failed: @openai/codex registry unavailable\n"; e.progress.String() != want {
		t.Fatalf("%q", e.progress.String())
	}
	if got := e.fake.verbs()[calls:]; !slices.Equal(got, []string{"image inspect"}) {
		t.Fatalf("%q", got)
	}
	_, err = e.host().Build(context.Background(), e.pkg, BuildSpec{}, 0)
	if message, _ := buildFailure(t, err); message != "Cannot look up the latest client releases; check the network and retry." {
		t.Fatal(message)
	}
}

func TestWorkerImageInstallsClientsAtBuildArgVersions(t *testing.T) {
	data, err := assets.ReadFile("assets/Dockerfile")
	if err != nil {
		t.Fatal(err)
	}
	text := string(data)
	for _, want := range []string{
		"ARG CLAUDE_VERSION\n", "ARG CODEX_VERSION\n", "ARG OPENCODE_VERSION\n",
		`test -n "${CLAUDE_VERSION}" && test -n "${CODEX_VERSION}" && test -n "${OPENCODE_VERSION}"`,
		"npm install --global --ignore-scripts --registry=https://registry.npmjs.org --prefix /opt/ensemble/clients @anthropic-ai/claude-code@${CLAUDE_VERSION} @openai/codex@${CODEX_VERSION} \\\n",
		"npm rebuild --global --prefix /opt/ensemble/clients --ignore-scripts=false @anthropic-ai/claude-code \\\n",
		`python3 -I /opt/ensemble/lib/opencode-install.py "$(dpkg --print-architecture)" /opt/ensemble/clients "${OPENCODE_VERSION}"`,
		`/opt/ensemble/clients/bin/claude --version | cut -d ' ' -f 1)" = "${CLAUDE_VERSION}"`,
		`python3 -I /opt/ensemble/lib/codex-probe.py /opt/ensemble/clients/bin/codex "${CODEX_VERSION}"`,
		`/opt/ensemble/clients/bin/opencode --version)" = "${OPENCODE_VERSION}"`,
		"ln -s /opt/ensemble/clients/bin/claude /opt/ensemble/bin/claude",
		"LABEL ensemble.claude-version=${CLAUDE_VERSION} ensemble.codex-version=${CODEX_VERSION} ensemble.opencode-version=${OPENCODE_VERSION}\n",
	} {
		if !strings.Contains(text, want) {
			t.Errorf("Dockerfile lacks %q", want)
		}
	}
	if strings.Count(text, "npm install") != 1 || strings.Contains(text, "@0.163.0") {
		t.Error("Dockerfile installs a client another way")
	}
}

// writeCheckState writes the client check state as check-clients would.
func (e *buildEnv) writeCheckState(t *testing.T, checkedAt time.Time, failure string) {
	t.Helper()
	data, err := json.Marshal(map[string]any{"checked_at": checkedAt, "failure": failure})
	if err != nil {
		t.Fatal(err)
	}
	putFile(t, filepath.Join(e.home, ".ensemble/docker/clients-check.json"), string(data))
}

// private reads path, failing unless it is a 0600 file.
func private(t *testing.T, path string) string {
	t.Helper()
	info, err := os.Stat(path)
	if err != nil || info.Mode() != 0o600 {
		t.Fatalf("%s: %v %v", path, info, err)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

func TestBuildSpawnsClientCheckWhenDue(t *testing.T) {
	ctx := context.Background()
	for name, tc := range map[string]struct {
		state string // "": none
		age   time.Duration
		due   bool
	}{
		"absent":     {due: true},
		"old":        {state: "ok", age: clientCheckInterval, due: true},
		"fresh":      {state: "ok", age: clientCheckInterval - time.Minute},
		"unreadable": {state: "{", due: true},
	} {
		t.Run(name, func(t *testing.T) {
			e := newBuildEnv(t)
			switch tc.state {
			case "ok":
				e.writeCheckState(t, time.Now().Add(-tc.age), "")
			case "{":
				putFile(t, filepath.Join(e.home, ".ensemble/docker/clients-check.json"), "{")
			}
			spawns := 0
			h := e.host()
			h.Spawn = func() error { spawns++; return nil }
			for _, wantRebuilt := range []bool{true, false} {
				if got, err := h.Build(ctx, e.pkg, BuildSpec{}, -1); err != nil || got.Rebuilt != wantRebuilt {
					t.Fatalf("%+v %v", got, err)
				}
			}
			if want := map[bool]int{true: 2, false: 0}[tc.due]; spawns != want {
				t.Fatalf("spawned %d times, want %d", spawns, want)
			}
		})
	}
	t.Run("a spawn failure is a note", func(t *testing.T) {
		e := newBuildEnv(t)
		h := e.host()
		h.Spawn = func() error { return errors.New("exec format error") }
		if got, err := h.Build(ctx, e.pkg, BuildSpec{}, -1); err != nil || !got.Rebuilt {
			t.Fatalf("%+v %v", got, err)
		}
		if !strings.HasSuffix(e.progress.String(), "ensemble-broker: cannot start the client check: exec format error\n") {
			t.Fatalf("%q", e.progress.String())
		}
	})
	t.Run("a failed build spawns nothing", func(t *testing.T) {
		e := newBuildEnv(t)
		e.failStage = "ensemble-user"
		h := e.host()
		h.Spawn = func() error { t.Fatal("spawned"); return nil }
		if _, err := h.Build(ctx, e.pkg, BuildSpec{}, -1); err == nil {
			t.Fatal("built")
		}
	})
}

func TestBuildReportsCheckFailureOnce(t *testing.T) {
	ctx := context.Background()
	e := newBuildEnv(t)
	if _, err := e.host().Build(ctx, e.pkg, BuildSpec{}, -1); err != nil {
		t.Fatal(err)
	}
	marker := filepath.Join(e.home, ".ensemble/docker/clients-check.reported")
	build := func() string {
		t.Helper()
		e.progress.Reset()
		if _, err := e.host().Build(ctx, e.pkg, BuildSpec{}, -1); err != nil {
			t.Fatal(err)
		}
		return e.progress.String()
	}
	const failed = "ensemble-worker:" + "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef" + ": Building the ensemble image stage failed."
	note := func(failure string) string {
		return "ensemble-broker: client check failed: " + failure + "; starts use the current image\n"
	}
	e.writeCheckState(t, time.Now(), failed)
	if got := build(); got != note(failed) {
		t.Fatalf("%q", got)
	}
	if got := private(t, marker); got != failed {
		t.Fatalf("marker %q", got)
	}
	if got := build(); got != "" {
		t.Fatalf("reported twice: %q", got)
	}
	e.writeCheckState(t, time.Now().Add(time.Minute), failed)
	if got := build(); got != "" {
		t.Fatalf("a later check with the same failure reported: %q", got)
	}
	e.writeCheckState(t, time.Now(), "")
	if got := build(); got != "" || private(t, marker) != "" {
		t.Fatalf("%q", got)
	}
	e.writeCheckState(t, time.Now(), failed)
	if got := build(); got != note(failed) {
		t.Fatalf("a failure after a clear not reported: %q", got)
	}
	e.writeCheckState(t, time.Now(), "registry: @openai/codex registry unavailable")
	if got := build(); got != note("registry: @openai/codex registry unavailable") {
		t.Fatalf("a new failure not reported: %q", got)
	}
	t.Run("no state, no marker", func(t *testing.T) {
		e := newBuildEnv(t)
		if _, err := e.host().Build(ctx, e.pkg, BuildSpec{}, -1); err != nil {
			t.Fatal(err)
		}
		if _, err := os.Stat(filepath.Join(e.home, ".ensemble/docker/clients-check.reported")); !os.IsNotExist(err) {
			t.Fatalf("marker written: %v", err)
		}
	})
}

func TestBuildRecordsVariant(t *testing.T) {
	ctx := context.Background()
	plugin := "x@owner/repo#" + strings.Repeat("a", 40)
	e := newBaseEnv(t)
	got, err := e.host().Build(ctx, e.pkg, BuildSpec{BaseImage: baseRef, Plugins: []string{plugin}, ImagePlugins: []string{"beta", "alpha"}}, -1)
	if err != nil {
		t.Fatal(err)
	}
	record := filepath.Join(e.home, ".ensemble/docker/images", strings.TrimPrefix(got.Image, "ensemble-worker:")+".json")
	want := `{"package":"` + e.pkg + `","base_image":"dummy-env:test","plugins":["` + plugin + `"],"image_plugins":["alpha","beta"]}`
	if data := private(t, record); !fixtures.SameJSON(t, []byte(data), []byte(want)) {
		t.Fatalf("%s", data)
	}
	e = newBuildEnv(t)
	got, err = e.host().Build(ctx, e.pkg, BuildSpec{}, -1)
	if err != nil {
		t.Fatal(err)
	}
	record = filepath.Join(e.home, ".ensemble/docker/images", strings.TrimPrefix(got.Image, "ensemble-worker:")+".json")
	if data := private(t, record); !fixtures.SameJSON(t, []byte(data), []byte(`{"package":"`+e.pkg+`","base_image":"","plugins":[],"image_plugins":[]}`)) {
		t.Fatalf("%s", data)
	}
	t.Run("a write failure is a note", func(t *testing.T) {
		if err := os.Remove(record); err != nil {
			t.Fatal(err)
		}
		putFile(t, filepath.Join(record, "blocker"), "")
		e.progress.Reset()
		if again, err := e.host().Build(ctx, e.pkg, BuildSpec{}, -1); err != nil || again.Rebuilt {
			t.Fatalf("%+v %v", again, err)
		}
		if !strings.HasPrefix(e.progress.String(), "ensemble-broker: cannot record "+got.Image+" for the client check: ") {
			t.Fatalf("%q", e.progress.String())
		}
	})
}

func TestBuildDuringCheckRebuildDoesNotWait(t *testing.T) {
	ctx := context.Background()
	e := newBuildEnv(t)
	first, err := e.host().Build(ctx, e.pkg, BuildSpec{}, -1)
	if err != nil {
		t.Fatal(err)
	}
	e.releases.latest["@openai/codex"] = "0.171.0"
	building, release := make(chan struct{}), make(chan struct{})
	inner := e.fake.respond
	e.fake.respond = func(args []string) ([]byte, error) {
		if args[0] == "build" && slices.Contains(args, "CODEX_VERSION=0.171.0") {
			close(building)
			<-release
		}
		return inner(args)
	}
	done := make(chan error)
	go func() {
		_, err := e.host().CheckClients(ctx)
		done <- err
	}()
	<-building
	// The fake image lock fails rather than waits: a check holding it across docker build fails this build.
	got, err := e.host().Build(ctx, e.pkg, BuildSpec{}, -1)
	close(release)
	if err != nil || got != (BuildResult{Image: first.Image, Digest: first.Digest}) {
		t.Fatalf("%+v %v", got, err)
	}
	if err := <-done; err != nil {
		t.Fatal(err)
	}
}

func TestRebuildRecordsReplacedImage(t *testing.T) {
	ctx := context.Background()
	e := newBuildEnv(t)
	e.ids = map[string]string{}
	clock := time.Date(2026, 10, 26, 12, 0, 0, 0, time.UTC)
	h := e.host()
	h.Now = func() time.Time { return clock }
	first, err := h.Build(ctx, e.pkg, BuildSpec{}, -1)
	if err != nil {
		t.Fatal(err)
	}
	replaced := filepath.Join(e.home, ".ensemble/docker/images", strings.TrimPrefix(first.Image, "ensemble-worker:")+".replaced")
	if _, err := os.Stat(replaced); !os.IsNotExist(err) {
		t.Fatalf("a first build replaced nothing: %v", err)
	}
	other, stale := "sha256:"+strings.Repeat("e", 64), "sha256:"+strings.Repeat("f", 64)
	putFile(t, replaced, `[{"id":"`+other+`","at":"2026-10-26T11:55:00Z"},{"id":"`+stale+`","at":"2026-10-26T11:49:59Z"}]`)
	// A long build: the clock moves on while the ensemble layer builds.
	inner := e.fake.respond
	e.fake.respond = func(args []string) ([]byte, error) {
		if args[0] == "build" && slices.Contains(args, "--build-arg") {
			clock = clock.Add(4 * time.Minute)
		}
		return inner(args)
	}
	again, err := h.Build(ctx, e.pkg, BuildSpec{}, 0)
	if err != nil || !again.Rebuilt || again.Digest == first.Digest {
		t.Fatalf("%+v %v", again, err)
	}
	want := `[{"id":"` + other + `","at":"2026-10-26T11:55:00Z"},{"id":"` + first.Digest + `","at":"2026-10-26T12:04:00Z"}]`
	if got := private(t, replaced); !fixtures.SameJSON(t, []byte(got), []byte(want)) {
		t.Fatalf("%s", got)
	}
	t.Run("an inspect failure fails the rebuild before docker build", func(t *testing.T) {
		e := newBuildEnv(t)
		if _, err := e.host().Build(ctx, e.pkg, BuildSpec{}, -1); err != nil {
			t.Fatal(err)
		}
		inner := e.fake.respond
		inspects := 0
		e.fake.respond = func(args []string) ([]byte, error) {
			if args[0] == "image" && args[1] == "inspect" {
				if inspects++; inspects == 2 {
					return nil, &docker.CommandError{Exit: 1, Stderr: "boom"}
				}
			}
			return inner(args)
		}
		calls := len(e.fake.calls)
		_, err := e.host().Build(ctx, e.pkg, BuildSpec{}, 0)
		if message, _ := buildFailure(t, err); message != "Cannot inspect the worker image." || packageBuild(e.fake.calls[calls:]) != nil {
			t.Fatalf("%s %q", message, e.fake.verbs()[calls:])
		}
	})
}

func TestPruneKeepsReplacedImageForGrace(t *testing.T) {
	young, old := "sha256:"+strings.Repeat("3", 64), "sha256:"+strings.Repeat("4", 64)
	e := newBuildEnv(t)
	e.dangling = young + "\n" + old + "\n"
	clock := time.Date(2026, 10, 26, 12, 0, 0, 0, time.UTC)
	h := e.host()
	h.Now = func() time.Time { return clock }
	putFile(t, filepath.Join(e.home, ".ensemble/docker/images", strings.Repeat("1", 64)+".replaced"), `[{"id":"`+young+`","at":"2026-10-26T11:51:00Z"}]`)
	putFile(t, filepath.Join(e.home, ".ensemble/docker/images", strings.Repeat("2", 64)+".replaced"), `[{"id":"`+old+`","at":"2026-10-26T11:50:00Z"}]`)
	h.prune(context.Background(), "")
	if got := e.fake.removed(); !slices.Equal(got, []string{old}) {
		t.Fatalf("%q", got)
	}
	clock = clock.Add(time.Minute)
	h.prune(context.Background(), "")
	if got := e.fake.removed(); !slices.Equal(got, []string{young, old}) {
		t.Fatalf("%q", got)
	}
	t.Run("an entry whose rebuild still runs outlives the grace", func(t *testing.T) {
		putFile(t, filepath.Join(e.home, ".ensemble/docker/images", strings.Repeat("1", 64)+".replaced"),
			`[{"id":"`+young+`","at":"2026-10-26T11:00:00Z","pending":true}]`)
		h.prune(context.Background(), "")
		if got := e.fake.removed(); !slices.Equal(got, []string{old}) {
			t.Fatalf("%q", got)
		}
		clock = clock.Add(pendingGrace)
		h.prune(context.Background(), "")
		if got := e.fake.removed(); !slices.Equal(got, []string{young, old}) {
			t.Fatalf("%q", got)
		}
	})
}

func TestPruneKeepsRecordedVariantsForTheVariantLease(t *testing.T) {
	recorded, unrecorded := strings.Repeat("a", 64), strings.Repeat("b", 64)
	e := newBuildEnv(t)
	e.images = "ensemble-worker:" + recorded + " sha256:" + strings.Repeat("5", 64) + "\n" +
		"ensemble-worker:" + unrecorded + " sha256:" + strings.Repeat("6", 64) + "\n"
	clock := time.Now()
	h := e.host()
	h.Now = func() time.Time { return clock }
	for _, key := range []string{recorded, unrecorded} {
		putFile(t, e.leasePath(key), "")
	}
	putFile(t, e.leasePath(recorded)+".json", "{}")
	clock = clock.Add(2 * time.Hour)
	h.prune(context.Background(), "")
	if got := e.fake.removed(); !slices.Equal(got, []string{"ensemble-worker:" + unrecorded}) {
		t.Fatalf("%q", got)
	}
	e.images = "ensemble-worker:" + recorded + " sha256:" + strings.Repeat("5", 64) + "\n"
	clock = clock.Add(variantLease)
	h.prune(context.Background(), "")
	if got := e.fake.removed(); !slices.Equal(got, []string{"ensemble-worker:" + recorded}) {
		t.Fatalf("%q", got)
	}
}
