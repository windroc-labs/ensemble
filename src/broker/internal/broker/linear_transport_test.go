package broker

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	"ensemble/broker/internal/credential"
)

func transportApp(t *testing.T, a LinearIdentity) credential.LinearApp {
	t.Helper()
	data, _ := json.Marshal(map[string]string{"role": a.Name, "client_id": a.ClientID, "user_id": a.UserID, "secret_service": a.SecretService, "token_service": a.TokenService, "renewal": "transport"})
	var app credential.LinearApp
	if err := json.Unmarshal(data, &app); err != nil {
		t.Fatal(err)
	}
	return app
}

func TestLinearTransportContainerWithoutEnrollment(t *testing.T) {
	l, k, h, _ := linearRig(t)
	a := linearApp("engineer")
	app := transportApp(t, a)
	expiry := l.now().Add(24 * time.Hour).Format(time.RFC3339)
	setToken := func(value string) {
		data, _ := json.Marshal(linearAccess{AccessToken: value, ExpiresAt: expiry})
		h.values[[2]string{a.TokenService, a.TokenService}] = data
	}
	setToken("HOST-TOKEN")
	l.http.Transport = linearRoundTrip(func(r *http.Request) (*http.Response, error) {
		if r.URL.String() != "https://api.linear.app/graphql" || !strings.HasPrefix(r.Header.Get("Authorization"), "Bearer HOST-TOKEN") {
			t.Fatal("transport attempted issuance, revocation or a non-role token")
		}
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(`{"data":{"viewer":{"id":"` + a.UserID + `"}}}`))}, nil
	})
	if err := l.Ready(app); err != nil {
		t.Fatalf("existing transport role requires enrollment: %v", err)
	}
	request := credential.LinearRequest{App: app}
	commands, rollback, gotExpiry, err := l.Issue(context.Background(), request, "transport")
	if err != nil || len(commands) != 2 || gotExpiry != expiry || !strings.Contains(string(commands[0].Stdin), "HOST-TOKEN") {
		t.Fatalf("transport delivery: %v %q", err, gotExpiry)
	}
	for _, command := range commands {
		command.Erase()
	}
	for _, value := range k.items {
		if strings.Contains(string(value), "HOST-TOKEN") {
			t.Fatal("shared host token persisted in per-container records")
		}
	}
	exec := func(_ []string, data []byte) error {
		if !strings.Contains(string(data), "HOST-TOKEN-UPDATED") {
			t.Fatal("maintenance did not reread the host token")
		}
		return nil
	}
	setToken("HOST-TOKEN-UPDATED")
	if got, err := l.Refresh(context.Background(), request, "transport", exec); err != nil || got != expiry {
		t.Fatalf("transport refresh: %v %q", err, got)
	}
	rollback()
	if err := l.Revoke(context.Background(), "transport"); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(h.value(t, a)), "HOST-TOKEN-UPDATED") || h.writes != 0 {
		t.Fatal("worker lifecycle changed the transport owner's token")
	}
	if _, err := l.Refresh(context.Background(), request, "transport", exec); err == nil {
		t.Fatal("closed worker received a token")
	}
}

func TestLinearTransportRejectsInvalidAccess(t *testing.T) {
	for _, scenario := range []string{"missing", "malformed", "expired", "undated", "identity", "network"} {
		t.Run(scenario, func(t *testing.T) {
			l, _, h, _ := linearRig(t)
			a := linearApp("engineer")
			token := linearAccess{AccessToken: "HOST-TOKEN", ExpiresAt: l.now().Add(time.Hour).Format(time.RFC3339)}
			if scenario == "expired" {
				token.ExpiresAt = l.now().Add(-time.Hour).Format(time.RFC3339)
			}
			if scenario == "undated" {
				token.ExpiresAt = ""
			}
			data, _ := json.Marshal(token)
			if scenario == "malformed" {
				data = []byte("{")
			}
			if scenario != "missing" {
				h.values[[2]string{a.TokenService, a.TokenService}] = data
			}
			l.http.Transport = linearRoundTrip(func(*http.Request) (*http.Response, error) {
				status := 200
				if scenario == "network" {
					status = 503
				}
				return &http.Response{StatusCode: status, Body: io.NopCloser(strings.NewReader(`{"data":{"viewer":{"id":"different-user"}}}`))}, nil
			})
			if err := l.Ready(transportApp(t, a)); err == nil {
				t.Fatal("invalid host access accepted")
			}
		})
	}
}
