package launcher

import (
	"context"
	"encoding/json"
	"fmt"
	"slices"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/protocol"
)

type lockObject struct {
	id, name, state string
	labels          map[string]string
}

// lockWorld models Docker's unique names and immutable IDs, shared by independent starters.
type lockWorld struct {
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
	case "create":
		name := flag(args, "--name")
		for _, c := range w.objects {
			if c.name == name {
				return nil, &docker.CommandError{Exit: 125, Stderr: "Conflict. The container name"}, true
			}
		}
		labels := map[string]string{}
		for i, a := range args {
			if a == "--label" {
				k, v, _ := strings.Cut(args[i+1], "=")
				labels[k] = v
			}
		}
		return []byte(w.add(name, "created", labels) + "\n"), nil, true
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
		if args[1] != "inspect" {
			return nil, nil, false
		}
		ref := args[len(args)-1]
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

func (w *lockWorld) host(t *testing.T, name string, now int64) (Host, string, *fakeDocker) {
	t.Helper()
	h, base := startHost(&world{labels: goodLabels(), volume: "matching"}, &fakeCreds{})
	d := &fakeDocker{respond: func(args []string) ([]byte, error) {
		out, err, handled := w.respond(args)
		if handled {
			if w.after != nil {
				w.after(name, args, out)
			}
			return out, err
		}
		return base.respond(args)
	}}
	h.Docker = d
	h.Now = func() time.Time { return time.Unix(now, 0) }
	path := editedManifest(t, func(f map[string]any) {
		f["name"], f["workspace"], f["lock"], f["lock_owner"] = name, "ensemble-ws-"+name, "dummy", name
		f["claude_dir"] = home + "/.ensemble/docker-state/" + name + "/claude"
	})
	return h, path, d
}

func (w *lockWorld) assertFinal(t *testing.T, holders int) {
	t.Helper()
	w.mu.Lock()
	defer w.mu.Unlock()
	active := 0
	for _, c := range w.objects {
		if strings.HasPrefix(c.name, "windroc-lock-") {
			t.Errorf("left mutex %+v", c)
		}
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
	h, path, d := w.host(t, "worker_1", 1000)
	if _, err := h.Start(context.Background(), path); err != nil {
		t.Fatal(err)
	}
	w.assertFinal(t, 1)
	var mutex []string
	for _, a := range d.calls {
		if a[0] == "create" {
			mutex = a
		}
	}
	for k, v := range map[string]string{"--pull": "never", "--network": "none", "--name": "windroc-lock-dummy", "--restart": "no"} {
		if flag(mutex, k) != v {
			t.Errorf("mutex %s: %q", k, mutex)
		}
	}
	for _, v := range []string{"windroc.lock.mutex=dummy", "windroc.lock.since=1000", digest} {
		if !slices.Contains(mutex, v) {
			t.Errorf("missing %s: %q", v, mutex)
		}
	}
	run := detached(d)
	if flag(run, "--restart") != "no" || !slices.Contains(run, "windroc.lock=dummy") || !slices.Contains(run, "windroc.lock.owner=worker_1") {
		t.Fatalf("run %q", run)
	}
	for _, a := range d.calls {
		if a[0] == "rm" && !containerIDPattern.MatchString(a[len(a)-1]) {
			t.Fatalf("mutex removed by name: %q", a)
		}
	}
}

func TestLockHolderStates(t *testing.T) {
	for _, state := range []string{"running", "paused", "restarting", "created", "exited", "dead"} {
		t.Run(state, func(t *testing.T) {
			w := newLockWorld()
			id := w.add("other-container", state, map[string]string{"windroc.lock": "dummy", "windroc.lock.owner": "other-session"})
			h, path, d := w.host(t, "worker_1", 1000)
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

func TestMutexExpiry(t *testing.T) {
	for _, age := range []int64{0, 119, 120, 121} {
		t.Run(strconv.FormatInt(age, 10), func(t *testing.T) {
			w := newLockWorld()
			oldID := w.add("windroc-lock-dummy", "created", map[string]string{"windroc.lock.mutex": "dummy", "windroc.lock.since": "1000"})
			h, path, d := w.host(t, "worker_1", 1000+age)
			_, err := h.Start(context.Background(), path)
			if age <= 120 {
				e := protoErr(t, err)
				if e.Code != protocol.Busy || e.Holder.Owner != "starting" || detached(d) != nil {
					t.Fatalf("%+v", e)
				}
				if _, ok := w.objects[oldID]; !ok {
					t.Fatal("fresh mutex removed")
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			w.assertFinal(t, 1)
			if !slices.ContainsFunc(d.calls, func(a []string) bool { return a[0] == "rm" && a[len(a)-1] == oldID }) {
				t.Fatal("stale mutex not removed by ID")
			}
		})
	}
	for _, since := range []string{"", "bad", "-1", "999999999999999999999999999", "2000"} {
		w := newLockWorld()
		w.add("windroc-lock-dummy", "created", map[string]string{"windroc.lock.mutex": "dummy", "windroc.lock.since": since})
		h, path, d := w.host(t, "worker_1", 1000)
		if _, err := h.Start(context.Background(), path); code(t, err) != protocol.Busy || hasCall(d, "rm") {
			t.Fatalf("since %q: %v", since, err)
		}
	}
}

func TestLockConfigBeforeBusy(t *testing.T) {
	for _, kind := range []string{"credentials", "image", "mount"} {
		w := newLockWorld()
		w.add("windroc-lock-dummy", "created", map[string]string{"windroc.lock.mutex": "dummy", "windroc.lock.since": "1000"})
		h, path, d := w.host(t, "worker_1", 1000)
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
			if a[0] == "ps" && strings.HasPrefix(flag(a, "--filter"), "label=") {
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
		h, path, _ := w.host(t, "worker_1", 1000)
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
	h1, p1, _ := w.host(t, "first", 1000)
	h2, p2, _ := w.host(t, "second", 1000)
	done := make(chan error, 1)
	go func() { _, err := h1.Start(context.Background(), p1); done <- err }()
	<-entered
	_, err := h2.Start(context.Background(), p2)
	e := protoErr(t, err)
	if e.Code != protocol.Busy || e.Holder.Container != "windroc-lock-dummy" || e.Holder.Owner != "starting" {
		t.Errorf("%+v", e)
	}
	close(release)
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	w.assertFinal(t, 1)
}

func TestExpiredMutexInterleavings(t *testing.T) {
	for _, both := range []bool{false, true} {
		t.Run(fmt.Sprintf("both_recheck_%t", both), func(t *testing.T) {
			w := newLockWorld()
			firstAtRun, secondAtRun := make(chan struct{}), make(chan struct{})
			firstRun, secondRun := make(chan struct{}), make(chan struct{})
			postQueries, peerSnapshots := make(chan struct{}), make(chan struct{})
			var mu sync.Mutex
			queries, peers := 0, 0
			w.before = func(a []string) {
				if a[0] == "run" && a[1] == "--detach" {
					if strings.Contains(flag(a, "--name"), "ensemble-first-") {
						close(firstAtRun)
						<-firstRun
					} else {
						close(secondAtRun)
						<-secondRun
					}
				}
				if both && a[0] == "ps" && strings.HasPrefix(flag(a, "--filter"), "label=") {
					mu.Lock()
					queries++
					post := queries > 2
					if queries == 4 {
						close(postQueries)
					}
					mu.Unlock()
					if post {
						<-postQueries
					}
				}
			}
			w.after = func(name string, a []string, out []byte) {
				if !both || a[0] != "container" || a[1] != "inspect" {
					return
				}
				var cs []lockContainer
				if json.Unmarshal(out, &cs) != nil || len(cs) != 1 || cs[0].Config.Labels["windroc.lock"] != "dummy" || strings.HasPrefix(cs[0].Name, "/ensemble-"+name+"-") {
					return
				}
				mu.Lock()
				peers++
				if peers == 2 {
					close(peerSnapshots)
				}
				mu.Unlock()
				<-peerSnapshots
			}
			h1, p1, _ := w.host(t, "first", 1000)
			h2, p2, _ := w.host(t, "second", 1121)
			e1, e2 := make(chan error, 1), make(chan error, 1)
			go func() { _, err := h1.Start(context.Background(), p1); e1 <- err }()
			<-firstAtRun
			go func() { _, err := h2.Start(context.Background(), p2); e2 <- err }()
			<-secondAtRun
			close(firstRun)
			if !both {
				if err := <-e1; err != nil {
					t.Fatal(err)
				}
				// The first starter's cleanup must leave the second starter's same-name mutex intact.
				w.mu.Lock()
				mutexes := 0
				for _, c := range w.objects {
					if c.name == "windroc-lock-dummy" {
						mutexes++
					}
				}
				w.mu.Unlock()
				if mutexes != 1 {
					t.Fatalf("removed replacement mutex: %d", mutexes)
				}
				close(secondRun)
				e := protoErr(t, <-e2)
				if e.Code != protocol.Busy || e.Holder.Owner != "first" || !strings.Contains(e.Holder.Container, "ensemble-first-") {
					t.Fatalf("%+v", e)
				}
				w.assertFinal(t, 1)
			} else {
				close(secondRun)
				for i, ch := range []chan error{e1, e2} {
					e := protoErr(t, <-ch)
					want := []string{"second", "first"}[i]
					if e.Code != protocol.Busy || e.Cleanup != protocol.Removed || e.Holder.Owner != want || !strings.Contains(e.Holder.Container, "ensemble-"+want+"-") {
						t.Fatalf("%+v", e)
					}
				}
				w.assertFinal(t, 0)
			}
		})
	}
}

func TestMutexRetryClashAndForeignContainer(t *testing.T) {
	for _, foreign := range []bool{false, true} {
		w := newLockWorld()
		labels := map[string]string{"windroc.lock.mutex": "dummy", "windroc.lock.since": "0"}
		if foreign {
			delete(labels, "windroc.lock.mutex")
		}
		w.add("windroc-lock-dummy", "created", labels)
		creates := 0
		w.fail = func(a []string) error {
			if a[0] == "create" {
				creates++
				if creates == 2 {
					return &docker.CommandError{Exit: 125, Stderr: "Conflict. The container name"}
				}
			}
			return nil
		}
		h, path, d := w.host(t, "worker_1", 1000)
		_, err := h.Start(context.Background(), path)
		if e := protoErr(t, err); e.Code != protocol.Busy || e.Holder.Owner != "starting" || detached(d) != nil {
			t.Fatalf("%+v", e)
		}
		want := 2
		if foreign {
			want = 1
		}
		if creates != want {
			t.Fatalf("creates=%d want %d", creates, want)
		}
		if foreign && hasCall(d, "rm") {
			t.Fatal("removed foreign container")
		}
	}
}

func TestMutexCleanup(t *testing.T) {
	for _, phase := range []string{"query", "recheck", "run", "bootstrap", "cancel", "release", "yield release", "run conflict", "stale delete", "create"} {
		t.Run(phase, func(t *testing.T) {
			w := newLockWorld()
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			queries := 0
			w.before = func(a []string) {
				if a[0] == "ps" && strings.HasPrefix(flag(a, "--filter"), "label=") {
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
				case "release", "yield release":
					fail = a[0] == "rm" && a[len(a)-1] != container
				case "run conflict":
					if a[0] == "run" && a[1] == "--detach" {
						return &docker.CommandError{Exit: 125, Stderr: "Conflict. The container name"}
					}
				case "stale delete":
					fail = a[0] == "rm"
				case "create":
					fail = a[0] == "create"
				}
				if fail {
					return fmt.Errorf("failed %s", phase)
				}
				return nil
			}
			if phase == "stale delete" {
				w.add("windroc-lock-dummy", "created", map[string]string{"windroc.lock.mutex": "dummy", "windroc.lock.since": "0"})
			}
			if phase == "yield release" {
				w.add("holder", "running", map[string]string{"windroc.lock": "dummy", "windroc.lock.owner": "owner"})
			}
			h, path, d := w.host(t, "worker_1", 1000)
			var progress strings.Builder
			h.Progress = &progress
			_, err := h.Start(ctx, path)
			e := protoErr(t, err)
			if e.Code != protocol.StartFailed || phase == "release" && e.Cleanup != protocol.Removed {
				t.Fatalf("%+v", e)
			}
			if progress.Len() != 0 {
				t.Fatalf("failed start emitted success progress: %q", progress.String())
			}
			if phase == "release" || phase == "yield release" || phase == "stale delete" {
				if !strings.Contains(e.Message, "mutex") {
					t.Fatalf("%+v", e)
				}
			} else {
				w.assertFinal(t, 0)
			}
			for i, a := range d.calls {
				if a[0] == "rm" && d.ctxErrs[i] != nil {
					t.Fatalf("cleanup used cancelled context: %q", a)
				}
			}
		})
	}
}

func TestMutexFailureRevokesIssuedTokens(t *testing.T) {
	for _, revocationFails := range []bool{false, true} {
		t.Run(fmt.Sprintf("revocation_fails_%t", revocationFails), func(t *testing.T) {
			w := newLockWorld()
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			w.fail = func(a []string) error {
				if a[0] == "rm" && a[len(a)-1] != container {
					cancel()
					return fmt.Errorf("cannot remove mutex")
				}
				return nil
			}
			h, _, _ := w.host(t, "worker_1", 1000)
			tokens := &fakeTokens{}
			if revocationFails {
				tokens.revoke = protocol.New(protocol.KeychainFailed, "revocation queued for retry")
			}
			tokens.onRevoke = func() {
				w.mu.Lock()
				defer w.mu.Unlock()
				if !slices.ContainsFunc(tokens.calls, func(c string) bool { return strings.HasPrefix(c, "issue ") }) {
					t.Error("revoked before issuance")
				}
				found := false
				for _, c := range w.objects {
					if c.name == container {
						found = true
					}
				}
				if !found {
					t.Error("removed run before revoking its tokens")
				}
			}
			h.GitHub = tokens
			path := editedManifest(t, func(f map[string]any) {
				f["lock"], f["lock_owner"] = "dummy", "worker_1"
				f["github"] = map[string]any{"app": "ensemble-bot", "permissions": map[string]string{"contents": "read"}}
			})
			got, err := h.Start(ctx, path)
			e := protoErr(t, err)
			if e.Code != protocol.StartFailed || e.Cleanup != protocol.Removed || got.Container != "" {
				t.Fatalf("%+v %+v", got, e)
			}
			if !slices.Equal(tokens.calls, []string{"ready ensemble-bot", "user ensemble-bot", "retry", "issue ensemble-bot " + container, "revoke " + container}) {
				t.Fatalf("%q", tokens.calls)
			}
			if len(tokens.revokeContexts) != 1 || tokens.revokeContexts[0] != nil {
				t.Fatalf("revocation used cancelled context: %v", tokens.revokeContexts)
			}
			if revocationFails && !strings.Contains(e.Message, "revocation queued for retry") {
				t.Fatalf("revocation failure hidden: %+v", e)
			}
		})
	}
}
