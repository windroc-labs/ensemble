package broker

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"ensemble/broker/internal/codex"
	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/protocol"
)

type fakeCodexNative struct {
	verifyErr, runErr error
	data              []byte
	home              string
	locked            bool
	runs              int
	onRun             func(string)
}

func (n *fakeCodexNative) Verify() (codex.Installation, error) {
	return codex.Installation{Version: "0.162.1", Path: "/fixed/codex", SHA256: strings.Repeat("a", 64)}, n.verifyErr
}

func (n *fakeCodexNative) Lock(ctx context.Context) (func(), error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	n.locked = true
	return func() { n.locked = false }, nil
}

func (n *fakeCodexNative) Refresh(context.Context, string) error {
	return errors.New("refresh unavailable in login fake")
}

func (n *fakeCodexNative) Run(ctx context.Context, home string, args ...string) error {
	n.home, n.runs = home, n.runs+1
	if !n.locked {
		return errors.New("not locked")
	}
	if !slices.Equal(args, []string{"-c", `cli_auth_credentials_store="file"`, "-c", `forced_login_method="chatgpt"`, "login"}) {
		return errors.New("wrong arguments")
	}
	if n.onRun != nil {
		n.onRun(home)
	}
	if n.runErr != nil {
		return n.runErr
	}
	return os.WriteFile(filepath.Join(home, "auth.json"), n.data, 0o600)
}

type fakeCodexKeychain struct {
	fakeApps
	native         *fakeCodexNative
	data           []byte
	missing        bool
	writeErr       error
	writeBreaksACL bool
	mismatchWrite  bool
	badReadOnce    bool
	failRestore    bool
	createTrusted  bool
	appliedErr     error // TrustApps' error after it applied the change
	writeCalls     int
	events         []string
}

func (k *fakeCodexKeychain) AppAccess(service, account string) (AppAccess, error) {
	if !k.native.locked {
		return AppAccess{}, errors.New("unlocked metadata read")
	}
	if service != codexService || account != codexAccount {
		return AppAccess{}, errors.New("wrong item")
	}
	if k.missing {
		return AppAccess{}, ErrNotFound
	}
	return k.fakeApps.AppAccess(service, account)
}

func (k *fakeCodexKeychain) Secret(service, account string) ([]byte, error) {
	if !k.native.locked {
		return nil, errors.New("unlocked read")
	}
	if service != codexService || account != codexAccount {
		return nil, errors.New("wrong item")
	}
	k.events = append(k.events, "read")
	if k.missing {
		return nil, ErrNotFound
	}
	if a, err := k.AppAccess(service, account); err != nil || onlyBroker(a) != nil {
		return nil, errors.New("not trusted")
	}
	if k.badReadOnce {
		k.badReadOnce = false
		return []byte("unverified-readback"), nil
	}
	return slices.Clone(k.data), k.getErr
}

func (k *fakeCodexKeychain) TrustApps(app, account string, password []byte) error {
	if err := k.fakeApps.TrustApps(app, account, password); err != nil {
		return err
	}
	return k.appliedErr
}

func (k *fakeCodexKeychain) CreateCodex() error {
	k.events = append(k.events, "create")
	if !k.missing {
		return errors.New("already exists")
	}
	k.missing, k.access = false, added()
	if k.createTrusted {
		a := brokerOnly()
		k.access = &a
	}
	return nil
}

func (k *fakeCodexKeychain) WriteCodex(data []byte) error {
	if !k.native.locked {
		return errors.New("unlocked write")
	}
	if err := onlyBroker(*k.access); err != nil {
		return errors.New("write before trust")
	}
	k.events = append(k.events, "write")
	k.writeCalls++
	if k.failRestore && k.writeCalls > 1 {
		return errors.New("restore denied")
	}
	if k.writeErr != nil {
		return k.writeErr
	}
	k.data = slices.Clone(data)
	if k.mismatchWrite {
		k.mismatchWrite = false
		k.badReadOnce = true
	}
	if k.writeBreaksACL {
		k.access = added()
	}
	return nil
}

