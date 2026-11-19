package launcher

import (
	"context"
	"encoding/json"
	"reflect"
	"slices"
	"testing"

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
