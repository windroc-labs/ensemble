package broker

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"

	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/protocol"
)

func nopExec([]string, []byte) error { return nil }

func nopPublish(string) error { return nil }

// A failed renewal's detail reaches neither the reply nor the audit line; TestCodexWriteFailureCodeAndCause pins a
// failed writer's.
func TestCodexSentinelNeverLeaks(t *testing.T) {
	const sentinel = "CODEX_SENTINEL_d41d8c"
	c, n, _ := renewalFixture(t)
	n.refresh = func(string) error { return errors.New(sentinel) }
	_, err := c.Refresh(context.Background(), nopExec, nopPublish)
	var e *protocol.Error
	if !errors.As(err, &e) {
		t.Fatalf("%#v", err)
	}
	response, _ := json.Marshal(e) // what the broker's reply and the audit line carry
	if strings.Contains(err.Error(), sentinel) || bytes.Contains(response, []byte(sentinel)) {
		t.Fatalf("sentinel leaked: %s", response)
	}
}

func TestCodexErrorMapping(t *testing.T) {
	failed := protocol.New(protocol.RefreshFailed, "Writing Codex credentials into the container failed")
	opaque := errors.New("opaque")
	if codexError(nil) != nil {
		t.Fatal("nil mapped to an error")
	}
	if err := codexError(errors.Join(failed, errRevision)); err != failed {
		t.Fatalf("protocol error mapped to %#v", err)
	}
	if err := codexError(docker.ErrUnreachable); !errors.Is(err, docker.ErrUnreachable) || err.Error() != "Codex credential injection: "+docker.ErrUnreachable.Error() {
		t.Fatalf("unreachable daemon mapped to %v", err)
	}
	if err := codexError(opaque); err != opaque {
		t.Fatalf("opaque error mapped to %v", err)
	}
}
