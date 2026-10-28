package launcher

import (
	"context"
	"encoding/json"
	"errors"
	"slices"
	"strings"
	"testing"

	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/protocol"
)

const lockID = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"

func lockInspect(id, name, state string, labels map[string]string) []byte {
	data, _ := json.Marshal([]any{map[string]any{
		"Id": id, "Name": "/" + name, "State": map[string]string{"Status": state},
		"Config": map[string]any{"Labels": labels},
	}})
	return data
}

func TestLockQuery(t *testing.T) {
	for _, state := range []string{"running", "paused", "restarting", "created", "exited", "dead", "removing"} {
		t.Run(state, func(t *testing.T) {
			d := &fakeDocker{respond: func(args []string, stdin []byte) ([]byte, error) {
				if args[0] == "ps" {
					if !slices.Contains(args, "label=windroc.lock=dummy") || !slices.Contains(args, "--all") {
						t.Fatalf("query flags: %q", args)
					}
					return []byte(lockID + "\n"), nil
				}
				if args[0] == "container" && args[1] == "inspect" {
					return lockInspect(lockID, container, state, map[string]string{"windroc.lock": "dummy", "windroc.lock.owner": "worker_1"}), nil
				}
				t.Fatalf("query mutated Docker: %q", args)
				return nil, nil
			}}
			got, err := (Host{Docker: d}).LockQuery(context.Background(), "dummy")
			if err != nil {
				t.Fatal(err)
			}
			active := state == "running" || state == "paused" || state == "restarting"
			if (got.Holder != nil) != active {
				t.Fatalf("state %s: %+v", state, got)
			}
			if active && (got.Holder.Container != container || got.Holder.Owner != "worker_1") {
				t.Fatalf("holder %+v", got.Holder)
			}
		})
	}
}

func TestLockQueryEmptyAndFailures(t *testing.T) {
	for name, tc := range map[string]struct {
		list    []byte
		inspect []byte
		err     error
		want    protocol.Code
	}{
		"empty":               {},
		"disappeared":         {list: []byte(lockID), err: &docker.CommandError{Exit: 1, Stderr: "No such container"}},
		"inspect failed":      {list: []byte(lockID), err: errors.New("bad inspect"), want: protocol.StartFailed},
		"inspect unreachable": {list: []byte(lockID), err: docker.ErrUnreachable, want: protocol.DockerUnreachable},
		"malformed":           {list: []byte(lockID), inspect: []byte(`[]`), want: protocol.StartFailed},
		"invalid ID":          {list: []byte("--help"), want: protocol.StartFailed},
		"different lock":      {list: []byte(lockID), inspect: lockInspect(lockID, container, "running", map[string]string{"windroc.lock": "other"})},
	} {
		t.Run(name, func(t *testing.T) {
			d := &fakeDocker{respond: func(args []string, stdin []byte) ([]byte, error) {
				if args[0] == "ps" {
					return tc.list, nil
				}
				return tc.inspect, tc.err
			}}
			got, err := (Host{Docker: d}).LockQuery(context.Background(), "dummy")
			if code(t, err) != tc.want || got.Holder != nil {
				t.Fatalf("%+v %v", got, err)
			}
		})
	}
	d := &fakeDocker{respond: func([]string, []byte) ([]byte, error) { return nil, docker.ErrUnreachable }}
	if _, err := (Host{Docker: d}).LockQuery(context.Background(), "dummy"); code(t, err) != protocol.DockerUnreachable {
		t.Fatal(err)
	}
	d = &fakeDocker{respond: func([]string, []byte) ([]byte, error) { t.Fatal("invalid name reached Docker"); return nil, nil }}
	if _, err := (Host{Docker: d}).LockQuery(context.Background(), "A B"); code(t, err) != protocol.ManifestRejected {
		t.Fatal(err)
	}
}

func TestBusyEnvelope(t *testing.T) {
	e := busy(&protocol.Holder{Container: container, Owner: "worker_1"})
	if protocol.Exit(e.Code) != 75 || !strings.Contains(e.Message, container) || !strings.Contains(e.Message, "worker_1") {
		t.Fatalf("%+v", e)
	}
	var out struct {
		Error struct{ Holder protocol.Holder }
	}
	if err := json.Unmarshal(protocol.Envelope("start", e), &out); err != nil || out.Error.Holder.Owner != "worker_1" {
		t.Fatalf("%+v %v", out, err)
	}
}
