package broker

import (
	"context"
	"encoding/json"
	"ensemble/broker/internal/audit"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"testing"

	"ensemble/broker/internal/credential"
)

func containerApp(a LinearIdentity) credential.LinearApp {
	return credential.LinearApp{Role: a.Name, ClientID: a.ClientID, UserID: a.UserID, SecretService: a.SecretService, TokenService: a.TokenService}
}

func TestLinearContainerIdentity(t *testing.T) {
	l, k, _, _ := linearRig(t)
	a := linearApp("engineer")
	if l.Ready(containerApp(a)) == nil {
		t.Fatal("unregistered role allowed")
	}
	enrollLinear(t, l, k, a)
	if err := l.Ready(containerApp(a)); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"harness", "manager"} {
		other := a
		other.Name = name
		other.Kind = name
		// The requested name cannot turn the enrolled role into another identity.
		if l.Ready(containerApp(other)) == nil {
			t.Fatal("non-role accepted")
		}
	}
	manager := linearApp("manager")
	manager.Kind = "manager"
	manager.UserID = "00000000-0000-4000-8000-000000000003"
	enrollLinear(t, l, k, manager)
	spoof := containerApp(manager)
	spoof.Role = "engineer"
	if l.Ready(spoof) == nil {
		t.Fatal("relabeled manager accepted")
	}
}

func TestLinearContainerLifecycle(t *testing.T) {
	l, k, h, _ := linearRig(t)
	a := linearApp("engineer")
	enrollLinear(t, l, k, a)
	if _, err := l.Maintain(context.Background(), a); err != nil {
		t.Fatal(err)
	}
	hostBefore := string(h.values[[2]string{a.TokenService, a.TokenService}])
	sink := &lines{}
	rec := &audit.Recorder{Command: "start", Container: "container", Log: sink, Now: l.now}
	l.Audit = rec
	issued := 0
	var requests []url.Values
	l.http.Transport = linearRoundTrip(func(r *http.Request) (*http.Response, error) {
		data, _ := io.ReadAll(r.Body)
		v, _ := url.ParseQuery(string(data))
		requests = append(requests, v)
		body := `{}`
		if strings.HasSuffix(r.URL.Path, "/token") {
			issued++
			body = fmt.Sprintf(`{"access_token":"CONTAINER-TOKEN-%d","expires_in":2592000}`, issued)
		}
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(body))}, nil
	})
	request := credential.LinearRequest{App: containerApp(a)}
	commands, rollback, expiry, err := l.Issue(context.Background(), request, "container")
	if err != nil || len(commands) != 2 || expiry == "" {
		t.Fatalf("issue %v", err)
	}
	if requests[0].Get("scope") != a.scopes() {
		t.Fatal("scope changed")
	}
	var token linearAccess
	if json.Unmarshal(commands[0].Stdin, &token) != nil || token.AccessToken != "CONTAINER-TOKEN-1" {
		t.Fatal("bad token file")
	}
	for _, command := range commands {
		if strings.Contains(strings.Join(command.Argv, " "), "CONTAINER-TOKEN") {
			t.Fatal("token in argv")
		}
	}
	read := func(context.Context, []string) ([]byte, error) { return json.Marshal(token) }
	exec := func([]string, []byte) error { return errors.New("CONTAINER-TOKEN ROOT-SENTINEL") }
	if _, err := l.Refresh(context.Background(), request, "container", exec, read); err == nil || strings.Contains(err.Error(), "CONTAINER-TOKEN") {
		t.Fatal("unsafe refresh error")
	}
	if requests[len(requests)-1].Get("token") != "CONTAINER-TOKEN-2" {
		t.Fatal("failed refresh did not revoke new token")
	}
	exec = func(argv []string, data []byte) error {
		if requests[len(requests)-1].Get("token") != "" {
			t.Fatal("old token revoked before delivery")
		}
		if !strings.Contains(strings.Join(argv, " "), "mv -f") {
			t.Fatal("replacement not atomic")
		}
		return json.Unmarshal(data, &token)
	}
	if _, err := l.Refresh(context.Background(), request, "container", exec, read); err != nil {
		t.Fatal(err)
	}
	if requests[len(requests)-1].Get("token") != "CONTAINER-TOKEN-1" {
		t.Fatal("old token not revoked")
	}
	l.Revoke(context.Background(), "container", read)
	if requests[len(requests)-1].Get("token") != "CONTAINER-TOKEN-3" {
		t.Fatal("close did not revoke current token")
	}
	rollback()
	if requests[len(requests)-1].Get("token") != "CONTAINER-TOKEN-1" {
		t.Fatal("rollback did not revoke issued token")
	}
	if err := rec.Flush(nil); err != nil {
		t.Fatal(err)
	}
	for _, line := range sink.got {
		if strings.Contains(string(line), "CONTAINER-TOKEN") || strings.Contains(string(line), "ROOT-SENTINEL") {
			t.Fatal("secret in audit")
		}
	}
	if string(h.values[[2]string{a.TokenService, a.TokenService}]) != hostBefore {
		t.Fatal("host token changed")
	}
	for _, v := range k.items {
		if strings.Contains(string(v), "CONTAINER-TOKEN") {
			t.Fatal("container token persisted")
		}
	}
	for _, command := range commands {
		command.Erase()
	}
}

func TestLinearContainerErrorsHideSecrets(t *testing.T) {
	l, k, _, _ := linearRig(t)
	a := linearApp("engineer")
	enrollLinear(t, l, k, a)
	l.http.Transport = linearRoundTrip(func(*http.Request) (*http.Response, error) { return nil, errors.New("ROOT-SENTINEL") })
	_, _, _, err := l.Issue(context.Background(), credential.LinearRequest{App: containerApp(a)}, "container")
	if err == nil || strings.Contains(err.Error(), "ROOT-SENTINEL") {
		t.Fatalf("unsafe error %v", err)
	}
}
