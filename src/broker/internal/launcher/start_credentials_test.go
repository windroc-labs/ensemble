package launcher

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"ensemble/broker/internal/credential"
	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/protocol"
)

func startCommands(t *testing.T, d *fakeDocker) []startCommand {
	t.Helper()
	for i, args := range d.calls {
		if slices.Contains(args, credentialBatch) {
			var commands []startCommand
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

func TestStartDeliveryCopiesAndErases(t *testing.T) {
	var batch startDelivery
	argv, data := []string{"write", "owner"}, []byte("secret")
	if err := batch.queue(argv, data); err != nil {
		t.Fatal(err)
	}
	argv[0] = "changed"
	clear(data)
	if batch.commands[0].Argv[0] != "write" || string(batch.commands[0].Stdin) != "secret" {
		t.Fatal("provider can erase or reuse its buffers after queueing")
	}
	batch.erase()
	if slices.ContainsFunc(batch.commands[0].Stdin, func(b byte) bool { return b != 0 }) {
		t.Fatal("queued credential not erased")
	}
}

func TestStartDeliveryConfirmsOnlySuccessfulSteps(t *testing.T) {
	for _, completed := range []int{0, 1, 2, 3} {
		var batch startDelivery
		confirmed := []int{}
		for i := range 3 {
			_ = batch.queue([]string{"write"}, []byte("secret"))
			batch.step([]string{"client", "env", "github"}[i], func() { confirmed = append(confirmed, i) })
		}
		d := &fakeDocker{respond: func([]string) ([]byte, error) {
			return json.Marshal(map[string]int{"completed": completed})
		}}
		err := (Host{Docker: d}).deliverStart(context.Background(), container, &batch)
		if len(confirmed) != completed || (err == nil) != (completed == 3) {
			t.Fatalf("completed %d, confirmed %v, error %v", completed, confirmed, err)
		}
		if err != nil && protoErr(t, err).Message != []string{"client", "env", "github"}[completed] {
			t.Fatal(err)
		}
	}
}

func TestStartDeliveryRejectsUnknownCompletion(t *testing.T) {
	for _, response := range []string{"", "{}", "null", `{"completed":-1}`, `{"completed":2}`, `{"completed":"secret"}`} {
		var batch startDelivery
		_ = batch.queue([]string{"write"}, []byte("secret"))
		batch.step("client", func() { t.Fatal("unconfirmed delivery") })
		d := &fakeDocker{respond: func([]string) ([]byte, error) { return []byte(response), nil }}
		err := (Host{Docker: d}).deliverStart(context.Background(), container, &batch)
		if err == nil || strings.Contains(err.Error(), "secret") {
			t.Fatalf("response %q: %v", response, err)
		}
	}
	for _, failure := range []error{errors.New("secret"), docker.ErrUnreachable} {
		var batch startDelivery
		_ = batch.queue([]string{"write"}, []byte("secret"))
		d := &fakeDocker{respond: func([]string) ([]byte, error) { return nil, failure }}
		if err := (Host{Docker: d}).deliverStart(context.Background(), container, &batch); err == nil || strings.Contains(err.Error(), "secret") {
			t.Fatal(err)
		}
	}
}

func TestStartCredentialBatchFailureRevokesGitHub(t *testing.T) {
	tokens := &fakeTokens{owners: []string{"first", "second"}}
	h, _ := githubHost(&world{labels: goodLabels(), execErr: failOn("write", errors.New("secret"))}, tokens)
	_, err := h.Start(context.Background(), githubManifest(t))
	if err == nil || protoErr(t, err).Message != "Injecting the GitHub tokens failed." || !slices.Contains(tokens.calls, "revoke "+container) {
		t.Fatalf("%v, calls %v", err, tokens.calls)
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
			w := &world{labels: goodLabels(), execErr: failOn("id_ed25519", errors.New("secret write failed"))}
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
