package launcher

import (
	"context"
	"errors"
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
			d := &fakeDocker{respond: func([]string) ([]byte, error) { return []byte(tc.out), tc.err }}
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

// TestCloseReleasesCodexGrant: close withdraws the container's Codex grant after deleting it, reports a withdrawal
// failure after the deletion, and keeps the grant of a container it could not delete.
func TestCloseReleasesCodexGrant(t *testing.T) {
	var log []string
	removal := error(nil)
	d := &fakeDocker{respond: func(args []string) ([]byte, error) {
		log = append(log, args[0])
		return []byte(container + "\n"), removal
	}}
	creds := &fakeCodexCreds{log: &log}
	h := Host{Docker: d, GitHub: &fakeTokens{}, Codex: creds}
	got, err := h.Close(context.Background(), container)
	if err != nil || got != (CloseResult{Container: container, Removed: true}) || !slices.Equal(log, []string{"rm", "release " + container}) {
		t.Fatalf("%+v %v %q", got, err, log)
	}
	log, creds.releaseErr = nil, errors.New("keychain STORE_SENTINEL")
	_, err = h.Close(context.Background(), container)
	if e := protoErr(t, err); e.Code != protocol.KeychainFailed || e.Message != "Withdrawing the Codex grant of "+container+" failed." || !slices.Equal(log, []string{"rm", "release " + container}) {
		t.Fatalf("%+v %q", e, log)
	}
	log, removal = nil, &docker.CommandError{Exit: 1, Stderr: "removal of container in progress"}
	if _, err := h.Close(context.Background(), container); protoErr(t, err).Code != protocol.CloseFailed || !slices.Equal(log, []string{"rm"}) {
		t.Fatalf("%v %q", err, log)
	}
}
