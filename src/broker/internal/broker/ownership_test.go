package broker

import (
	"bytes"
	"context"
	"errors"
	"strings"
	"testing"
)

func TestOpenCodeOwnershipRecovery(t *testing.T) {
	for _, phase := range []string{"save", "partition", "apps"} {
		t.Run(phase, func(t *testing.T) {
			k := &fakeSecrets{fakeApps: fakeApps{access: added()}}
			switch phase {
			case "save":
				k.putErr = ErrInvalidOwnerEdit
			case "partition":
				k.partitionErr = ErrInvalidOwnerEdit
			case "apps":
				k.appsErr = ErrInvalidOwnerEdit
			}
			_, err := storeLogin(t, "opencode", k, &askOnce{}, apiKey)
			assertOwnershipRecovery(t, err, "ensemble-broker-opencode/strata")
		})
	}
}

func TestCodexOwnershipRecovery(t *testing.T) {
	for _, phase := range []string{"save", "partition", "apps"} {
		t.Run(phase, func(t *testing.T) {
			c, _, k := codexFixture(t)
			k.missing, k.access = false, reinstalled()
			switch phase {
			case "save":
				k.writeErr = ErrInvalidOwnerEdit
			case "partition":
				k.partitionErr = ErrInvalidOwnerEdit
			case "apps":
				k.appsErr = ErrInvalidOwnerEdit
			}
			err := c.Login(context.Background(), &bytes.Buffer{})
			assertOwnershipRecovery(t, err, "ensemble-broker-codex/auth")
			if strings.Contains(err.Error(), "fake-") {
				t.Fatal("credential in diagnostic")
			}
		})
	}
}

func assertOwnershipRecovery(t *testing.T, err error, item string) {
	t.Helper()
	if err == nil || !errors.Is(err, ErrInvalidOwnerEdit) {
		t.Fatalf("ownership error lost: %v", err)
	}
	for _, text := range []string{item, "delete", "login keychain", "rerun install.sh"} {
		if !strings.Contains(err.Error(), text) {
			t.Fatalf("missing %q in %v", text, err)
		}
	}
}
