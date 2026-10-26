package broker

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"slices"
	"strings"
	"testing"
	"time"

	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/github"
	"ensemble/broker/internal/launcher"
	"ensemble/broker/internal/protocol"
)

const container = "ensemble-worker_1-0123456789abcdef0123456789abcdef"

type closeDocker func([]string) ([]byte, error)

func (f closeDocker) Run(_ context.Context, args []string, _ []byte) ([]byte, error) { return f(args) }

// closeSecrets retains only fake persisted bytes across Host/GitHub reconstruction.
type closeSecrets struct {
	items map[string][]byte
	fail  string
}

func (s *closeSecrets) Secret(service, account string) ([]byte, error) {
	if service == "ensemble-revocations" && s.fail == "read" {
		return nil, errors.New("fake backlog read failure")
	}
	if b, ok := s.items[service+"/"+account]; ok {
		return slices.Clone(b), nil
	}
	return nil, ErrNotFound
}

func (s *closeSecrets) PutSecret(service, account string, b []byte) error {
	if service == "ensemble-revocations" && s.fail == "write" {
		return errors.New("fake backlog write failure")
	}
	s.items[service+"/"+account] = slices.Clone(b)
	return nil
}

func (s *closeSecrets) DeleteSecret(service, account string) error {
	delete(s.items, service+"/"+account)
	return nil
}

type closeTransport func(*http.Request) (*http.Response, error)

func (f closeTransport) RoundTrip(req *http.Request) (*http.Response, error) { return f(req) }

func TestCloseRetainsGitHubRecovery(t *testing.T) {
	for _, kind := range []string{"read", "write"} {
		t.Run(kind, func(t *testing.T) {
			const other = "ensemble-other-0123456789abcdef0123456789abcdef"
			const record = `{"app":"ensemble-bot","tokens":[{"account":"acme","installation":1,"token":"ghs_close_fixture","expires_at":"2026-10-25T13:00:00Z"}]}`
			const peer = `{"app":"ensemble-bot","tokens":[{"account":"acme","installation":1,"token":"ghs_peer_fixture","expires_at":"2026-10-25T13:00:00Z"}]}`
			store := &closeSecrets{items: map[string][]byte{
				"ensemble-tokens/" + container: []byte(record),
				"ensemble-tokens/" + other:     []byte(peer),
			}, fail: kind}
			var revoked []string
			remoteFailed := true
			transport := closeTransport(func(req *http.Request) (*http.Response, error) {
				if req.Method != http.MethodDelete || req.URL.Path != "/installation/token" {
					t.Error("unexpected fake GitHub request")
				}
				token := strings.TrimPrefix(req.Header.Get("Authorization"), "Bearer ")
				if token != "ghs_close_fixture" {
					t.Error("close tried to revoke another consumer's grant")
				}
				status := http.StatusServiceUnavailable
				if !remoteFailed {
					status = http.StatusNoContent
					revoked = append(revoked, token)
				}
				return &http.Response{StatusCode: status, Body: io.NopCloser(strings.NewReader("")), Header: make(http.Header)}, nil
			})
			present, removals := true, 0
			d := closeDocker(func(args []string) ([]byte, error) {
				if !slices.Equal(args, []string{"rm", "--force", container}) {
					t.Error("close ran an unexpected Docker command")
				}
				removals++
				if !present {
					return nil, &docker.CommandError{Exit: 1, Stderr: "No such container: " + container}
				}
				present = false
				return []byte(container + "\n"), nil
			})
			newHost := func() launcher.Host {
				return launcher.Host{Docker: d, GitHub: GitHub{
					Keychain: store,
					Lock:     func() (func(), error) { return func() {}, nil },
					API: github.Client{Base: "https://fake.invalid", HTTP: &http.Client{Transport: transport},
						Now: func() time.Time { return time.Date(2026, 10, 25, 12, 0, 0, 0, time.UTC) }},
				}}
			}
			_, err := newHost().Close(context.Background(), container)
			wantCode(t, err, protocol.KeychainFailed)
			if present || removals != 1 {
				t.Fatal("revocation failure prevented container removal or was not reported")
			}
			// Repeat with a fresh Host while the double failure still holds and Docker is absent.
			_, err = newHost().Close(context.Background(), container)
			wantCode(t, err, protocol.KeychainFailed)
			if string(store.items["ensemble-tokens/"+container]) != record {
				t.Fatal("repeated absent close lost its persistent recovery material")
			}
			store.fail, remoteFailed = "", false
			got, err := newHost().Close(context.Background(), container)
			if err != nil || got != (launcher.CloseResult{Container: container, Removed: false}) || len(revoked) != 1 {
				t.Fatalf("reconstructed absent close did not recover: %+v, %v, revoked=%d", got, err, len(revoked))
			}
			if _, found := store.items["ensemble-tokens/"+container]; found {
				t.Fatal("successful recovery retained the retired record")
			}
			if got, err := newHost().Close(context.Background(), container); err != nil || got.Removed || len(revoked) != 1 {
				t.Fatal("repeated completed close was not idempotent")
			}
			if string(store.items["ensemble-tokens/"+other]) != peer {
				t.Fatal("close changed another consumer's recovery record")
			}
			data, err := json.Marshal(got)
			if err != nil || strings.Contains(string(data), "ghs_") {
				t.Fatal("close response leaked credential material")
			}
		})
	}
}