var codexNow = time.Date(2026, 10, 17, 12, 0, 0, 0, time.UTC)

func codexAuth(exp int64) []byte {
	payload, _ := json.Marshal(map[string]int64{"exp": exp})
	token := "eyJhbGciOiJSUzI1NiJ9." + base64.RawURLEncoding.EncodeToString(payload) + ".signature"
	data, _ := json.Marshal(map[string]any{"auth_mode": "chatgpt", "OPENAI_API_KEY": nil, "tokens": map[string]string{"access_token": token, "refresh_token": "fake-refresh-secret", "id_token": "fake-id-secret", "account_id": "fake-account-secret"}, "last_refresh": codexNow.Format(time.RFC3339)})
	return data
}

func codexFixture(t *testing.T) (Codex, *fakeCodexNative, *fakeCodexKeychain) {
	t.Helper()
	n := &fakeCodexNative{data: codexAuth(codexNow.Add(time.Hour).Unix())}
	k := &fakeCodexKeychain{native: n, missing: true}
	root := t.TempDir()
	return Codex{native: n, keychain: k, tempRoot: root, revisionDir: filepath.Join(root, "codex-coordination"), now: func() time.Time { return codexNow }, uid: func() int { return 501 }, password: func() ([]byte, error) { return []byte(keychainPass), nil }}, n, k
}

func TestCodexContainerCredential(t *testing.T) {
	c, n, k := codexFixture(t)
	if err := c.Login(context.Background(), io.Discard); err != nil {
		t.Fatal(err)
	}
	original := slices.Clone(k.data)
	var payload []byte
	expiry, err := c.Inject(context.Background(), func(argv []string, stdin []byte) error {
		if !n.locked || bytes.Contains([]byte(strings.Join(argv, " ")), []byte("secret")) {
			t.Fatal("unlocked injection or secret argv")
		}
		payload = slices.Clone(stdin)
		return nil
	})
	if err != nil || expiry != codexNow.Add(time.Hour).Format(time.RFC3339) {
		t.Fatalf("expiry %s: %v", expiry, err)
	}
	var auth codexAuthDocument
	if json.Unmarshal(payload, &auth) != nil || auth.Mode != "chatgpt" || auth.Tokens.Refresh != "" || auth.Tokens.ID != "fake-id-secret" || auth.Tokens.Account != "fake-account-secret" {
		t.Fatal("wrong access-only document")
	}
	if bytes.Contains(payload, []byte("fake-refresh-secret")) || !bytes.Equal(original, k.data) || n.runs != 1 || n.locked {
		t.Fatal("credential isolation failed")
	}
}

func TestCodexContainerPreflightFailures(t *testing.T) {
	for _, failure := range []string{"missing", "expired", "native", "trust", "inject"} {
		t.Run(failure, func(t *testing.T) {
			c, n, k := codexFixture(t)
			if err := c.Login(context.Background(), io.Discard); err != nil {
				t.Fatal(err)
			}
			switch failure {
			case "missing":
				k.missing = true
			case "expired":
				k.data = codexAuth(codexNow.Unix())
			case "native":
				n.verifyErr = errors.New("bad native")
			case "trust":
				k.access = added()
			}
			called := false
			_, err := c.Inject(context.Background(), func([]string, []byte) error {
				called = true
				return errors.New("fake-refresh-secret")
			})
			if err == nil || strings.Contains(err.Error(), "fake-refresh-secret") || called != (failure == "inject") || n.locked {
				t.Fatal("failure leaked credentials, lock or executed injection")
			}
		})
	}
}

