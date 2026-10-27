package broker

import (
	"bytes"
	"errors"
	"path/filepath"
	"testing"
)

func TestNonInteractiveDialogCallsRefuse(t *testing.T) {
	k := fileKeychain{path: filepath.Join(t.TempDir(), "none.keychain-db")} // no file: a Security call would fail otherwise
	if b, err := k.GetWithDialog("default"); !errors.Is(err, ErrNeedsDialog) || !bytes.Equal(b, nil) {
		t.Errorf("GetWithDialog: %q, %v", b, err)
	}
	if err := k.TrustAppsByDialog("ensemble-broker-test-dialog", "test-account"); !errors.Is(err, ErrNeedsDialog) {
		t.Errorf("TrustAppsByDialog: %v", err)
	}
}
