package broker

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"ensemble/broker/internal/codex"
)

type refreshingNative struct {
	*serializedCodexNative
	refreshes int
	refresh   func(string) error
	enter     chan struct{}
}

func (n *refreshingNative) Lock(ctx context.Context) (func(), error) {
	if n.enter != nil {
		n.enter <- struct{}{}
	}
	return n.serializedCodexNative.Lock(ctx)
}

func (n *refreshingNative) Refresh(_ context.Context, home string) error {
	if !n.locked {
		return errors.New("unlocked refresh")
	}
	n.refreshes++
	n.home = home
	return n.refresh(home)
}

func renewalFixture(t *testing.T) (Codex, *refreshingNative, *fakeCodexKeychain) {
	t.Helper()
	c, native, key := codexFixture(t)
	if err := c.Login(context.Background(), io.Discard); err != nil {
		t.Fatal(err)
	}
	n := &refreshingNative{serializedCodexNative: &serializedCodexNative{fakeCodexNative: native, gate: make(chan struct{}, 1)}}
	c.native = n
	key.data = codexAuth(codexNow.Add(30 * time.Second).Unix())
	n.refresh = func(home string) error {
		return os.WriteFile(filepath.Join(home, "auth.json"), codexAuth(c.now().Add(time.Hour).Unix()), 0o600)
	}
	return c, n, key
}