func TestCodexWriteFailureCodeAndCause(t *testing.T) {
	const full = "bootstrap: cannot write managed Codex credentials: No space left on device"
	for _, tc := range []struct {
		name, stderr, cause string
		err                 error
	}{
		{"known", "Traceback (most recent call last):\n  File x\n" + full + "\n", full, nil},
		{"fixed", "bootstrap: existing Codex credentials belong to another account\n", "bootstrap: existing Codex credentials belong to another account", nil},
		{"slash", "bootstrap: cannot write managed Codex credentials: Input/output error\n", "bootstrap: cannot write managed Codex credentials: Input/output error", nil},
		{"forged", "bootstrap: cannot write managed Codex credentials: eyJ.fake-id-secret.sig\n", "credential writer exited 1 without a known diagnostic", nil},
		{"forged digits", "bootstrap: cannot write managed Codex credentials: fake/secret/42\n", "credential writer exited 1 without a known diagnostic", nil},
		{"other", "fake-account-secret\n", "credential writer exited 1 without a known diagnostic", nil},
		{"command", "", "credential writer failed", errors.New("context canceled")},
	} {
		for _, command := range []string{"start", "refresh"} {
			t.Run(tc.name+"/"+command, func(t *testing.T) {
				c, _, _ := codexFixture(t)
				if err := c.Login(context.Background(), io.Discard); err != nil {
					t.Fatal(err)
				}
				exec := func([]string, []byte) error {
					if tc.err != nil {
						return tc.err
					}
					return &docker.CommandError{Exit: 1, Stderr: tc.stderr}
				}
				var err error
				want := protocol.StartFailed
				if command == "start" {
					_, err = c.Inject(context.Background(), exec)
				} else {
					want = protocol.RefreshFailed
					_, err = c.Refresh(context.Background(), exec, func(string) error { return nil })
				}
				var e *protocol.Error
				if !errors.As(err, &e) || e.Code != want || e.Cause != tc.cause || e.Message != "Writing Codex credentials into the container failed" {
					t.Fatalf("%#v", err)
				}
			})
		}
	}
	c, _, _ := codexFixture(t)
	if err := c.Login(context.Background(), io.Discard); err != nil {
		t.Fatal(err)
	}
	_, err := c.Refresh(context.Background(), func([]string, []byte) error { return docker.ErrUnreachable }, func(string) error { return nil })
	if !errors.Is(err, docker.ErrUnreachable) {
		t.Fatalf("unreachable daemon reclassified: %v", err)
	}
}

func TestCodexLoginIsolation(t *testing.T) {
	c, n, k := codexFixture(t)
	n.onRun = func(home string) {
		info, err := os.Stat(home)
		if err != nil || info.Mode().Perm() != 0o700 {
			t.Fatal("nonprivate home")
		}
		if filepath.Dir(home) != c.tempRoot {
			t.Fatal("wrong temp root")
		}
	}
	var out bytes.Buffer
	if err := c.Login(context.Background(), &out); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(n.home); !os.IsNotExist(err) {
		t.Fatal("temporary credentials survived")
	}
	if n.locked {
		t.Fatal("lock leaked")
	}
	if !slices.Equal(k.steps, []string{"partition ensemble-codex auth", "apps ensemble-codex auth"}) {
		t.Fatalf("ACL order %v", k.steps)
	}
	if !bytes.Equal(k.data, n.data) {
		t.Fatal("wrong stored auth")
	}
	if strings.Contains(out.String(), "secret") || strings.Contains(out.String(), "eyJ") {
		t.Fatal("secret output")
	}
}

