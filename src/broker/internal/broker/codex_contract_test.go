package broker

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"ensemble/broker/internal/credential"
	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/protocol"
)

const (
	codexA = "ensemble-codex_a-0123456789abcdef0123456789abcdef"
	codexB = "ensemble-codex_b-0123456789abcdef0123456789abcdef"
)

func nopExec([]string, []byte) error { return nil }

func nopPublish(string) error { return nil }

// grant is container's grant record in c's fake grants store.
func grant(t *testing.T, c Codex, container string) (credential.Record, bool) {
	t.Helper()
	var rec credential.Record
	data, ok := c.grants.(*secrets).items[codexGrantsService+"/"+container]
	if ok {
		if err := json.Unmarshal(data, &rec); err != nil {
			t.Fatal(err)
		}
	}
	return rec, ok
}

// grantState is container's newest grant entry; once delivered, it must be the only one.
func grantState(t *testing.T, c Codex, container string) credential.Material {
	t.Helper()
	rec, ok := grant(t, c, container)
	if !ok || len(rec.Tokens) == 0 {
		t.Fatalf("%s has no grant", container)
	}
	newest := slices.MaxFunc(rec.Tokens, func(a, b credential.Material) int { return a.Revision - b.Revision })
	if newest.State == credential.Delivered && len(rec.Tokens) != 1 {
		t.Fatalf("%s grant %+v", container, rec.Tokens)
	}
	return newest
}

// grantedPair is a logged-in renewal fixture with containers A and B started while the login was fresh, then the
// login left within codexRefreshAhead of expiry, so the next refresh is due.
func grantedPair(t *testing.T) (Codex, *refreshingNative, *fakeCodexKeychain) {
	t.Helper()
	c, n, k := renewalFixture(t)
	due := k.data
	k.data = codexAuth(codexNow.Add(time.Hour).Unix())
	for _, ctr := range []string{codexA, codexB} {
		if _, err := c.Inject(context.Background(), ctr, nopExec); err != nil {
			t.Fatal(err)
		}
	}
	k.data = due
	if n.refreshes != 0 {
		t.Fatalf("start renewed: %d", n.refreshes)
	}
	return c, n, k
}

// reopened is a new Codex over c's persistent fakes, as the next broker command builds one.
func reopened(c Codex) Codex {
	return Codex{native: c.native, keychain: c.keychain, grants: c.grants, grantsLock: c.grantsLock, password: c.password,
		revisionDir: c.revisionDir, tempRoot: c.tempRoot, now: c.now, uid: c.uid}
}

func TestCodexConcurrentRefreshesRotateOnce(t *testing.T) {
	c, n, _ := grantedPair(t)
	n.enter = make(chan struct{}, 2)
	entered, release := make(chan struct{}), make(chan struct{})
	published, result := make(chan string, 2), make(chan error, 2)
	publish := func(expiry string) error { published <- expiry; return nil }
	go func() {
		_, err := c.Refresh(context.Background(), codexA, func([]string, []byte) error {
			close(entered)
			<-release
			return nil
		}, publish)
		result <- err
	}()
	<-n.enter
	<-entered
	go func() {
		_, err := c.Refresh(context.Background(), codexB, nopExec, publish)
		result <- err
	}()
	<-n.enter
	close(release)
	for range 2 {
		if err := <-result; err != nil {
			t.Fatal(err)
		}
	}
	if n.refreshes != 1 {
		t.Fatalf("native refresh count %d", n.refreshes)
	}
	want := codexNow.Add(time.Hour).Format(time.RFC3339)
	if a, b := <-published, <-published; a != want || b != want {
		t.Fatalf("published %s and %s, want %s", a, b, want)
	}
	a, b := grantState(t, c, codexA), grantState(t, c, codexB)
	if a.Token != b.Token || a.State != credential.Delivered || b.State != credential.Delivered || a.ExpiresAt != want {
		t.Fatalf("grants %+v %+v", a, b)
	}
}

