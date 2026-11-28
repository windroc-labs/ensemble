package launcher

import (
	"context"
	"errors"
	"testing"
	"time"

	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/protocol"
)

// stalled answers once its caller gives up, or after a while as unreachable.
type stalled struct {
	endpoint string
	after    time.Duration
	ended    chan error
}

func (s stalled) Run(ctx context.Context, _ []string, _ []byte) ([]byte, error) {
	select {
	case <-ctx.Done():
		s.ended <- ctx.Err()
		return nil, ctx.Err()
	case <-time.After(s.after):
		s.ended <- nil
		return nil, &docker.Unreachable{Endpoint: s.endpoint, Reason: "connection failed"}
	}
}

func remoteAt(endpoint string, r docker.Runner) Daemon {
	return Daemon{Name: DaemonRemote, Endpoint: endpoint, Remote: true, Runner: r}
}

func TestFindProbesRemotesInParallelAndReturnsTheFirstMatch(t *testing.T) {
	slow := stalled{endpoint: "ssh://u@lan", after: 5 * time.Second, ended: make(chan error, 1)}
	h := Host{Home: home, Daemons: []Daemon{remoteAt("ssh://u@lan", slow), remoteAt("ssh://u@ts", identified("R1", nil)),
		{Name: DaemonLocal, Endpoint: localEndpoint, Runner: identified("L1", nil)}}}
	d, err := h.Find(context.Background(), "R1", protocol.CloseFailed)
	if err != nil || d.Endpoint != "ssh://u@ts" || d.ID != "R1" {
		t.Fatalf("%+v %v", d, err)
	}
	// Asked one at a time, the stalled address would run out its wait before the match is found.
	if ended := <-slow.ended; !errors.Is(ended, context.Canceled) {
		t.Fatalf("find waited on the stalled address: %v", ended)
	}
}

func TestFindReportsTheFirstFailureWithoutAMatch(t *testing.T) {
	h := Host{Home: home, Daemons: []Daemon{
		remoteAt("ssh://u@lan", stalled{endpoint: "ssh://u@lan", after: 100 * time.Millisecond, ended: make(chan error, 1)}),
		remoteAt("ssh://u@ts", identified("", &docker.Unreachable{Endpoint: "ssh://u@ts", Reason: "authentication failed"})),
		{Name: DaemonLocal, Endpoint: localEndpoint, Runner: identified("L1", nil)}}}
	_, err := h.Find(context.Background(), "R1", protocol.CloseFailed)
	if e := protoErr(t, err); e.Code != protocol.DockerUnreachable || e.Message != "Cannot reach Docker target ssh://u@lan: connection failed." {
		t.Fatalf("%+v", e)
	}
}

func TestFindListsEachConfiguredDaemonOnce(t *testing.T) {
	h := Host{Home: home, Daemons: []Daemon{remoteAt("ssh://u@lan", identified("R1", nil)), remoteAt("ssh://u@ts", identified("R1", nil)),
		remoteAt("ssh://u@other", identified("R2", nil)), {Name: DaemonLocal, Endpoint: localEndpoint, Runner: identified("L1", nil)}}}
	_, err := h.Find(context.Background(), "GONE", protocol.CloseFailed)
	want := "Docker daemon GONE is not configured; the configured daemons: local " + localEndpoint +
		" (L1), remote ssh://u@lan (R1), remote ssh://u@other (R2)."
	if e := protoErr(t, err); e.Code != protocol.DockerUnreachable || e.Message != want {
		t.Fatalf("%+v", e)
	}
}
