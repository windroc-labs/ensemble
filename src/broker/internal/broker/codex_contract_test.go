package broker

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"slices"
	"strings"
	"testing"
	"time"

	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/protocol"
)

func nopExec([]string, []byte) error { return nil }

func nopPublish(string) error { return nil }

func TestCodexRefreshDeliversAndPublishes(t *testing.T) {
	c, _, _ := codexFixture(t)
	if err := c.Login(context.Background(), io.Discard); err != nil {
		t.Fatal(err)
	}
	var payload []byte
	var expiry string
	got, err := c.Refresh(context.Background(), func(_ []string, stdin []byte) error {
		payload = slices.Clone(stdin)
		return nil
	}, func(value string) error { expiry = value; return nil })
	want := codexNow.Add(time.Hour).Format(time.RFC3339)
	if err != nil || got != want || expiry != want {
		t.Fatalf("%s %s: %v", got, expiry, err)
	}
	var auth codexAuthDocument
	if json.Unmarshal(payload, &auth) != nil || auth.Mode != "chatgpt" || auth.Tokens.Refresh != "" || auth.Tokens.Access == "" {
		t.Fatalf("delivered %s", payload)
	}
}

func TestCodexSentinelNeverLeaks(t *testing.T) {
	const sentinel = "CODEX_SENTINEL_d41d8c"
	for _, failure := range []string{"renewal", "writer", "unknown writer"} {
		t.Run(failure, func(t *testing.T) {
			c, n, _ := renewalFixture(t)
			exec := nopExec
			switch failure {
			case "renewal":
				n.refresh = func(string) error { return errors.New(sentinel) }
			case "writer":
				exec = func([]string, []byte) error {
					return &docker.CommandError{Exit: 1, Stderr: "bootstrap: cannot write managed Codex credentials: " + sentinel + "\n"}
				}
			case "unknown writer":
				exec = func([]string, []byte) error { return &docker.CommandError{Exit: 1, Stderr: sentinel + "\n"} }
			}
			_, err := c.Refresh(context.Background(), exec, nopPublish)
			var e *protocol.Error
			if !errors.As(err, &e) {
				t.Fatalf("%#v", err)
			}
			response, _ := json.Marshal(e) // what the broker's reply and the audit line carry
			if strings.Contains(err.Error(), sentinel) || bytes.Contains(response, []byte(sentinel)) {
				t.Fatalf("sentinel leaked: %s", response)
			}
		})
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