func TestCodexRefreshConcurrentDelayedWaiter(t *testing.T) {
	c, n, _ := renewalFixture(t)
	n.enter = make(chan struct{}, 2)
	clock := codexNow
	c.now = func() time.Time { return clock }
	n.refresh = func(home string) error {
		return os.WriteFile(filepath.Join(home, "auth.json"), codexAuth(codexNow.Add(120*time.Second).Unix()), 0o600)
	}
	entered, release := make(chan struct{}), make(chan struct{})
	result := make(chan error, 2)
	go func() {
		_, err := c.Inject(context.Background(), func([]string, []byte) error {
			close(entered)
			<-release
			clock = clock.Add(61 * time.Second)
			return nil
		})
		result <- err
	}()
	<-n.enter
	<-entered
	go func() {
		_, err := c.Inject(context.Background(), func([]string, []byte) error { return nil })
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
	clock = clock.Add(time.Second)
	n.refresh = func(home string) error {
		return os.WriteFile(filepath.Join(home, "auth.json"), codexAuth(clock.Add(time.Hour).Unix()), 0o600)
	}
	if _, err := c.Inject(context.Background(), func([]string, []byte) error { return nil }); err != nil {
		t.Fatal(err)
	}
	if n.refreshes != 2 {
		t.Fatalf("later refresh count %d", n.refreshes)
	}
}

func TestCodexRenewalValidationAndCleanup(t *testing.T) {
	for _, failure := range []string{"native", "account", "expiry", "write", "cancel"} {
		t.Run(failure, func(t *testing.T) {
			c, n, k := renewalFixture(t)
			original := bytes.Clone(k.data)
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			n.refresh = func(home string) error {
				if failure == "native" {
					return errors.New("fake-refresh-secret")
				}
				data := codexAuth(codexNow.Add(time.Hour).Unix())
				if failure == "account" {
					data = bytes.ReplaceAll(data, []byte("fake-account-secret"), []byte("another-account"))
				}
				if failure == "expiry" {
					data = bytes.Clone(original)
				}
				if failure == "cancel" {
					cancel()
				}
				return os.WriteFile(filepath.Join(home, "auth.json"), data, 0o600)
			}
			if failure == "write" {
				k.writeErr = errors.New("fake-refresh-secret")
			}
			_, err := c.Inject(ctx, func([]string, []byte) error { t.Fatal("injected failed renewal"); return nil })
			if err == nil || strings.Contains(err.Error(), "fake-refresh-secret") {
				t.Fatalf("unsafe failure: %v", err)
			}
			if !bytes.Equal(original, k.data) {
				t.Fatal("invalid refresh replaced login")
			}
			if n.home != "" {
				if _, err := os.Stat(n.home); !os.IsNotExist(err) {
					t.Fatal("temporary home retained")
				}
			}
		})
	}
}

func TestCodexManagedCarrierAndPublicationLock(t *testing.T) {
	c, n, _ := renewalFixture(t)
	var auth codexAuthDocument
	_, err := c.Inject(context.Background(), func(_ []string, data []byte) error {
		if !n.locked {
			t.Fatal("publication outside account lock")
		}
		return json.Unmarshal(data, &auth)
	})
	if err != nil {
		t.Fatal(err)
	}
	if auth.Mode != "chatgpt" || auth.Tokens.Refresh != "" || auth.Tokens.ID != "fake-id-secret" {
		t.Fatal("wrong native guarded-reload carrier")
	}
}

func TestCodexPublicationFailureRepairsWithoutRotation(t *testing.T) {
	c, n, _ := renewalFixture(t)
	n.refresh = func(home string) error {
		return os.WriteFile(filepath.Join(home, "auth.json"), codexAuth(codexNow.Add(120*time.Second).Unix()), 0o600)
	}
	if _, err := c.Refresh(context.Background(), func([]string, []byte) error { return nil }, func(string) error { return errors.New("deadline write failed") }); err == nil {
		t.Fatal("accepted failed publication")
	}
	var expiry string
	if _, err := c.Refresh(context.Background(), func([]string, []byte) error { return nil }, func(value string) error {
		if !n.locked {
			t.Fatal("unlocked deadline")
		}
		expiry = value
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	if n.refreshes != 1 || expiry != codexNow.Add(120*time.Second).Format(time.RFC3339) {
		t.Fatal("failed to repair publication independently")
	}
}

func TestCodexLoginWinsAgainstQueuedRefresh(t *testing.T) {
	c, n, k := renewalFixture(t)
	n.enter = make(chan struct{}, 2)
	loginStarted, release := make(chan struct{}), make(chan struct{})
	n.data = bytes.ReplaceAll(codexAuth(codexNow.Add(time.Hour).Unix()), []byte("fake-account-secret"), []byte("new-login-account"))
	n.onRun = func(string) { close(loginStarted); <-release }
	result := make(chan error, 2)
	go func() { result <- c.Login(context.Background(), io.Discard) }()
	<-n.enter
	<-loginStarted
	go func() {
		_, err := c.Inject(context.Background(), func([]string, []byte) error { return nil })
		result <- err
	}()
	<-n.enter
	close(release)
	for range 2 {
		if err := <-result; err != nil {
			t.Fatal(err)
		}
	}
	if n.refreshes != 0 || !bytes.Equal(k.data, n.data) {
		t.Fatal("queued refresh overwrote new login")
	}
}

func TestCodexRevisionRejectsUnsafeMetadata(t *testing.T) {
	for _, kind := range []string{"directory-link", "file-link", "permissions", "malformed", "hardlink"} {
		t.Run(kind, func(t *testing.T) {
			c, _, _ := codexFixture(t)
			dir, err := c.revisionDirectory(true)
			if err != nil {
				t.Fatal(err)
			}
			path := filepath.Join(dir, "revision.json")
			switch kind {
			case "directory-link":
				c.revisionDir = filepath.Join(c.tempRoot, "linked")
				if err := os.Symlink(dir, c.revisionDir); err != nil {
					t.Fatal(err)
				}
			case "file-link":
				if err := os.Symlink(filepath.Join(c.tempRoot, "elsewhere"), path); err != nil {
					t.Fatal(err)
				}
			case "permissions":
				if err := os.Chmod(dir, 0o755); err != nil {
					t.Fatal(err)
				}
			case "malformed":
				if err := os.WriteFile(path, []byte(`{"revision":"bad","state":"complete"}`), 0o600); err != nil {
					t.Fatal(err)
				}
			case "hardlink":
				if err := os.WriteFile(path, []byte(`{}`), 0o600); err != nil {
					t.Fatal(err)
				}
				if err := os.Link(path, filepath.Join(dir, "alias")); err != nil {
					t.Fatal(err)
				}
			}
			if _, err := c.readRevision(); err == nil {
				t.Fatal("accepted unsafe coordination")
			}
		})
	}
}

func TestCodexRefreshUnsupportedPinNamesUpgrade(t *testing.T) {
	c, n, _ := renewalFixture(t)
	n.refresh = func(string) error { return codex.ErrRefreshVersion }
	err := c.Ready(context.Background())
	if err == nil || !strings.Contains(err.Error(), "sudo /usr/local/libexec/ensemble/ensemble-broker update-codex 0.163.0") {
		t.Fatalf("missing pinned upgrade repair: %v", err)
	}
	if strings.Contains(err.Error(), "login codex") {
		t.Fatal("unsupported pin suggested login")
	}
	if _, err := os.Stat(n.home); !os.IsNotExist(err) {
		t.Fatal("refresh home remains")
	}
}
