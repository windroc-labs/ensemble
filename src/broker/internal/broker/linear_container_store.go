package broker

import (
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"syscall"
)

// linearContainerRecord keeps revocation possible after the guard and tmpfs stop.
// Closed blocks queued maintenance; Tokens retains failed revocations beside Current.
type linearContainerRecord struct {
	Closed  bool           `json:"closed"`
	Current linearAccess   `json:"current"`
	Tokens  []linearAccess `json:"tokens"`
}

func (l Linear) containerFile(c string) string {
	return filepath.Join(l.containerDir, fmt.Sprintf("%x.json", sha256.Sum256([]byte(c))))
}

func (l Linear) readContainer(c string) (linearContainerRecord, bool, error) {
	var record linearContainerRecord
	if _, err := privateDirectory(l.containerDir, false); err != nil {
		return record, false, errLinear
	}
	f, err := os.OpenFile(l.containerFile(c), os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
	if os.IsNotExist(err) {
		return record, false, nil
	}
	if err != nil {
		return record, false, errLinear
	}
	defer f.Close()
	fi, err := f.Stat()
	if err != nil {
		return record, false, errLinear
	}
	stat, ok := fi.Sys().(*syscall.Stat_t)
	if !ok || !fi.Mode().IsRegular() || fi.Mode().Perm()&0o077 != 0 || stat.Uid != uint32(os.Geteuid()) || stat.Nlink != 1 || fi.Size() > 32<<20 {
		return record, false, errLinear
	}
	data, err := io.ReadAll(io.LimitReader(f, 32<<20))
	defer erase(data)
	if err != nil || json.Unmarshal(data, &record) != nil {
		return record, false, errLinear
	}
	return record, true, nil
}

// writeContainer is called under the container lock, before any token is delivered.
func (l Linear) writeContainer(c string, record linearContainerRecord) error {
	directory, err := privateDirectory(l.containerDir, true)
	if err != nil {
		return errLinear
	}
	if len(record.Tokens) == 0 {
		if err := os.Remove(l.containerFile(c)); err != nil && !os.IsNotExist(err) {
			return errLinear
		}
	} else {
		f, err := os.CreateTemp(directory, ".token-")
		if err != nil {
			return errLinear
		}
		defer os.Remove(f.Name())
		data, _ := json.Marshal(record)
		_, err = f.Write(data)
		erase(data)
		if err == nil {
			err = f.Sync()
		}
		err = errors.Join(err, f.Close())
		if err == nil {
			err = os.Rename(f.Name(), l.containerFile(c))
		}
		if err != nil {
			return errLinear
		}
	}
	dir, err := os.Open(directory)
	if err != nil {
		return errLinear
	}
	defer dir.Close()
	if dir.Sync() != nil {
		return errLinear
	}
	return nil
}
