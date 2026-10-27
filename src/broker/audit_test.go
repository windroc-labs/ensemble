package main

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"strings"
	"testing"

	"ensemble/broker/internal/audit"
	"ensemble/broker/internal/launcher"
	"ensemble/broker/internal/protocol"
)

type failedAudit struct{ calls int }

func (s *failedAudit) Append(...[]byte) error {
	s.calls++
	return errors.New("AUDIT_SECRET_SENTINEL disk full")
}

type successfulAudit struct{ calls int }

func (s *successfulAudit) Append(...[]byte) error {
	s.calls++
	return nil
}

func TestSuccessfulAuditDoesNotMaskRecoveryFailure(t *testing.T) {
	const container = "ensemble-worker_1-0123456789abcdef0123456789abcdef"
	sink := &successfulAudit{}
	rec := &audit.Recorder{Command: "start", Container: container, Log: sink}
	failure := protocol.New(protocol.KeychainFailed, "Recovery persistence failed.")
	failure.Container, failure.Cleanup, failure.CleanupError = container, protocol.Failed, "Container removal failed."
	failure.Cause = "safe persistence diagnostic"
	a := app{start: func(context.Context, keys, string, io.Writer) (any, error) {
		return withAudit(rec, func() (any, error) { return nil, failure })
	}}
	code, out, errOut := invoke(a, "start", "--manifest", "/manifest")
	var got struct {
		AuditWarning string `json:"audit_warning"`
		Error        protocol.Error
	}
	if err := json.Unmarshal([]byte(errOut), &got); err != nil || code != 11 || out != "" || got.Error != *failure || got.AuditWarning != "" || sink.calls != 1 {
		t.Fatalf("audit success masked recovery failure: %d %q %q, writes=%d, err=%v", code, out, errOut, sink.calls, err)
	}
}

func TestAuditWarningKeepsOperationOutcome(t *testing.T) {
	const container = "ensemble-worker_1-0123456789abcdef0123456789abcdef"
	for _, command := range []string{"start", "maintain", "close", "status"} {
		for _, failed := range []bool{false, true} {
			t.Run(command+"/"+map[bool]string{false: "success", true: "failure"}[failed], func(t *testing.T) {
				sink := &failedAudit{}
				rec := &audit.Recorder{Command: command, Container: container, Log: sink}
				var operationErr error
				if failed {
					e := protocol.New(protocol.KeychainFailed, "Recovery persistence failed.")
					if command == "start" {
						e.Cleanup, e.Container, e.CleanupError = protocol.Failed, container, "Container removal failed."
					}
					operationErr = e
				}
				calls := 0
				f := func() (any, error) {
					calls++
					if command == "maintain" {
						rec.Renewing("")
					}
					if command == "status" {
						rec.Add("ensemble-other-0123456789abcdef0123456789abcdef", audit.Credential{Kind: audit.GitHubToken, Result: audit.Revoked}, "")
					}
					return map[string]any{"container": container, "removed": true, "github_user_id": int64(9007199254740993)}, operationErr
				}
				step := func(context.Context, keys, string, io.Writer) (any, error) { return withAudit(rec, f) }
				a := app{start: step, close: step, maintain: func(ctx context.Context, _ keys, c, _ string, _ []string, _ bool) (any, error) {
					return step(ctx, keys{}, c, nil)
				}, status: func(ctx context.Context, _ keys, _ bool) (any, error) { return step(ctx, keys{}, "", nil) }}
				args := map[string][]string{"start": {"start", "--manifest", "/manifest"}, "maintain": {"maintain", "--container", container, "--manifest", "/manifest"}, "close": {"close", "--container", container}, "status": {"status", "--cleanup"}}[command]
				code, out, errOut := invoke(a, args...)
				wire := out
				if failed {
					wire = errOut
					if code != 11 || out != "" {
						t.Fatalf("operation error replaced: %d %s %s", code, out, errOut)
					}
					var got struct{ Error protocol.Error }
					if err := json.Unmarshal([]byte(wire), &got); err != nil || got.Error != *operationErr.(*protocol.Error) {
						t.Fatalf("operation details changed: %+v %v", got, err)
					}
				} else if code != 0 || errOut != "" || !strings.Contains(out, `"github_user_id":9007199254740993`) || !strings.Contains(out, container) {
					t.Fatalf("successful operation replaced: %d %s %s", code, out, errOut)
				}
				if calls != 1 || sink.calls != 1 || !strings.Contains(wire, `"audit_warning":"audit_write_failed"`) || strings.Contains(wire, "AUDIT_SECRET_SENTINEL") {
					t.Fatalf("unsafe warning or repeated operation: %s, %d/%d", wire, calls, sink.calls)
				}
			})
		}
	}
}

func TestNoAuditWarningWithoutWrite(t *testing.T) {
	for _, rec := range []*audit.Recorder{nil, {Command: "status", Log: &failedAudit{}}} {
		result, err := withAudit(rec, func() (any, error) { return launcher.StatusResult{Revocations: []protocol.Pending{}}, nil })
		wire, _ := json.Marshal(result)
		if err != nil || strings.Contains(string(wire), "audit_warning") {
			t.Fatalf("query produced an audit warning: %s %v", wire, err)
		}
	}
}
