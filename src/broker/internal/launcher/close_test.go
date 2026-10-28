package launcher

import (
	"context"
	"slices"
	"testing"

	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/protocol"
)

func TestClose(t *testing.T) {
	for name, tc := range map[string]struct {
		out     string
		err     error
		removed bool
		code    protocol.Code
	}{
		"removed":       {container + "\n", nil, true, ""},
		"absent":        {"", &docker.CommandError{Exit: 1, Stderr: "Error response from daemon: No such container: " + container}, false, ""},
		"absent exit 0": {"", nil, false, ""},
		"unreachable":   {"", docker.ErrUnreachable, false, protocol.DockerUnreachable},
		"failed":        {"", &docker.CommandError{Exit: 1, Stderr: "removal of container in progress"}, false, protocol.CloseFailed},
	} {
		t.Run(name, func(t *testing.T) {
			d := &fakeDocker{respond: func([]string, []byte) ([]byte, error) { return []byte(tc.out), tc.err }}
			got, err := Host{Docker: d, GitHub: &fakeTokens{}}.Close(context.Background(), container)
			if len(d.calls) != 1 || !slices.Equal(d.calls[0], []string{"rm", "--force", container}) {
				t.Fatalf("%q", d.calls)
			}
			if tc.code != "" {
				if protoErr(t, err).Code != tc.code {
					t.Fatal(err)
				}
				return
			}
			if err != nil || got != (CloseResult{Container: container, Removed: tc.removed}) {
				t.Fatalf("%+v %v", got, err)
			}
		})
	}
}

// TestCloseMakesNoCodexCall: close deletes a Codex container without any Codex call, whatever the deletion did.
func TestCloseMakesNoCodexCall(t *testing.T) {
	for _, removal := range []error{nil, &docker.CommandError{Exit: 1, Stderr: "removal of container in progress"}} {
		var log []string
		d := &fakeDocker{respond: func(args []string, stdin []byte) ([]byte, error) {
			log = append(log, args[0])
			return []byte(container + "\n"), removal
		}}
		h := Host{Docker: d, GitHub: &fakeTokens{}, Codex: &fakeCodexCreds{log: &log}}
		got, err := h.Close(context.Background(), container)
		if !slices.Equal(log, []string{"rm"}) {
			t.Fatalf("calls %q", log)
		}
		if removal == nil && (err != nil || got != (CloseResult{Container: container, Removed: true})) {
			t.Fatalf("%+v %v", got, err)
		}
	}
}

// AC6: close reads the token files as agent through docker exec on the context Revoke gives it, then removes the
// container.
func TestCloseRevokesBeforeRemove(t *testing.T) {
	d := &fakeDocker{respond: func(args []string, stdin []byte) ([]byte, error) {
		if args[0] == "exec" {
			return []byte("acme 1 ghs_x\n"), nil
		}
		return []byte(container + "\n"), nil
	}}
	tokens := &fakeTokens{readFiles: true}
	got, err := Host{Docker: d, GitHub: tokens}.Close(context.Background(), container)
	if err != nil || got != (CloseResult{Container: container, Removed: true}) {
		t.Fatalf("%+v %v", got, err)
	}
	want := [][]string{
		{"exec", "--user", "agent", container, "/bin/sh", "-c", "tokens-script", "sh", "/run/ensemble/secrets/github"},
		{"rm", "--force", container},
	}
	if !slices.EqualFunc(d.calls, want, slices.Equal) || !slices.Equal(tokens.calls, []string{"read-revoke " + container}) {
		t.Fatalf("docker %q tokens %q", d.calls, tokens.calls)
	}
	if string(tokens.readOut) != "acme 1 ghs_x\n" || tokens.readErr != nil {
		t.Fatalf("read %q %v", tokens.readOut, tokens.readErr)
	}
}

// AC6: an absent container's read says so (nothing to revoke), and close succeeds as before.
func TestCloseAbsentContainer(t *testing.T) {
	absent := &docker.CommandError{Exit: 1, Stderr: "Error response from daemon: No such container: " + container}
	d := &fakeDocker{respond: func([]string, []byte) ([]byte, error) { return nil, absent }}
	tokens := &fakeTokens{readFiles: true}
	got, err := Host{Docker: d, GitHub: tokens}.Close(context.Background(), container)
	if err != nil || got != (CloseResult{Container: container, Removed: false}) || !docker.Absent(tokens.readErr) {
		t.Fatalf("%+v %v read %v", got, err, tokens.readErr)
	}
}

// AC6: a read that fails never keeps the container: close removes it and succeeds.
func TestCloseRemovesWhenReadFails(t *testing.T) {
	for _, readErr := range []error{&docker.CommandError{Exit: 1, Stderr: "container is not running"}, context.DeadlineExceeded} {
		d := &fakeDocker{respond: func(args []string, stdin []byte) ([]byte, error) {
			if args[0] == "exec" {
				return nil, readErr
			}
			return []byte(container + "\n"), nil
		}}
		tokens := &fakeTokens{readFiles: true}
		got, err := Host{Docker: d, GitHub: tokens}.Close(context.Background(), container)
		if err != nil || got != (CloseResult{Container: container, Removed: true}) || tokens.readErr == nil || !hasCall(d, "rm") {
			t.Fatalf("%v: %+v %v", readErr, got, err)
		}
	}
}
