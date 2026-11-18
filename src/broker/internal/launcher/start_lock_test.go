package launcher

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"syscall"
	"testing"

	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/protocol"
)

type lockObject struct {
	id, name, state string
	labels          map[string]string
}

// lockWorld models Docker's unique names and immutable IDs, shared by independent starters, and their one Home.
type lockWorld struct {
	home     string
	mu       sync.Mutex
	objects  map[string]lockObject
	sequence int
	calls    [][]string
	before   func([]string)
	after    func(string, []string, []byte)
	fail     func([]string) error
}

func newLockWorld() *lockWorld { return &lockWorld{objects: map[string]lockObject{}} }

func flag(args []string, key string) string {
	if i := slices.Index(args, key); i >= 0 && i+1 < len(args) {
		return args[i+1]
	}
	return ""
}

func (w *lockWorld) add(name, state string, labels map[string]string) string {
	w.sequence++
	id := fmt.Sprintf("%064x", w.sequence)
	w.objects[id] = lockObject{id: id, name: name, state: state, labels: labels}
	return id
}

func (w *lockWorld) respond(args []string) ([]byte, error, bool) {
	if w.before != nil {
		w.before(args)
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	w.calls = append(w.calls, slices.Clone(args))
	if w.fail != nil {
		if err := w.fail(args); err != nil {
			return nil, err, true
		}
	}
	switch args[0] {
	case "ps":
		if !strings.HasPrefix(flag(args, "--filter"), "label=windroc.lock=") {
			return nil, nil, false
		}
		lock := strings.TrimPrefix(flag(args, "--filter"), "label=windroc.lock=")
		var ids []string
		for id, c := range w.objects {
			if c.labels["windroc.lock"] == lock {
				ids = append(ids, id)
			}
		}
		slices.Sort(ids)
		return []byte(strings.Join(ids, "\n")), nil, true
	case "container":
		ref := args[len(args)-1]
		if args[1] != "inspect" || ref == proxyID { // the egress proxy is the base world's
			return nil, nil, false
		}
		for _, c := range w.objects {
			if c.id == ref || c.name == ref {
				out := lockInspect(c.id, c.name, c.state, c.labels)
				return out, nil, true
			}
		}
		return nil, &docker.CommandError{Exit: 1, Stderr: "No such container"}, true
	case "run":
		if args[1] != "--detach" {
			return nil, nil, false
		}
		labels := map[string]string{}
		for i, a := range args {
			if a == "--label" {
				k, v, _ := strings.Cut(args[i+1], "=")
				labels[k] = v
			}
		}
		return []byte(w.add(flag(args, "--name"), "running", labels) + "\n"), nil, true
	case "rm":
		ref := args[len(args)-1]
		for id, c := range w.objects {
			if id == ref || c.name == ref {
				delete(w.objects, id)
				return []byte(ref + "\n"), nil, true
			}
		}
		return nil, nil, true
	}
	return nil, nil, false
}

func (w *lockWorld) host(t *testing.T, name string) (Host, string, *fakeDocker) {
	t.Helper()
	if w.home == "" {
		w.home = t.TempDir()
	}
	h, base := startHost(&world{labels: goodLabels(), volume: "matching"}, &fakeCreds{})
	h.Home = w.home
	d := &fakeDocker{respond: func(args []string, stdin []byte) ([]byte, error) {
		out, err, handled := w.respond(args)
		if handled {
			if w.after != nil {
				w.after(name, args, out)
			}
			return out, err
		}
		return base.respond(args, stdin)
	}}
	h.Docker = d
	path := editedManifest(t, func(f map[string]any) {
		f["name"], f["workspace"], f["lock"], f["lock_owner"] = name, "ensemble-ws-"+name, "dummy", name
		f["claude_dir"] = w.home + "/.ensemble/docker-state/" + name + "/claude"
	})
	return h, path, d
}

// heldBy is the busy holder of lock dummy while another start holds it.
var heldBy = protocol.Holder{Container: "~/.ensemble/docker/locks/dummy.lock", Owner: "starting"}

// takeStartLock takes lock dummy's start lock in home on a descriptor of its own, as another broker's start does;
// closing it is that broker's process ending.
func takeStartLock(home string) (*os.File, error) {
	dir := filepath.Join(home, ".ensemble/docker/locks")
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, err
	}
	f, err := os.OpenFile(filepath.Join(dir, "dummy.lock"), os.O_RDWR|os.O_CREATE, 0o600)
	if err != nil {
		return nil, err
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		f.Close()
		return nil, err
	}
	return f, nil
}