// renewsTo makes n's renewal return a login expiring at expires.
func renewsTo(n *refreshingNative, expires time.Time) {
	n.refresh = func(home string) error {
		return os.WriteFile(filepath.Join(home, "auth.json"), codexAuth(expires.Unix()), 0o600)
	}
}

func TestCodexFailedPublicationDoesNotRotateAgain(t *testing.T) {
	c, n, _ := grantedPair(t)
	clock := codexNow
	c.now = func() time.Time { return clock }
	renewed := codexNow.Add(5 * time.Minute) // still within codexRefreshAhead: due again at once
	renewsTo(n, renewed)
	failed := protocol.New(protocol.RefreshFailed, "Publishing the Codex credential deadline failed; retry refresh.")
	var e *protocol.Error
	if _, err := c.Refresh(context.Background(), codexA, nopExec, func(string) error { return failed }); !errors.As(err, &e) || e.Message != failed.Message {
		t.Fatalf("%v", err)
	}
	if g := grantState(t, c, codexA); g.State != credential.Prepared || g.ExpiresAt != renewed.Format(time.RFC3339) {
		t.Fatalf("grant after failed publication %+v", g)
	}
	clock = clock.Add(2 * time.Minute) // past the account's one-minute repair window
	var expiry string
	if _, err := reopened(c).Refresh(context.Background(), codexA, nopExec, func(value string) error {
		expiry = value
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	if n.refreshes != 1 || expiry != renewed.Format(time.RFC3339) {
		t.Fatalf("refreshes %d, published %s", n.refreshes, expiry)
	}
	if g := grantState(t, c, codexA); g.State != credential.Delivered {
		t.Fatalf("grant after repair %+v", g)
	}
	// Once delivered, a due login renews again.
	renewsTo(n, clock.Add(time.Hour))
	if _, err := c.Refresh(context.Background(), codexA, nopExec, nopPublish); err != nil || n.refreshes != 2 {
		t.Fatalf("refreshes %d: %v", n.refreshes, err)
	}
}

func TestCodexFailedDeliveryKeepsPreparedGrant(t *testing.T) {
	c, n, _ := grantedPair(t)
	clock := codexNow
	c.now = func() time.Time { return clock }
	renewsTo(n, codexNow.Add(5*time.Minute))
	n.enter = make(chan struct{}, 2)
	entered, release := make(chan struct{}), make(chan struct{})
	resultA, resultB := make(chan error, 1), make(chan error, 1)
	go func() {
		_, err := c.Refresh(context.Background(), codexA, func([]string, []byte) error {
			close(entered)
			<-release
			return &docker.CommandError{Exit: 1, Stderr: "bootstrap: invalid Codex home\n"}
		}, nopPublish)
		resultA <- err
	}()
	<-n.enter
	<-entered
	go func() {
		_, err := c.Refresh(context.Background(), codexB, nopExec, nopPublish)
		resultB <- err
	}()
	<-n.enter
	close(release)
	if err := <-resultA; err == nil {
		t.Fatal("refresh succeeded")
	}
	// B, queued behind A's failed delivery, is not failed by it: the renewal itself completed.
	if err := <-resultB; err != nil || n.refreshes != 1 {
		t.Fatalf("sibling: refreshes %d: %v", n.refreshes, err)
	}
	if g := grantState(t, c, codexA); g.State != credential.Prepared {
		t.Fatalf("grant after failed delivery %+v", g)
	}
	clock = clock.Add(2 * time.Minute)
	if _, err := reopened(c).Refresh(context.Background(), codexA, nopExec, nopPublish); err != nil || n.refreshes != 1 {
		t.Fatalf("refreshes %d: %v", n.refreshes, err)
	}
	if g := grantState(t, c, codexA); g.State != credential.Delivered {
		t.Fatalf("grant after redelivery %+v", g)
	}
}

func TestCodexReleaseKeepsSiblingsAndAccount(t *testing.T) {
	c, n, k := renewalFixture(t)
	k.data = codexAuth(codexNow.Add(time.Hour).Unix())
	for _, ctr := range []string{codexA, codexB} {
		if _, err := c.Inject(context.Background(), ctr, nopExec); err != nil {
			t.Fatal(err)
		}
	}
	items := c.grants.(*secrets).items
	login, siblingGrant, writes := slices.Clone(k.data), slices.Clone(items[codexGrantsService+"/"+codexB]), k.writeCalls
	revision, err := os.ReadFile(filepath.Join(c.revisionDir, "revision.json"))
	if err != nil && !os.IsNotExist(err) {
		t.Fatal(err)
	}
	for range 2 { // the second finds nothing: an absent grant is released
		if err := c.Release(context.Background(), codexA); err != nil {
			t.Fatal(err)
		}
	}
	if _, ok := grant(t, c, codexA); ok {
		t.Fatal("released grant kept")
	}
	after, _ := os.ReadFile(filepath.Join(c.revisionDir, "revision.json"))
	if !bytes.Equal(items[codexGrantsService+"/"+codexB], siblingGrant) || !bytes.Equal(k.data, login) || k.writeCalls != writes || !bytes.Equal(after, revision) || n.refreshes != 0 {
		t.Fatal("releasing A touched B's grant or the account")
	}
	if _, err := c.Refresh(context.Background(), codexB, nopExec, nopPublish); err != nil {
		t.Fatal(err)
	}
	if g := grantState(t, c, codexB); g.State != credential.Delivered {
		t.Fatalf("B %+v", g)
	}
	// A refresh racing A's close finds no container: it leaves no grant behind.
	gone := func([]string, []byte) error { return &docker.CommandError{Exit: 1, Stderr: "No such container"} }
	if _, err := c.Refresh(context.Background(), codexA, gone, nopPublish); err == nil {
		t.Fatal("refresh of a closed container succeeded")
	}
	if _, ok := grant(t, c, codexA); ok {
		t.Fatal("a refresh after close left a grant")
	}
}

func TestCodexGrantRecordHoldsNoToken(t *testing.T) {
	c, n, k := grantedPair(t)
	renewsTo(n, codexNow.Add(time.Hour))
	if _, err := c.Refresh(context.Background(), codexA, nopExec, nopPublish); err != nil {
		t.Fatal(err)
	}
	var auth codexAuthDocument
	if err := json.Unmarshal(k.data, &auth); err != nil {
		t.Fatal(err)
	}
	leaks := []string{auth.Tokens.Access, auth.Tokens.Refresh, auth.Tokens.ID, auth.Tokens.Account, "fake-refresh-secret", "eyJ"}
	for name, value := range c.grants.(*secrets).items {
		for _, s := range leaks {
			if bytes.Contains(value, []byte(s)) {
				t.Fatalf("%s holds %q", name, s)
			}
		}
	}
	if g := grantState(t, c, codexA); !strings.HasPrefix(g.Token, "sha256:") || len(g.Token) != len("sha256:")+64 {
		t.Fatalf("grant token %q", g.Token)
	}
}

func TestCodexRefreshAdoptsUnrecordedContainer(t *testing.T) {
	c, _, _ := codexFixture(t)
	if err := c.Login(context.Background(), io.Discard); err != nil {
		t.Fatal(err)
	}
	var payload []byte
	var expiry string
	got, err := c.Refresh(context.Background(), codexA, func(_ []string, stdin []byte) error {
		payload = slices.Clone(stdin)
		return nil
	}, func(value string) error { expiry = value; return nil })
	want := codexNow.Add(time.Hour).Format(time.RFC3339)
	if err != nil || got != want || expiry != want || len(payload) == 0 {
		t.Fatalf("%s %s: %v", got, expiry, err)
	}
	if g := grantState(t, c, codexA); g.State != credential.Delivered || g.Token != codexFingerprint(payload) {
		t.Fatalf("adopted grant %+v", g)
	}
}

func TestCodexSentinelNeverLeaks(t *testing.T) {
	const sentinel = "CODEX_SENTINEL_d41d8c"
	for _, failure := range []string{"renewal", "writer", "unknown writer"} {
		t.Run(failure, func(t *testing.T) {
			c, n, _ := grantedPair(t)
			exec := nopExec
			switch failure {
			case "renewal":
				n.refresh = func(string) error { return errors.New(sentinel) }
			case "writer":
				renewsTo(n, codexNow.Add(time.Hour))
				exec = func([]string, []byte) error {
					return &docker.CommandError{Exit: 1, Stderr: "bootstrap: cannot write managed Codex credentials: " + sentinel + "\n"}
				}
			case "unknown writer":
				renewsTo(n, codexNow.Add(time.Hour))
				exec = func([]string, []byte) error { return &docker.CommandError{Exit: 1, Stderr: sentinel + "\n"} }
			}
			_, err := c.Refresh(context.Background(), codexA, exec, nopPublish)
			var e *protocol.Error
			if !errors.As(err, &e) {
				t.Fatalf("%#v", err)
			}
			response, _ := json.Marshal(e) // what the broker's reply and the audit line carry
			if strings.Contains(err.Error(), sentinel) || bytes.Contains(response, []byte(sentinel)) {
				t.Fatalf("sentinel leaked: %s", response)
			}
			for name, value := range c.grants.(*secrets).items {
				if bytes.Contains(value, []byte(sentinel)) {
					t.Fatalf("%s holds the sentinel", name)
				}
			}
		})
	}
}

// brokenGrants fails every grant-record write, or the lock.
type brokenGrants struct{ *secrets }

func (brokenGrants) PutSecret(string, string, []byte) error {
	return errors.New("keychain detail STORE_SENTINEL")
}

func TestCodexGrantErrors(t *testing.T) {
	for name, want := range map[string]string{
		"store": "Keychain access to the Codex grants failed.",
		"lock":  "Taking the Codex grant lock ~/.ensemble/docker/codex-grants.lock failed.",
	} {
		t.Run(name, func(t *testing.T) {
			c, n, _ := codexFixture(t)
			if err := c.Login(context.Background(), io.Discard); err != nil {
				t.Fatal(err)
			}
			if name == "store" {
				c.grants = brokenGrants{c.grants.(*secrets)}
			} else {
				c.grantsLock = func() (func(), error) { return nil, errors.New("flock: STORE_SENTINEL") }
			}
			_, err := c.Inject(context.Background(), codexA, func([]string, []byte) error {
				t.Fatal("wrote a credential whose grant was not recorded")
				return nil
			})
			var e *protocol.Error
			if !errors.As(err, &e) || e.Code != protocol.KeychainFailed || e.Message != want || strings.Contains(err.Error(), "STORE_SENTINEL") {
				t.Fatalf("%#v", err)
			}
			if n.locked {
				t.Fatal("account lock held after the run")
			}
		})
	}
	err := grantsError(codexA, credential.ErrReleased)
	var e *protocol.Error
	if !errors.As(err, &e) || e.Code != protocol.RefreshFailed || e.Message != codexA+" was closed during the refresh." {
		t.Fatalf("%#v", err)
	}
}

// TestCodexFailedGrantWriteKeepsDeliveredGrant: a refresh whose grant write fails, the login not renewed meanwhile,
// leaves the container's delivered grant as it was.
func TestCodexFailedGrantWriteKeepsDeliveredGrant(t *testing.T) {
	c, n, k := renewalFixture(t)
	k.data = codexAuth(codexNow.Add(time.Hour).Unix()) // not due: every refresh re-issues the same login
	if _, err := c.Inject(context.Background(), codexA, nopExec); err != nil {
		t.Fatal(err)
	}
	granted := grantState(t, c, codexA)
	working := c.grants
	c.grants = brokenGrants{working.(*secrets)}
	_, err := c.Refresh(context.Background(), codexA, nopExec, nopPublish)
	var e *protocol.Error
	if !errors.As(err, &e) || e.Code != protocol.KeychainFailed {
		t.Fatalf("%#v", err)
	}
	c.grants = working
	if g := grantState(t, c, codexA); g != granted {
		t.Fatalf("grant %+v, was %+v", g, granted)
	}
	if _, err := c.Refresh(context.Background(), codexA, nopExec, nopPublish); err != nil || n.refreshes != 0 {
		t.Fatalf("refreshes %d: %v", n.refreshes, err)
	}
}