// Every terminal question comes before the browser: the password only when the item needs it, never after the run.
func TestCodexLoginAsksBeforeBrowser(t *testing.T) {
	for _, kind := range []string{"fresh", "reinstalled", "fresh trusted", "trusted"} {
		t.Run(kind, func(t *testing.T) {
			c, n, k := codexFixture(t)
			c.password = func() ([]byte, error) {
				k.events = append(k.events, "password")
				return []byte(keychainPass), nil
			}
			switch kind {
			case "reinstalled":
				k.missing, k.data, k.access = false, codexAuth(codexNow.Add(2*time.Hour).Unix()), reinstalled()
			case "fresh trusted":
				k.createTrusted = true
			case "trusted":
				a := brokerOnly()
				k.missing, k.data, k.access = false, codexAuth(codexNow.Add(2*time.Hour).Unix()), &a
			}
			var stepsAtRun []string
			n.onRun = func(string) {
				k.events = append(k.events, "run")
				stepsAtRun = slices.Clone(k.steps)
			}
			if err := c.Login(context.Background(), &bytes.Buffer{}); err != nil {
				t.Fatal(err)
			}
			run, asked := slices.Index(k.events, "run"), 0
			for _, e := range k.events {
				if e == "password" {
					asked++
				}
			}
			if slices.Contains(k.events[run:], "password") {
				t.Fatalf("password asked after the browser: %v", k.events)
			}
			wantAsk := kind == "fresh" || kind == "reinstalled"
			if wantAsk && asked != 1 || !wantAsk && asked != 0 {
				t.Fatalf("password questions %v", k.events)
			}
			if wantAsk && !slices.Equal(stepsAtRun, []string{"partition ensemble-codex auth", "apps ensemble-codex auth"}) {
				t.Fatalf("trust at browser start %v", stepsAtRun)
			}
			if !wantAsk && len(k.steps) != 0 {
				t.Fatalf("trusted item changed %v", k.steps)
			}
			if !bytes.Equal(k.data, n.data) {
				t.Fatal("login not saved")
			}
		})
	}
}

// After the browser nothing asks: a restore that needs a password not read before it fails instead.
func TestCodexRestoreNeverAsksAfterBrowser(t *testing.T) {
	c, _, k := codexFixture(t)
	a := brokerOnly()
	k.missing, k.access, k.data = false, &a, codexAuth(codexNow.Add(2*time.Hour).Unix())
	k.writeBreaksACL = true
	asked := 0
	c.password = func() ([]byte, error) { asked++; return []byte(keychainPass), nil }
	err := c.Login(context.Background(), &bytes.Buffer{})
	if err == nil || !strings.Contains(err.Error(), "previous login could not be restored") || asked != 0 {
		t.Fatalf("restore %v, %d password questions", err, asked)
	}
}

func TestOnePassword(t *testing.T) {
	reads := 0
	p := onePassword{read: func() ([]byte, error) { reads++; return []byte(keychainPass), nil }}
	first, err := p.get()
	erase(first)
	second, _ := p.get()
	if err != nil || string(second) != keychainPass || reads != 1 {
		t.Fatalf("%v %q read %d times", err, second, reads)
	}
	p.close()
	if again, err := p.get(); err != nil || string(again) != keychainPass {
		t.Fatalf("close after a read dropped it: %v", err)
	}
	p.erase()
	if !bytes.Equal(p.value, make([]byte, len(keychainPass))) {
		t.Fatal("password not erased")
	}
	closed := onePassword{read: func() ([]byte, error) { reads++; return []byte(keychainPass), nil }}
	closed.close()
	if pw, err := closed.get(); err == nil || pw != nil || reads != 1 {
		t.Fatalf("closed reader asked: %q %v", pw, err)
	}
}

func TestCodexTrustMessages(t *testing.T) {
	for name, want := range map[string]string{
		"access unread": "cannot inspect Codex keychain access",
		"partition":     "Codex keychain trust incomplete; retry " + codexLoginCommand,
	} {
		c, n, k := codexFixture(t)
		k.missing, k.access = false, reinstalled()
		if name == "access unread" {
			k.readBackErr = errors.New("keychain status -25300")
		} else {
			k.partitionErr = errors.New("keychain status -25293")
		}
		if err := c.Login(context.Background(), &bytes.Buffer{}); err == nil || err.Error() != want || n.runs != 0 {
			t.Fatalf("%s: %v, browser runs %d", name, err, n.runs)
		}
	}
}

// A trust step that errors with its change applied passes on the access read back, as in login github.
func TestCodexTrustStepErrorButDone(t *testing.T) {
	c, n, k := codexFixture(t)
	k.missing, k.access = false, reinstalled()
	k.appliedErr = errors.New("keychain status -25300")
	if err := c.Login(context.Background(), &bytes.Buffer{}); err != nil || !bytes.Equal(k.data, n.data) {
		t.Fatalf("%v", err)
	}
}

