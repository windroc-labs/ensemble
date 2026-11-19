package broker

import (
	"errors"
	"strings"
	"testing"

	"ensemble/broker/internal/docker"
)

// unreachableErr is what the docker CLI returns for a target it cannot reach.
var unreachableErr = &docker.Unreachable{Endpoint: "ssh://u@pc", Reason: "connection failed"}

func TestCredentialWritesKeepTheUnreachableTarget(t *testing.T) {
	failed := errors.Join(errors.New("secret stderr"), unreachableErr)
	s := &codexSession{carrier: []byte("x")}
	for name, err := range map[string]error{
		"github token": writeError(failed),
		"codex error":  codexError(failed),
		"codex write":  s.write(func([]string, []byte) error { return failed }),
	} {
		var u *docker.Unreachable
		if !errors.As(err, &u) || u != unreachableErr || strings.Contains(err.Error(), "secret stderr") {
			t.Fatalf("%s: %v", name, err)
		}
	}
}
