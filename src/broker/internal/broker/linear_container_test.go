package broker

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"ensemble/broker/internal/audit"
	"ensemble/broker/internal/credential"
)

func containerApp(a LinearIdentity) credential.LinearApp {
	return credential.LinearApp{Role: a.Name, ClientID: a.ClientID, UserID: a.UserID, SecretService: a.SecretService, TokenService: a.TokenService}
}

func TestLinearContainerLockCleanup(t *testing.T) {
	for _, action := range []string{"close", "rollback", "retry", "cleanup-retry"} {
		t.Run(action, func(t *testing.T) {
			l, k, _, _ := linearRig(t)
			home := t.TempDir()
			locks := NewLinear(home, SystemKeychain{}, nil)
			l.lock = locks.lock
			l.removeLock = locks.removeLock
			a := linearApp("engineer")
			enrollLinear(t, l, k, a)
			commands, rollback, _, err := l.Issue(context.Background(), credential.LinearRequest{App: containerApp(a), Scopes: a.scopes()}, "cleanup")
			if err != nil {
				t.Fatal(err)
			}
			for _, command := range commands {
				command.Erase()
			}
			path := filepath.Join(home, ".ensemble", "linear", fmt.Sprintf("%x.lock", sha256.Sum256([]byte("container/cleanup"))))
			if _, err := os.Stat(path); err != nil {
				t.Fatal(err)
			}
			if action == "retry" {
				transport := l.http.Transport
				l.http.Transport = linearRoundTrip(func(*http.Request) (*http.Response, error) { return nil, errors.New("offline") })
				if err := l.Revoke(context.Background(), "cleanup"); err == nil {
					t.Fatal("failed revocation hidden")
				}
				if _, err := os.Stat(path); err != nil {
					t.Fatal("failed revocation lost its retry lock")
				}
				l.http.Transport = transport
			}
			if action == "cleanup-retry" {
				l.removeLock = func(string) error { return os.ErrPermission }
				if err := l.Revoke(context.Background(), "cleanup"); err == nil {
					t.Fatal("lock cleanup failure hidden")
				}
				l.removeLock = locks.removeLock
			}
			if action == "rollback" {
				rollback()
			} else if err := l.Revoke(context.Background(), "cleanup"); err != nil {
				t.Fatal(err)
			}
			if _, err := os.Stat(path); !os.IsNotExist(err) {
				t.Fatal("revoked container retained its lock")
			}
			if _, err := l.Refresh(context.Background(), credential.LinearRequest{App: containerApp(a), Scopes: a.scopes()}, "cleanup", func([]string, []byte) error {
				t.Fatal("closed container received a token")
				return nil
			}); err == nil {
				t.Fatal("closed container refreshed")
			}
			if err := l.Revoke(context.Background(), "cleanup"); err != nil {
				t.Fatal(err)
			}
			if _, err := os.Stat(path); !os.IsNotExist(err) {
				t.Fatal("maintenance or repeated close recreated the lock")
			}
		})
	}
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
	request := credential.LinearRequest{App: containerApp(a), Scopes: a.scopes()}
	commands, _, expiry, err := l.Issue(context.Background(), request, "container")
	if err != nil || len(commands) != 2 || expiry == "" {
		t.Fatalf("issue %v", err)
	}
	if requests[0].Get("scope") != a.scopes() {
		t.Fatal("scope changed")
	}
	var stored linearContainerRecord
	if l.read("container/container", &stored) != nil || len(stored.Tokens) != 1 || stored.Tokens[0].AccessToken != "CONTAINER-TOKEN-1" {
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

func TestLinearContainerReadScope(t *testing.T) {
	l, k, _, calls := linearRig(t)
	a := linearApp("engineer")
	enrollLinear(t, l, k, a)
	data, _ := json.Marshal(map[string]any{"app": containerApp(a), "scopes": "read"})
	var request credential.LinearRequest
	if err := json.Unmarshal(data, &request); err != nil {
		t.Fatal(err)
	}
	commands, _, _, err := l.Issue(context.Background(), request, "reader")
	if err != nil {
		t.Fatal(err)
	}
	for _, c := range commands {
		c.Erase()
	}
	if _, err := l.Refresh(context.Background(), request, "reader", func([]string, []byte) error { return nil }); err != nil {
		t.Fatal(err)
	}
	issued := 0
	for _, form := range *calls {
		if form.Get("grant_type") == "" {
			continue
		}
		issued++
		if form.Get("scope") != "read" {
			t.Fatalf("scope = %q", form.Get("scope"))
		}
	}
	if issued != 2 {
		t.Fatalf("issuances = %d", issued)
	}
}

func TestLinearContainerErrorsHideSecrets(t *testing.T) {
	l, k, _, _ := linearRig(t)
	a := linearApp("engineer")
	enrollLinear(t, l, k, a)
	l.http.Transport = linearRoundTrip(func(*http.Request) (*http.Response, error) { return nil, errors.New("ROOT-SENTINEL") })
	_, _, _, err := l.Issue(context.Background(), credential.LinearRequest{App: containerApp(a), Scopes: a.scopes()}, "container")
	if err == nil || strings.Contains(err.Error(), "ROOT-SENTINEL") {
		t.Fatalf("unsafe error %v", err)
	}
}

func TestLinearStoppedContainerClose(t *testing.T) {
	l, k, _, calls := linearRig(t)
	a := linearApp("engineer")
	enrollLinear(t, l, k, a)
	commands, _, _, err := l.Issue(context.Background(), credential.LinearRequest{App: containerApp(a), Scopes: a.scopes()}, "stopped")
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
	request := credential.LinearRequest{App: containerApp(a), Scopes: a.scopes()}
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
	request := credential.LinearRequest{App: containerApp(a), Scopes: a.scopes()}
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
	base := l.lock
	l.lock = func(id string, create bool) func() (func(), error) {
		if create {
			t.Fatal("undeclared Linear close creates a lock")
		}
		return func() (func(), error) {
			unlock, err := base(id, create)()
			if !os.IsNotExist(err) {
				t.Fatal("undeclared Linear close took a lock")
			}
			return unlock, err
		}
	}
	if err := l.Revoke(context.Background(), "undeclared"); err != nil {
		t.Fatal(err)
	}
}

func TestLinearCloseWaitsForIssuance(t *testing.T) {
	l, k, _, _ := linearRig(t)
	a := linearApp("engineer")
	enrollLinear(t, l, k, a)
	entered, release, waiting := make(chan struct{}), make(chan struct{}), make(chan struct{})
	var releaseOnce sync.Once
	defer releaseOnce.Do(func() { close(release) })
	var revoked string
	l.http.Transport = linearRoundTrip(func(r *http.Request) (*http.Response, error) {
		body := `{}`
		if strings.HasSuffix(r.URL.Path, "/token") {
			close(entered)
			<-release
			body = `{"access_token":"ISSUING-TOKEN","expires_in":2592000}`
		} else {
			data, _ := io.ReadAll(r.Body)
			values, _ := url.ParseQuery(string(data))
			revoked = values.Get("token")
		}
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(body))}, nil
	})
	base := l.lock
	var calls int
	l.lock = func(id string, create bool) func() (func(), error) {
		calls++
		if calls == 2 {
			close(waiting)
		}
		return base(id, create)
	}
	issued, closed := make(chan error, 1), make(chan error, 1)
	go func() {
		commands, _, _, err := l.Issue(context.Background(), credential.LinearRequest{App: containerApp(a), Scopes: a.scopes()}, "issuing")
		for _, command := range commands {
			command.Erase()
		}
		issued <- err
	}()
	<-entered
	go func() { closed <- l.Revoke(context.Background(), "issuing") }()
	select {
	case <-waiting:
	case err := <-closed:
		t.Fatalf("close returned before issuance: %v", err)
	}
	releaseOnce.Do(func() { close(release) })
	if err := <-issued; err != nil {
		t.Fatal(err)
	}
	if err := <-closed; err != nil || revoked != "ISSUING-TOKEN" {
		t.Fatalf("close missed issued token: %v", err)
	}
}

