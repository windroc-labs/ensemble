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
