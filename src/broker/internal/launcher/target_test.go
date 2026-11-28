package launcher

import (
	"context"
	"encoding/json"
	"errors"
	"reflect"
	"slices"
	"sync"
	"testing"
	"time"

	"ensemble/broker/internal/docker"
)

// identified answers docker info with id, else err.
func identified(id string, err error) *fakeDocker {
	return &fakeDocker{respond: func(args []string, _ []byte) ([]byte, error) {
		if !slices.Equal(args, docker.InfoID) {
			return nil, nil
		}
		if err != nil {
			return nil, err
		}
		return json.Marshal(id)
	}}
}

const sshPath = docker.SSHDir + ":/usr/bin:/bin:/usr/sbin:/sbin"

func TestTargetListsTheConfiguredDaemons(t *testing.T) {
	local := Daemon{Name: DaemonLocal, Endpoint: localEndpoint, Path: "/usr/bin:/bin:/usr/sbin:/sbin"}
	remote := Daemon{Name: DaemonRemote, Endpoint: remoteEndpoint, Remote: true, Path: sshPath}
	withRunners := func(d Daemon, r docker.Runner) Daemon { d.Runner = r; return d }
	for name, tc := range map[string]struct {
		daemons []Daemon
		want    TargetResult
	}{
		"local only": {
			[]Daemon{withRunners(local, identified("L1", nil))},
			TargetResult{Default: DaemonLocal, Daemons: []TargetDaemon{
				{Name: DaemonLocal, Endpoint: localEndpoint, Path: local.Path, ID: "L1"}}},
		},
		"remote reachable": {
			[]Daemon{withRunners(remote, identified("R1", nil)), withRunners(local, identified("L1", nil))},
			TargetResult{Default: DaemonRemote, Daemons: []TargetDaemon{
				{Name: DaemonRemote, Endpoint: remoteEndpoint, Remote: true, Path: sshPath, ID: "R1"},
				{Name: DaemonLocal, Endpoint: localEndpoint, Path: local.Path, ID: "L1"}}},
		},
		"remote unreachable": {
			[]Daemon{withRunners(remote, identified("", unreachableErr)), withRunners(local, identified("L1", nil))},
			TargetResult{Default: DaemonRemote, Daemons: []TargetDaemon{
				{Name: DaemonRemote, Endpoint: remoteEndpoint, Remote: true, Path: sshPath, Unreachable: "host key mismatch"},
				{Name: DaemonLocal, Endpoint: localEndpoint, Path: local.Path, ID: "L1"}}},
		},
		"unusable ID": {
			[]Daemon{withRunners(local, identified("bad id", nil))},
			TargetResult{Default: DaemonLocal, Daemons: []TargetDaemon{
				{Name: DaemonLocal, Endpoint: localEndpoint, Path: local.Path, Unreachable: `The Docker daemon's ID "\"bad id\"" is not usable.`}}},
		},
	} {
		got := Host{Home: home, Daemons: tc.daemons}.Target(context.Background())
		if !reflect.DeepEqual(got, tc.want) {
			t.Errorf("%s: got %+v want %+v", name, got, tc.want)
		}
	}
}

func TestTargetOutputMatchesItsFixture(t *testing.T) {
	out, err := json.Marshal(TargetResult{Default: DaemonRemote, Daemons: []TargetDaemon{
		{Name: DaemonRemote, Endpoint: remoteEndpoint, Remote: true, Path: sshPath, Unreachable: "why"},
		{Name: DaemonLocal, Endpoint: localEndpoint, Path: "/usr/bin", ID: "L1"}}})
	if err != nil {
		t.Fatal(err)
	}
	want := `{"default":"remote","daemons":[{"name":"remote","endpoint":"ssh://u@pc","remote":true,"path":"` + sshPath +
		`","unreachable":"why"},{"name":"local","endpoint":"` + localEndpoint + `","remote":false,"path":"/usr/bin","id":"L1"}]}`
	if string(out) != want {
		t.Fatalf("%s", out)
	}
}

// gate lets its runners answer only once all n have been asked: one at a time, the first waits out its deadline.
type gate struct {
	n, count int32
	mu       sync.Mutex
	all      chan struct{}
}

func newGate(n int32) *gate { return &gate{n: n, all: make(chan struct{})} }

type gated struct {
	g  *gate
	id string
}

func (r gated) Run(context.Context, []string, []byte) ([]byte, error) {
	r.g.mu.Lock()
	if r.g.count++; r.g.count == r.g.n {
		close(r.g.all)
	}
	r.g.mu.Unlock()
	select {
	case <-r.g.all:
		return json.Marshal(r.id)
	case <-time.After(2 * time.Second):
		return nil, errors.New("asked one at a time")
	}
}

func TestTargetProbesInParallel(t *testing.T) {
	g := newGate(4)
	h := Host{Home: home, Daemons: []Daemon{
		{Name: DaemonRemote, Endpoint: "ssh://u@a", Remote: true, Runner: gated{g, "A"}},
		{Name: DaemonRemote, Endpoint: "ssh://u@b", Remote: true, Runner: gated{g, "B"}},
		{Name: DaemonRemote, Endpoint: "ssh://u@c", Remote: true, Runner: gated{g, "C"}},
		{Name: DaemonLocal, Endpoint: localEndpoint, Runner: gated{g, "L"}},
	}}
	got := h.Target(context.Background())
	var ids []string
	for _, d := range got.Daemons {
		ids = append(ids, d.Endpoint+"="+d.ID+d.Unreachable)
	}
	if want := []string{"ssh://u@a=A", "ssh://u@b=B", "ssh://u@c=C", localEndpoint + "=L"}; !slices.Equal(ids, want) || got.Default != DaemonRemote {
		t.Fatalf("got %q default %s", ids, got.Default)
	}
}

func TestTargetListsOneEntryPerDaemonID(t *testing.T) {
	remote := func(endpoint string, r docker.Runner) Daemon {
		return Daemon{Name: DaemonRemote, Endpoint: endpoint, Remote: true, Runner: r}
	}
	local := Daemon{Name: DaemonLocal, Endpoint: localEndpoint, Runner: identified("L1", nil)}
	for name, tc := range map[string]struct {
		daemons []Daemon
		want    []string
	}{
		"one machine at two addresses, a second machine, a down address": {
			[]Daemon{remote("ssh://u@lan", identified("R1", nil)), remote("ssh://u@down", identified("", unreachableErr)),
				remote("ssh://u@ts", identified("R1", nil)), remote("ssh://u@other", identified("R2", nil)), local},
			[]string{"ssh://u@lan=R1", "ssh://u@down=host key mismatch", "ssh://u@other=R2", localEndpoint + "=L1"},
		},
		"a remote address reaching the local daemon keeps local": {
			[]Daemon{remote("ssh://u@mac", identified("L1", nil)), local},
			[]string{"ssh://u@mac=L1", localEndpoint + "=L1"},
		},
	} {
		got := Host{Home: home, Daemons: tc.daemons}.Target(context.Background())
		var ids []string
		for _, d := range got.Daemons {
			ids = append(ids, d.Endpoint+"="+d.ID+d.Unreachable)
		}
		if !slices.Equal(ids, tc.want) {
			t.Errorf("%s: got %q", name, ids)
		}
	}
}