type failedContainerSave struct {
	linearKeys
	remaining int
	skip      int
}

func (k *failedContainerSave) UpdateSecret(service, account string, data []byte) error {
	if strings.HasPrefix(account, "container/") && k.skip > 0 {
		k.skip--
		return k.linearKeys.UpdateSecret(service, account, data)
	}
	if strings.HasPrefix(account, "container/") && k.remaining > 0 {
		k.remaining--
		return errors.New("temporary storage failure")
	}
	return k.linearKeys.UpdateSecret(service, account, data)
}

func TestLinearFailedRefreshPreservesPreviouslyDeliveredToken(t *testing.T) {
	l, k, _, calls := linearRig(t)
	a := linearApp("engineer")
	enrollLinear(t, l, k, a)
	request := credential.LinearRequest{App: containerApp(a), Scopes: a.scopes()}
	commands, _, _, err := l.Issue(context.Background(), request, "delivered")
	if err != nil {
		t.Fatal(err)
	}
	for _, command := range commands {
		command.Erase()
	}
	l.keys = &failedContainerSave{linearKeys: l.keys, skip: 1, remaining: 1}
	var installed linearAccess
	if _, err := l.Refresh(context.Background(), request, "delivered", func(_ []string, data []byte) error {
		return json.Unmarshal(data, &installed)
	}); err == nil || installed.AccessToken == "" {
		t.Fatal("expected successful delivery followed by failed record update")
	}
	if _, err := l.Refresh(context.Background(), request, "delivered", func([]string, []byte) error {
		return errors.New("delivery failed before replacement")
	}); err == nil {
		t.Fatal("delivery failure hidden")
	}
	for _, call := range *calls {
		if call.Get("token") == installed.AccessToken {
			t.Fatal("failed refresh revoked the token still installed")
		}
	}
	if err := l.Revoke(context.Background(), "delivered"); err != nil {
		t.Fatal(err)
	}
}

