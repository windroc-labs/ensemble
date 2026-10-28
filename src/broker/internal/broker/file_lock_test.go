package broker

import (
	"os"
	"path/filepath"
	"testing"
)

func TestFileLockRejectsUnlinkedFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), "container.lock")
	stale, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	defer stale.Close()
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	unlock, err := FileLock(path, true)()
	if err != nil {
		t.Fatal(err)
	}
	defer unlock()
	if release, err := lockFile(stale); !os.IsNotExist(err) {
		if release != nil {
			release()
		}
		t.Fatalf("unlinked file accepted as current lock: %v", err)
	}
}
