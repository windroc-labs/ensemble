package launcher

import (
	"os"
	"path/filepath"
)

// writeState replaces file in m's session state directory atomically: a synced temporary file renamed over it.
func (h Host) writeState(m Manifest, file string, data []byte) error {
	parent := filepath.Join(h.Home, ".ensemble/docker-state", m.Name)
	if err := h.Safe(parent); err != nil {
		return err
	}
	temporary, err := os.CreateTemp(parent, ".state-")
	if err != nil {
		return err
	}
	defer os.Remove(temporary.Name())
	if _, err := temporary.Write(data); err != nil {
		_ = temporary.Close()
		return err
	}
	if err := temporary.Sync(); err != nil {
		_ = temporary.Close()
		return err
	}
	if err := temporary.Close(); err != nil {
		return err
	}
	if err := os.Rename(temporary.Name(), filepath.Join(parent, file)); err != nil {
		return err
	}
	directory, err := os.Open(parent)
	if err != nil {
		return err
	}
	defer directory.Close()
	return directory.Sync()
}