func TestCodexLoginFailureCleanup(t *testing.T) {
	for _, runErr := range []error{errors.New("child fake-refresh-secret"), context.Canceled} {
		c, n, k := codexFixture(t)
		n.runErr = runErr
		var out bytes.Buffer
		err := c.Login(context.Background(), &out)
		if err == nil || strings.Contains(err.Error(), "fake-refresh-secret") {
			t.Fatalf("unsafe error %v", err)
		}
		if _, err := os.Stat(n.home); !os.IsNotExist(err) {
			t.Fatal("temporary home survived")
		}
		if k.data != nil || n.locked {
			t.Fatal("failure persisted login or lock")
		}
	}
}

func TestCodexLoginRejectsBeforeRun(t *testing.T) {
	for _, kind := range []string{"root", "pin", "cancel"} {
		c, n, _ := codexFixture(t)
		ctx := context.Background()
		switch kind {
		case "root":
			c.uid = func() int { return 0 }
		case "pin":
			n.verifyErr = errors.New("bad hash")
		case "cancel":
			var cancel context.CancelFunc
			ctx, cancel = context.WithCancel(ctx)
			cancel()
		}
		if c.Login(ctx, &bytes.Buffer{}) == nil || n.runs != 0 {
			t.Fatal(kind)
		}
	}
}

func TestCodexAuthValidation(t *testing.T) {
	for name, data := range map[string][]byte{
		"empty": nil, "malformed": []byte("fake-refresh-secret"), "expired": codexAuth(codexNow.Unix()),
		"api key":         []byte(`{"OPENAI_API_KEY":"fake-api-secret"}`),
		"missing refresh": []byte(`{"tokens":{"access_token":"a.b.c","id_token":"id"}}`),
		"oversize":        bytes.Repeat([]byte("a"), maxCodexAuth+1),
	} {
		t.Run(name, func(t *testing.T) {
			c, n, k := codexFixture(t)
			n.data = data
			err := c.Login(context.Background(), &bytes.Buffer{})
			if err == nil || strings.Contains(err.Error(), "fake-") {
				t.Fatalf("unsafe auth result %v", err)
			}
			if k.data != nil {
				t.Fatal("invalid auth saved")
			}
		})
	}
}

func TestCodexReloginAndTrustFailure(t *testing.T) {
	for _, kind := range []string{"relogin", "partition", "readback", "password", "write", "postwrite", "postread"} {
		t.Run(kind, func(t *testing.T) {
			c, n, k := codexFixture(t)
			old := codexAuth(codexNow.Add(2 * time.Hour).Unix())
			k.missing, k.data, k.access = false, slices.Clone(old), reinstalled()
			switch kind {
			case "partition":
				k.partitionErr = errors.New("fake-refresh-secret")
			case "readback":
				k.frozen = true
			case "password":
				c.password = func() ([]byte, error) { return nil, errors.New("needs an interactive terminal") }
			case "write":
				k.writeErr = errors.New("fake-refresh-secret")
			case "postwrite":
				k.writeBreaksACL = true
			case "postread":
				k.mismatchWrite = true
			}
			err := c.Login(context.Background(), &bytes.Buffer{})
			if kind == "relogin" {
				if err != nil || !bytes.Equal(k.data, n.data) {
					t.Fatal(err)
				}
				return
			}
			if err == nil || strings.Contains(err.Error(), "fake-refresh-secret") {
				t.Fatalf("unsafe error %v", err)
			}
			if !bytes.Equal(k.data, old) {
				t.Fatal("old login overwritten")
			}
			if (kind == "partition" || kind == "readback" || kind == "password") && n.runs != 0 {
				t.Fatal("browser opened before the keychain trusted only the broker")
			}
			if (kind == "postwrite" || kind == "postread") && !c.Status(context.Background()).LoggedIn {
				t.Fatal("previous login no longer usable")
			}
		})
	}
}

