package broker

import (
	"bytes"
	"errors"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"
)

// The no-UI contract against a temporary keychain: an item another code signature (/usr/bin/security) owns is read,
// replaced and rewritten without UI, each call failing within seconds and the item unchanged. The login keychain and
// the user's search list stay as they were: every security call names the temporary keychain's path.
const (
	noUIService = "ensemble-broker-test-noui"
	noUIAccount = "test-account"
	noUIValue   = "foreign-value"
)

func runSecurity(t *testing.T, args ...string) string {
	t.Helper()
	out, err := exec.Command("/usr/bin/security", args...).CombinedOutput()
	if err != nil {
		t.Fatalf("security %s: %v: %s", strings.Join(args, " "), err, out)
	}
	return string(out)
}

// userSearchList is the user's keychain search list, for restoring after create-keychain adds the new one to it.
func userSearchList(t *testing.T) []string {
	t.Helper()
	var list []string
	for _, line := range strings.Split(runSecurity(t, "list-keychains", "-d", "user"), "\n") {
		if line = strings.Trim(strings.TrimSpace(line), `"`); line != "" {
			list = append(list, line)
		}
	}
	return list
}

func tempKeychain(t *testing.T) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "noui.keychain-db")
	list := userSearchList(t)
	restore := func() {
		if err := exec.Command("/usr/bin/security", append([]string{"list-keychains", "-d", "user", "-s"}, list...)...).Run(); err != nil {
			t.Errorf("restoring the keychain search list: %v", err)
		}
	}
	t.Cleanup(func() {
		restore()
		_ = exec.Command("/usr/bin/security", "delete-keychain", path).Run()
	})
	runSecurity(t, "create-keychain", "-p", "test-password", path)
	restore()
	runSecurity(t, "unlock-keychain", "-p", "test-password", path)
	runSecurity(t, "set-keychain-settings", path)
	return path
}

// within fails the test unless f returns within 10 s: a call that waits for a dialog never returns.
func within(t *testing.T, what string, f func() error) error {
	t.Helper()
	done := make(chan error, 1)
	go func() { done <- f() }()
	select {
	case err := <-done:
		return err
	case <-time.After(10 * time.Second):
		t.Fatalf("%s did not return within 10 s: it waits for a keychain dialog", what)
		return nil
	}
}

func TestNoUIAgainstForeignItem(t *testing.T) {
	path := tempKeychain(t)
	runSecurity(t, "add-generic-password", "-s", noUIService, "-a", noUIAccount, "-w", noUIValue, path)
	k := fileKeychain{path: path}

	if err := within(t, "Secret", func() error { _, err := k.Secret(noUIService, noUIAccount); return err }); !errors.Is(err, ErrNeedsDialog) {
		t.Errorf("Secret: %v, want ErrNeedsDialog", err)
	}
	if err := within(t, "AppKey", func() error { _, err := k.AppKey(noUIService, noUIAccount); return err }); !errors.Is(err, ErrNeedsDialog) {
		t.Errorf("AppKey: %v, want ErrNeedsDialog", err)
	}
	if err := within(t, "PutSecret", func() error { return k.PutSecret(noUIService, noUIAccount, []byte("replaced")) }); err == nil {
		t.Error("PutSecret replaced a foreign item")
	}
	if err := within(t, "WriteSecret", func() error { return k.WriteSecret(noUIService, noUIAccount, []byte("rewritten")) }); err == nil {
		t.Error("WriteSecret rewrote a foreign item")
	}
	if got := runSecurity(t, "find-generic-password", "-s", noUIService, "-a", noUIAccount, "-w", path); strings.TrimSpace(got) != noUIValue {
		t.Errorf("the item holds %q, want it unchanged", got)
	}

	accounts, err := k.Accounts(noUIService)
	if err != nil || !slices.Equal(accounts, []string{noUIAccount}) {
		t.Errorf("Accounts = %q, %v", accounts, err)
	}
	if accounts, err := k.Accounts("ensemble-broker-test-none"); err != nil || len(accounts) != 0 {
		t.Errorf("Accounts of no item = %q, %v", accounts, err)
	}
	services, err := k.Services("ensemble-broker-test-")
	if err != nil || !slices.Equal(services, []string{noUIService}) {
		t.Errorf("Services = %q, %v", services, err)
	}
}

func TestNonInteractiveDialogCallsRefuse(t *testing.T) {
	k := fileKeychain{path: filepath.Join(t.TempDir(), "none.keychain-db")} // no file: a Security call would fail otherwise
	if b, err := k.GetWithDialog("default"); !errors.Is(err, ErrNeedsDialog) || !bytes.Equal(b, nil) {
		t.Errorf("GetWithDialog: %q, %v", b, err)
	}
	if err := k.TrustAppsByDialog(noUIService, noUIAccount); !errors.Is(err, ErrNeedsDialog) {
		t.Errorf("TrustAppsByDialog: %v", err)
	}
}
