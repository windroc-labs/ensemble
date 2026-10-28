package launcher

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"testing"

	"ensemble/broker/internal/credential"
	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/protocol"
)

func startCommands(t *testing.T, d *fakeDocker) []credential.Command {
	t.Helper()
	for i, args := range d.calls {
		if slices.Contains(args, credentialBatch) {
			var commands []credential.Command
			if err := json.Unmarshal(d.stdin[i], &commands); err != nil {
				t.Fatal(err)
			}
			return commands
		}
	}
	return nil
}

func TestStartExecBudget(t *testing.T) {
	for _, hook := range []bool{false, true} {
		tokens := &fakeTokens{owners: []string{"acme", "other", "third", "fourth"}}
		h, d := githubHost(&world{labels: goodLabels()}, tokens)
		h.Env = &fakeEnv{}
		path := fixtureManifest(t, "manifest-github-app.json", withSecrets(dummySecrets), func(m map[string]any) {
			if !hook {
				delete(m, "hook_path")
			}
		})
		if _, err := h.Start(context.Background(), path); err != nil {
			t.Fatal(err)
		}
		count := 0
		for _, args := range d.calls {
			if args[0] == "exec" {
				count++
			}
		}
		if count > 4 {
			t.Fatalf("hook=%v: Claude start with four GitHub owners and two secrets used %d execs; limit 4", hook, count)
		}
		if commands := startCommands(t, d); len(commands) != 7 {
			t.Fatalf("batched %d commands; want client + two secrets + four owners", len(commands))
		}
	}
}

func TestStartCredentialDelivery(t *testing.T) {
	for _, tc := range []struct {
		response string
		runErr   error
		message  string
		issued   int
	}{
		{response: `{"completed":0}`, message: "Injecting the Claude token failed."},
		{response: `{"completed":1}`, message: "Writing ensemble-broker-env/dummy into the container failed.", issued: 1},
		{response: `{"completed":2}`, message: "Writing ensemble-broker-env/ssh into the container failed.", issued: 2},
		{response: `{"completed":3}`, message: "Injecting the GitHub tokens failed.", issued: 3},
		{response: `{"completed":4}`, message: "Injecting the GitHub tokens failed.", issued: 3},
		{response: `{"completed":5}`, issued: 3},
		{response: ""},
		{response: "{}"},
		{response: "null"},
		{response: `{"completed":-1}`},
		{response: `{"completed":6}`},
		{response: `{"completed":"secret"}`},
		{runErr: errors.New("secret")},
		{runErr: docker.ErrUnreachable},
	} {
		t.Run(tc.response+fmt.Sprint(tc.runErr), func(t *testing.T) {
			tokens := &fakeTokens{owners: []string{"first", "second"}}
			h, _ := githubHost(&world{labels: goodLabels(), batchReply: &tc.response, execErr: failOn(credentialBatch, tc.runErr)}, tokens)
			h.Env = &fakeEnv{}
			rec, path := audited(t, &h, "start", "")
			_, err := h.Start(context.Background(), fixtureManifest(t, "manifest-github-app.json", withSecrets(dummySecrets)))
			rec.Flush(err)
			success := tc.response == `{"completed":5}` && tc.runErr == nil
			if (err == nil) != success {
				t.Fatal(err)
			}
			if !success {
				want := tc.message
				if want == "" {
					want = "Writing startup credentials failed."
				}
				wantCode := protocol.StartFailed
				if errors.Is(tc.runErr, docker.ErrUnreachable) {
					wantCode = protocol.DockerUnreachable
					want = "Cannot reach Docker Desktop; start it and retry."
				}
				e := protoErr(t, err)
				if e.Message != want || e.Code != wantCode || e.Cleanup != protocol.Removed || !slices.Contains(tokens.calls, "revoke "+container) {
					t.Fatalf("error=%+v tokens=%v", e, tokens.calls)
				}
			}
			credentials := readLines(t, path)[0].Credentials
			want := tc.issued
			if !success {
				want *= 2
			}
			if len(credentials) != want {
				t.Fatalf("credentials=%+v want %d", credentials, want)
			}
		})
	}
}

func TestStartPreparationDoesNotDeliverCredentials(t *testing.T) {
	h, d, _ := envHost(&world{labels: goodLabels()}, &fakeEnv{failOn: map[string]error{"ensemble-broker-env/ssh": errors.New("secret")}})
	_, err := h.Start(context.Background(), editedManifest(t, withSecrets(dummySecrets)))
	if err == nil || len(startCommands(t, d)) != 0 {
		t.Fatalf("preparation failure delivered credentials: %v", err)
	}
}

func TestStartPartialDeliveryAuditAndPersistence(t *testing.T) {
	for _, rollbackFails := range []bool{false, true} {
		t.Run(map[bool]string{false: "removed", true: "retained"}[rollbackFails], func(t *testing.T) {
			w := &world{labels: goodLabels(), batchReply: ptr(`{"completed":2}`)}
			if rollbackFails {
				w.rmErr = errors.New("container busy")
			}
			tokens := &fakeTokens{owners: []string{"first", "second"}}
			h, d := githubHost(w, tokens)
			h.Home, h.Env = t.TempDir(), &fakeEnv{}
			h.Static = &fakeStatic{loads: []map[string]credential.Static{{}, {}}}
			h.Schedule = StateSchedule{Home: h.Home, Safe: h.Safe}
			parent := filepath.Join(h.Home, ".ensemble/docker-state/worker_1")
			if err := os.MkdirAll(parent, 0o700); err != nil {
				t.Fatal(err)
			}
			path := fixtureManifest(t, "manifest-github-app.json", withSecrets(dummySecrets), func(m map[string]any) {
				m["claude_dir"] = filepath.Join(parent, "claude")
			})
			rec, log := audited(t, &h, "start", "")
			_, err := h.Start(context.Background(), path)
			rec.Flush(err)
			e := protoErr(t, err)
			if e.Message != "Writing ensemble-broker-env/ssh into the container failed." || e.Code != protocol.StartFailed {
				t.Fatalf("wrong failed step: %+v", e)
			}
			want := []string{"claude-setup-token issued", "env-secret issued ensemble-broker-env/dummy"}
			cleanup := protocol.Failed
			if !rollbackFails {
				want = append(want, "claude-setup-token withdrawn", "env-secret withdrawn ensemble-broker-env/dummy")
				cleanup = protocol.Removed
			}
			line := readLines(t, log)[0]
			if !slices.Equal(kinds(line), want) || e.Cleanup != cleanup || line.Credentials[1].Item != "ensemble-broker-env/dummy" {
				t.Fatalf("audit=%+v cleanup=%s", line.Credentials, e.Cleanup)
			}
			if !slices.Contains(tokens.calls, "revoke "+container) || !hasCall(d, "rm") {
				t.Fatalf("rollback or revocation missing: %v %v", tokens.calls, d.verbs())
			}
			for _, suffix := range []string{".static.json", ".tokens.json"} {
				if _, err := os.Stat(filepath.Join(parent, tokensGeneration+suffix)); !errors.Is(err, os.ErrNotExist) {
					t.Fatalf("failed delivery wrote %s: %v", suffix, err)
				}
			}
		})
	}
}