func TestLinearFailedSaveRetainsUnrevokedToken(t *testing.T) {
	for _, phase := range []string{"issue", "refresh"} {
		t.Run(phase, func(t *testing.T) {
			l, k, _, calls := linearRig(t)
			a := linearApp("engineer")
			enrollLinear(t, l, k, a)
			request := credential.LinearRequest{App: containerApp(a), Scopes: a.scopes()}
			count := 1
			if phase == "refresh" {
				commands, _, _, err := l.Issue(context.Background(), request, "save-failure")
				if err != nil {
					t.Fatal(err)
				}
				for _, command := range commands {
					command.Erase()
				}
				count = 2
			}
			l.keys = &failedContainerSave{linearKeys: l.keys, remaining: 1}
			transport := l.http.Transport
			l.http.Transport = linearRoundTrip(func(r *http.Request) (*http.Response, error) {
				if strings.HasSuffix(r.URL.Path, "/revoke") {
					return nil, errors.New("revocation unavailable")
				}
				return transport.RoundTrip(r)
			})
			var err error
			if phase == "issue" {
				_, _, _, err = l.Issue(context.Background(), request, "save-failure")
			} else {
				_, err = l.Refresh(context.Background(), request, "save-failure", func([]string, []byte) error { t.Fatal("delivered unsaved token"); return nil })
			}
			if err == nil {
				t.Fatal("save failure hidden")
			}
			var record linearContainerRecord
			if err := l.read("container/save-failure", &record); err != nil || len(record.Tokens) != count {
				t.Fatalf("unrevoked token reference lost: %v", err)
			}
			l.http.Transport = transport
			if err := l.Revoke(context.Background(), "save-failure"); err != nil {
				t.Fatal(err)
			}
			if !errors.Is(l.read("container/save-failure", &record), ErrNotFound) {
				t.Fatal("revoked record retained")
			}
			var revoked []string
			for _, call := range *calls {
				if token := call.Get("token"); token != "" {
					revoked = append(revoked, token)
				}
			}
			if len(revoked) != count {
				t.Fatalf("close revoked %d of %d tokens", len(revoked), count)
			}
		})
	}
}

func TestLinearRefreshRevocationRetry(t *testing.T) {
	l, k, _, calls := linearRig(t)
	a := linearApp("engineer")
	enrollLinear(t, l, k, a)
	request := credential.LinearRequest{App: containerApp(a), Scopes: a.scopes()}
	commands, _, _, err := l.Issue(context.Background(), request, "refresh-retry")
	if err != nil {
		t.Fatal(err)
	}
	for _, command := range commands {
		command.Erase()
	}
	transport := l.http.Transport
	l.http.Transport = linearRoundTrip(func(r *http.Request) (*http.Response, error) {
		if strings.HasSuffix(r.URL.Path, "/revoke") {
			return nil, errors.New("revocation unavailable")
		}
		return transport.RoundTrip(r)
	})
	delivered := false
	if _, err := l.Refresh(context.Background(), request, "refresh-retry", func([]string, []byte) error { delivered = true; return nil }); err == nil || !delivered {
		t.Fatal("expected delivered replacement with failed old-token revocation")
	}
	var record linearContainerRecord
	if l.read("container/refresh-retry", &record) != nil || len(record.Tokens) != 2 || record.Tokens[1].AccessToken != "TOKEN-2" {
		t.Fatal("lost current or unrevoked token")
	}
	l.http.Transport = transport
	if err := l.Revoke(context.Background(), "refresh-retry"); err != nil {
		t.Fatal(err)
	}
	if len(*calls) != 4 || (*calls)[2].Get("token") != "TOKEN-1" || (*calls)[3].Get("token") != "TOKEN-2" {
		t.Fatal("close did not revoke both tokens")
	}
	if !errors.Is(l.read("container/refresh-retry", &record), ErrNotFound) {
		t.Fatal("revoked record retained")
	}
}