func holdStartLock(t *testing.T, home string) *os.File {
	t.Helper()
	f, err := takeStartLock(home)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { f.Close() })
	return f
}

func (w *lockWorld) assertFinal(t *testing.T, holders int) {
	t.Helper()
	w.mu.Lock()
	defer w.mu.Unlock()
	if f, err := takeStartLock(w.home); err != nil {
		t.Errorf("start lock still held: %v", err)
	} else {
		f.Close()
	}
	active := 0
	for _, c := range w.objects {
		if c.labels["windroc.lock"] == "dummy" && (c.state == "running" || c.state == "paused" || c.state == "restarting") {
			active++
		}
	}
	if active != holders {
		t.Errorf("active holders=%d, want %d", active, holders)
	}
}

func TestLockedStartArguments(t *testing.T) {
	w := newLockWorld()
	h, path, d := w.host(t, "worker_1")
	if _, err := h.Start(context.Background(), path); err != nil {
		t.Fatal(err)
	}
	w.assertFinal(t, 1)
	if hasCall(d, "create") {
		t.Fatalf("container created besides the worker: %q", d.verbs())
	}
	for p, mode := range map[string]os.FileMode{".ensemble/docker/locks": os.ModeDir | 0o700, ".ensemble/docker/locks/dummy.lock": 0o600} {
		if info, err := os.Stat(filepath.Join(w.home, p)); err != nil || info.Mode() != mode {
			t.Fatalf("%s: %v", p, err)
		}
	}
	run := detached(d)
	if flag(run, "--restart") != "no" || !slices.Contains(run, "windroc.lock=dummy") || !slices.Contains(run, "windroc.lock.owner=worker_1") {
		t.Fatalf("run %q", run)
	}
}

// Another broker's start holds the lock until its descriptor closes, as when its process dies.
func TestStartLockHeld(t *testing.T) {
	w := newLockWorld()
	h, path, d := w.host(t, "worker_1")
	other := holdStartLock(t, w.home)
	_, err := h.Start(context.Background(), path)
	if e := protoErr(t, err); e.Code != protocol.Busy || e.Holder == nil || *e.Holder != heldBy || detached(d) != nil || hasCall(d, "create") {
		t.Fatalf("%+v", e)
	}
	other.Close()
	if _, err := h.Start(context.Background(), path); err != nil {
		t.Fatal(err)
	}
	w.assertFinal(t, 1)
}

