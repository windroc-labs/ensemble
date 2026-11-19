package launcher

import (
	"context"
	"ensemble/broker/internal/protocol"
	"errors"
	"strings"
	"testing"
)

// lockedManifest is worker_1's manifest with lock dummy, given secrets when not nil.
func lockedManifest(t *testing.T, secrets []any) string {
	return editedManifest(t, func(f map[string]any) {
		f["name"], f["workspace"], f["lock"], f["lock_owner"] = "worker_1", "ensemble-ws-worker_1", "dummy", "worker_1"
		f["claude_dir"] = ClientHomes + "/claude"
		if secrets != nil {
			withSecrets(secrets)(f)
		}
	})
}

// lockOnly is a daemon that answers only w's lock queries.
func lockOnly(w *lockWorld) *fakeDocker {
	return &fakeDocker{respond: func(args []string, _ []byte) ([]byte, error) {
		if out, err, handled := w.respond(args); handled {
			return out, err
		}
		return nil, errors.New("unexpected docker " + strings.Join(args, " "))
	}}
}

// A lock spans every configured daemon: a start checks the one it is placed on, then each other one.
func TestLockIteratesDaemons(t *testing.T) {
	t.Run("held on the other daemon", lockHeldOnOtherDaemon)
	t.Run("other daemon unreachable", lockOtherDaemonUnreachable)
}

func lockHeldOnOtherDaemon(t *testing.T) {
	for _, holderLocal := range []bool{true, false} {
		holding, starting := newLockWorld(), newLockWorld()
		holding.add("ensemble-holder-1", "running", map[string]string{lockLabel: "dummy", ownerLabel: "holder"})
		h, _, _ := starting.host(t, "worker_1")
		h.Env = &fakeEnv{}
		started := h.Daemons[0].Runner
		var secrets []any
		if holderLocal {
			h = onTwo(h, started, lockOnly(holding))
		} else {
			h = onTwo(h, lockOnly(holding), started)
			secrets = []any{secret(miniPassword, "mini-password")}
		}
		_, err := h.Start(context.Background(), lockedManifest(t, secrets))
		e := protoErr(t, err)
		peer := remoteEndpoint
		if holderLocal {
			peer = localEndpoint
		}
		if e.Code != protocol.Busy || e.Holder == nil || e.Holder.Container != "ensemble-holder-1" || e.Holder.Owner != "holder" ||
			!strings.Contains(e.Message, peer) {
			t.Fatalf("holder on the local daemon %v: %+v %+v", holderLocal, e, e.Holder)
		}
		starting.assertFinal(t, 0)
	}
}

func lockOtherDaemonUnreachable(t *testing.T) {
	w := &world{labels: goodLabels()}
	h, local := startHost(w, &fakeCreds{})
	base := local.respond
	local.respond = func(args []string, stdin []byte) ([]byte, error) {
		if args[0] == "ps" && strings.HasPrefix(flag(args, "--filter"), "label="+lockLabel+"=") {
			return nil, nil
		}
		return base(args, stdin)
	}
	h.Env = &fakeEnv{}
	remote := &fakeDocker{respond: func([]string, []byte) ([]byte, error) { return nil, unreachableErr }}
	h = onTwo(h, remote, local)
	h.Home = t.TempDir()
	_, err := h.Start(context.Background(), lockedManifest(t, []any{secret(miniPassword, "mini-password")}))
	e := protoErr(t, err)
	if e.Code != protocol.DockerUnreachable || !strings.Contains(e.Message, remoteEndpoint) {
		t.Fatalf("%+v", e)
	}
	stateRemoved := false
	for _, c := range local.calls {
		if len(c) > 1 && c[0] == "run" && c[1] == "--detach" {
			t.Fatalf("started with the lock unknown: %q", local.calls)
		}
		stateRemoved = stateRemoved || strings.Join(c[:min(2, len(c))], " ") == "volume rm" && c[len(c)-1] == "ensemble-state-worker_1"
	}
	if !stateRemoved {
		t.Fatalf("the state volume this start created was kept: %q", local.calls)
	}
}