func TestCodexReportsRestorationFailure(t *testing.T) {
	c, n, k := codexFixture(t)
	a := brokerOnly()
	k.missing, k.access, k.data = false, &a, codexAuth(codexNow.Add(2*time.Hour).Unix())
	k.mismatchWrite, k.failRestore = true, true
	err := c.Login(context.Background(), &bytes.Buffer{})
	if err == nil || !strings.Contains(err.Error(), "previous login could not be restored") || strings.Contains(err.Error(), "secret") {
		t.Fatalf("hidden restoration failure %v", err)
	}
	if n.locked {
		t.Fatal("failed restoration retained lock")
	}
}

func TestCodexPinRepair(t *testing.T) {
	for _, kind := range []string{"missing", "corrupt"} {
		c, n, _ := codexFixture(t)
		want := "update-codex"
		n.verifyErr = errors.New("hash mismatch")
		if kind == "missing" {
			n.verifyErr = codex.ErrNotInstalled
			want = "install-codex"
		}
		if repair := c.Status(context.Background()).Repair; !strings.Contains(repair, want) {
			t.Fatalf("wrong repair %s", repair)
		}
		if err := c.Login(context.Background(), &bytes.Buffer{}); err == nil || !strings.Contains(err.Error(), want) {
			t.Fatalf("wrong diagnostic %v", err)
		}
	}
}

func TestCodexStatusSanitized(t *testing.T) {
	for _, kind := range []string{"ready", "missing", "expired", "invalid", "pin", "trust", "read"} {
		t.Run(kind, func(t *testing.T) {
			c, n, k := codexFixture(t)
			a := brokerOnly()
			k.access, k.missing, k.data = &a, false, n.data
			switch kind {
			case "missing":
				k.missing = true
			case "expired":
				k.data = codexAuth(codexNow.Add(-time.Hour).Unix())
			case "invalid":
				k.data = []byte("fake-refresh-secret")
			case "pin":
				n.verifyErr = errors.New("bad pin")
			case "trust":
				k.access = added()
			case "read":
				k.getErr = errors.New("fake-refresh-secret")
			}
			status := c.Status(context.Background())
			if status.LoggedIn != (kind == "ready") {
				t.Fatalf("wrong status %#v", status)
			}
			data, _ := json.Marshal(status)
			for _, secret := range []string{"fake-", "eyJ", "access_token\"", "refresh_token"} {
				if bytes.Contains(data, []byte(secret)) {
					t.Fatal("secret status")
				}
			}
			if kind == "ready" && status.ExpiresAt != codexNow.Add(time.Hour).Format(time.RFC3339) {
				t.Fatal("wrong expiry")
			}
		})
	}
}

type serializedCodexNative struct {
	*fakeCodexNative
	gate chan struct{}
}

func (n *serializedCodexNative) Lock(ctx context.Context) (func(), error) {
	select {
	case n.gate <- struct{}{}:
	case <-ctx.Done():
		return nil, ctx.Err()
	}
	n.locked = true
	return func() { n.locked = false; <-n.gate }, nil
}

func TestCodexConcurrentLoginRereads(t *testing.T) {
	c, n, k := codexFixture(t)
	c.native = &serializedCodexNative{fakeCodexNative: n, gate: make(chan struct{}, 1)}
	var wg sync.WaitGroup
	errs := make(chan error, 2)
	for range 2 {
		wg.Go(func() { errs <- c.Login(context.Background(), &bytes.Buffer{}) })
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Fatal(err)
		}
	}
	if n.runs != 2 || !slices.Equal(k.events, []string{"create", "write", "read", "read", "write", "read"}) {
		t.Fatalf("missing locked reread %v", k.events)
	}
}

func TestCodexRejectsCredentialSymlink(t *testing.T) {
	c, n, k := codexFixture(t)
	target := filepath.Join(t.TempDir(), "other")
	n.onRun = func(home string) {
		if err := os.Symlink(target, filepath.Join(home, "auth.json")); err != nil {
			t.Fatal(err)
		}
	}
	if c.Login(context.Background(), &bytes.Buffer{}) == nil || k.data != nil {
		t.Fatal("accepted credential symlink")
	}
}