func TestStartLockUnusable(t *testing.T) {
	w := newLockWorld()
	h, path, d := w.host(t, "worker_1")
	if err := os.MkdirAll(filepath.Join(w.home, ".ensemble/docker"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(w.home, ".ensemble/docker/locks"), nil, 0o600); err != nil {
		t.Fatal(err)
	}
	_, err := h.Start(context.Background(), path)
	if e := protoErr(t, err); e.Code != protocol.StartFailed || !strings.Contains(e.Message, heldBy.Container) || detached(d) != nil {
		t.Fatalf("%+v", e)
	}
}

func TestLockHolderStates(t *testing.T) {
	for _, state := range []string{"running", "paused", "restarting", "created", "exited", "dead"} {
		t.Run(state, func(t *testing.T) {
			w := newLockWorld()
			id := w.add("other-container", state, map[string]string{"windroc.lock": "dummy", "windroc.lock.owner": "other-session"})
			h, path, d := w.host(t, "worker_1")
			_, err := h.Start(context.Background(), path)
			active := state == "running" || state == "paused" || state == "restarting"
			if active {
				e := protoErr(t, err)
				if e.Code != protocol.Busy || e.Holder.Container != "other-container" || e.Holder.Owner != "other-session" || detached(d) != nil {
					t.Fatalf("%+v", e)
				}
			} else if err != nil {
				t.Fatal(err)
			}
			if _, ok := w.objects[id]; !ok {
				t.Fatal("removed pre-existing container")
			}
			w.assertFinal(t, 1)
		})
	}
}

func TestLockConfigBeforeBusy(t *testing.T) {
	for _, kind := range []string{"credentials", "image", "mount"} {
		w := newLockWorld()
		h, path, d := w.host(t, "worker_1")
		holdStartLock(t, w.home)
		want := protocol.LoginRequired
		switch kind {
		case "credentials":
			h.Creds = &fakeCreds{ready: protocol.New(protocol.LoginRequired, "run ensemble-broker login claude")}
		case "image":
			w.fail = func(a []string) error {
				if a[0] == "image" {
					return noImage
				}
				return nil
			}
			want = protocol.ImageRejected
		case "mount":
			h.IsDir = func(string) bool { return false }
			want = protocol.MountRejected
		}
		if _, err := h.Start(context.Background(), path); code(t, err) != want || hasCall(d, "create") {
			t.Fatalf("%s: %v", kind, err)
		}
	}
}

func TestLockRecheckCleanup(t *testing.T) {
	for _, rmFails := range []bool{false, true} {
		w := newLockWorld()
		queries := 0
		w.before = func(a []string) {
			if a[0] == "ps" && strings.HasPrefix(flag(a, "--filter"), "label=windroc.lock=") {
				queries++
				if queries == 2 {
					w.mu.Lock()
					w.add("racer", "running", map[string]string{"windroc.lock": "dummy", "windroc.lock.owner": "racing-session"})
					w.mu.Unlock()
				}
			}
		}
		w.fail = func(a []string) error {
			if rmFails && a[0] == "rm" && a[len(a)-1] == container {
				return fmt.Errorf("cannot remove")
			}
			return nil
		}
		h, path, _ := w.host(t, "worker_1")
		_, err := h.Start(context.Background(), path)
		e := protoErr(t, err)
		if rmFails {
			if e.Code != protocol.StartFailed || e.Cleanup != protocol.Failed || e.Container != container || !strings.Contains(e.Message, container) {
				t.Fatalf("%+v", e)
			}
			w.assertFinal(t, 2)
		} else {
			if e.Code != protocol.Busy || e.Cleanup != protocol.Removed || e.Holder.Container != "racer" || e.Holder.Owner != "racing-session" {
				t.Fatalf("%+v", e)
			}
			w.assertFinal(t, 1)
		}
	}
}

func TestConcurrentLockedStarts(t *testing.T) {
	w := newLockWorld()
	entered, release := make(chan struct{}), make(chan struct{})
	w.before = func(a []string) {
		if a[0] == "run" && a[1] == "--detach" {
			close(entered)
			<-release
		}
	}
	h1, p1, _ := w.host(t, "first")
	h2, p2, _ := w.host(t, "second")
	done := make(chan error, 1)
	go func() { _, err := h1.Start(context.Background(), p1); done <- err }()
	<-entered
	_, err := h2.Start(context.Background(), p2)
	e := protoErr(t, err)
	if e.Code != protocol.Busy || e.Holder == nil || *e.Holder != heldBy {
		t.Errorf("%+v", e)
	}
	close(release)
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	w.assertFinal(t, 1)
}

func TestLockedStartCleanup(t *testing.T) {
	for _, phase := range []string{"query", "recheck", "run", "bootstrap", "cancel", "run conflict"} {
		t.Run(phase, func(t *testing.T) {
			w := newLockWorld()
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			queries := 0
			w.before = func(a []string) {
				if a[0] == "ps" && strings.HasPrefix(flag(a, "--filter"), "label=windroc.lock=") {
					queries++
				}
			}
			w.fail = func(a []string) error {
				fail := false
				switch phase {
				case "query":
					fail = a[0] == "ps" && queries == 1
				case "recheck":
					fail = a[0] == "ps" && queries == 2
				case "run":
					fail = a[0] == "run" && a[1] == "--detach"
				case "bootstrap":
					fail = a[0] == "exec"
				case "cancel":
					if a[0] == "exec" {
						cancel()
						return context.Canceled
					}
				case "run conflict":
					if a[0] == "run" && a[1] == "--detach" {
						return &docker.CommandError{Exit: 125, Stderr: "Conflict. The container name"}
					}
				}
				if fail {
					return fmt.Errorf("failed %s", phase)
				}
				return nil
			}
			h, path, d := w.host(t, "worker_1")
			var progress strings.Builder
			h.Progress = &progress
			_, err := h.Start(ctx, path)
			e := protoErr(t, err)
			if e.Code != protocol.StartFailed {
				t.Fatalf("%+v", e)
			}
			if progress.Len() != 0 {
				t.Fatalf("failed start emitted success progress: %q", progress.String())
			}
			w.assertFinal(t, 0)
			for i, a := range d.calls {
				if a[0] == "rm" && d.ctxErrs[i] != nil {
					t.Fatalf("cleanup used cancelled context: %q", a)
				}
			}
		})
	}
}
