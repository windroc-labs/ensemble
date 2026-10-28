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
	"sync"
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
	commands, _, expiry, err := l.Issue(context.Background(), request, "container")
	if err != nil || len(commands) != 2 || expiry == "" {
		t.Fatalf("issue %v", err)
	}
	if requests[0].Get("scope") != a.scopes() {
		t.Fatal("scope changed")
	}
	var stored linearContainerRecord
	if l.read("container/container", &stored) != nil || stored.Current.AccessToken != "CONTAINER-TOKEN-1" {
		t.Fatal("container record missing from Keychain")
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
	exec := func([]string, []byte) error { return errors.New("CONTAINER-TOKEN ROOT-SENTINEL") }
	if _, err := l.Refresh(context.Background(), request, "container", exec); err == nil || strings.Contains(err.Error(), "CONTAINER-TOKEN") {
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
	if _, err := l.Refresh(context.Background(), request, "container", exec); err != nil {
		t.Fatal(err)
	}
	if requests[len(requests)-1].Get("token") != "CONTAINER-TOKEN-1" {
		t.Fatal("old token not revoked")
	}
	_ = l.Revoke(context.Background(), "container")
	if requests[len(requests)-1].Get("token") != "CONTAINER-TOKEN-3" {
		t.Fatal("close did not revoke current token")
	}
	rollbackCommands, rollback, _, err := l.Issue(context.Background(), request, "rollback")
	if err != nil {
		t.Fatal(err)
	}
	for _, command := range rollbackCommands {
		command.Erase()
	}
	rollback()
	if requests[len(requests)-1].Get("token") != "CONTAINER-TOKEN-4" {
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

func TestLinearStoppedContainerClose(t *testing.T) {
	l, k, _, calls := linearRig(t)
	a := linearApp("engineer")
	enrollLinear(t, l, k, a)
	commands, _, _, err := l.Issue(context.Background(), credential.LinearRequest{App: containerApp(a)}, "stopped")
	if err != nil {
		t.Fatal(err)
	}
	defer func() {
		for _, command := range commands {
			command.Erase()
		}
	}()
	if err := l.Revoke(context.Background(), "stopped"); err != nil {
		t.Fatal(err)
	}
	if len(*calls) != 2 || (*calls)[1].Get("token") != "TOKEN-1" {
		t.Fatal("stopped-container close did not revoke")
	}
}

func TestLinearConcurrentRenewAndClose(t *testing.T) {
	l, k, _, _ := linearRig(t)
	a := linearApp("engineer")
	enrollLinear(t, l, k, a)
	var mu sync.Mutex
	active := map[string]bool{}
	issued := 0
	l.http.Transport = linearRoundTrip(func(r *http.Request) (*http.Response, error) {
		data, _ := io.ReadAll(r.Body)
		values, _ := url.ParseQuery(string(data))
		mu.Lock()
		defer mu.Unlock()
		body := `{}`
		if strings.HasSuffix(r.URL.Path, "/token") {
			issued++
			token := fmt.Sprintf("TOKEN-%d", issued)
			active[token] = true
			body = fmt.Sprintf(`{"access_token":%q,"expires_in":2592000}`, token)
		} else {
			delete(active, values.Get("token"))
		}
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(body))}, nil
	})
	request := credential.LinearRequest{App: containerApp(a)}
	commands, _, _, err := l.Issue(context.Background(), request, "race")
	if err != nil {
		t.Fatal(err)
	}
	for _, command := range commands {
		command.Erase()
	}
	entered, release := make(chan struct{}), make(chan struct{})
	var once sync.Once
	exec := func([]string, []byte) error { once.Do(func() { close(entered); <-release }); return nil }
	first := make(chan error, 1)
	go func() { _, err := l.Refresh(context.Background(), request, "race", exec); first <- err }()
	<-entered
	second, closed := make(chan error, 1), make(chan error, 1)
	go func() { _, err := l.Refresh(context.Background(), request, "race", exec); second <- err }()
	go func() { closed <- l.Revoke(context.Background(), "race") }()
	close(release)
	if err := <-first; err != nil {
		t.Fatal(err)
	}
	<-second // May be refused because close acquired the lock first.
	if err := <-closed; err != nil {
		t.Fatal(err)
	}
	if _, err := l.Refresh(context.Background(), request, "race", exec); err == nil {
		t.Fatal("renewal after close allowed")
	}
	mu.Lock()
	defer mu.Unlock()
	if len(active) != 0 {
		t.Fatalf("%d token(s) remain active", len(active))
	}
}

func TestLinearRevocationRecordRetry(t *testing.T) {
	l, k, _, _ := linearRig(t)
	a := linearApp("engineer")
	enrollLinear(t, l, k, a)
	request := credential.LinearRequest{App: containerApp(a)}
	commands, _, _, err := l.Issue(context.Background(), request, "retry")
	if err != nil {
		t.Fatal(err)
	}
	for _, command := range commands {
		command.Erase()
	}
	transport := l.http.Transport
	l.http.Transport = linearRoundTrip(func(*http.Request) (*http.Response, error) { return nil, errors.New("TOKEN-1 ROOT-SENTINEL") })
	if l.Revoke(context.Background(), "retry") == nil {
		t.Fatal("revocation failure hidden")
	}
	var record linearContainerRecord
	if l.read("container/retry", &record) != nil || !record.Closed || len(record.Tokens) != 1 {
		t.Fatal("lost revocation reference")
	}
	if _, err := l.Refresh(context.Background(), request, "retry", func([]string, []byte) error { return nil }); err == nil {
		t.Fatal("closed record renewed")
	}
	l.http.Transport = transport
	if err := l.Revoke(context.Background(), "retry"); err != nil {
		t.Fatal(err)
	}
	if !errors.Is(l.read("container/retry", &record), ErrNotFound) {
		t.Fatal("revoked token retained")
	}
}

func TestLinearUndeclaredCloseDoesNotLock(t *testing.T) {
	l, _, _, _ := linearRig(t)
	l.lock = func(string) func() (func(), error) {
		t.Fatal("undeclared Linear close acquired a lock")
		return nil
	}
	if err := l.Revoke(context.Background(), "undeclared"); err != nil {
		t.Fatal(err)
	}
}
