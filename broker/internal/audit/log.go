package audit

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"syscall"
)

// MaxSize is the most the log grows to before a write moves it to <path>.1, replacing the old one.
const MaxSize = 10 << 20

// Path is the account's log, under home.
func Path(home string) string {
	return filepath.Join(home, "Library/Logs/ensemble/broker.jsonl")
}

// Log appends lines to Path: a 0600 file, in a 0700 directory, both the account's own and never symlinks.
type Log struct{ Path string }

// owned fails unless f is the account's own file of the wanted type.
func owned(f *os.File, dir bool) error {
	info, err := f.Stat()
	if err != nil {
		return err
	}
	st, ok := info.Sys().(*syscall.Stat_t)
	switch {
	case !ok || int(st.Uid) != os.Getuid():
		return fmt.Errorf("%s is not the account's own", f.Name())
	case dir && !info.IsDir():
		return fmt.Errorf("%s is not a directory", f.Name())
	case !dir && !info.Mode().IsRegular():
		return fmt.Errorf("%s is not a regular file", f.Name())
	}
	return nil
}

func (l Log) open() (*os.File, int64, error) {
	// O_NONBLOCK: a FIFO with no reader fails instead of blocking; no effect on a regular file.
	f, err := os.OpenFile(l.Path, os.O_WRONLY|os.O_APPEND|os.O_CREATE|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0o600)
	if err != nil {
		return nil, 0, err
	}
	info, err := f.Stat()
	if err == nil {
		err = owned(f, false)
	}
	if err == nil {
		err = f.Chmod(0o600)
	}
	if err != nil {
		f.Close()
		return nil, 0, err
	}
	return f, info.Size(), nil
}

// Append writes each line plus LF in one write, under an exclusive flock on the directory, rotating first when a
// line would take a non-empty file past MaxSize.
func (l Log) Append(lines ...[]byte) error {
	dir := filepath.Dir(l.Path)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	d, err := os.OpenFile(dir, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_DIRECTORY, 0)
	if err != nil {
		return err
	}
	defer d.Close()
	if err := owned(d, true); err != nil {
		return err
	}
	if err := d.Chmod(0o700); err != nil {
		return err
	}
	if err := syscall.Flock(int(d.Fd()), syscall.LOCK_EX); err != nil {
		return err
	}
	f, size, err := l.open()
	if err != nil {
		return err
	}
	defer func() { f.Close() }()
	for _, line := range lines {
		record := append(append([]byte{}, line...), '\n')
		if size > 0 && size+int64(len(record)) > MaxSize {
			f.Close()
			if err := os.Rename(l.Path, l.Path+".1"); err != nil {
				return err
			}
			if f, size, err = l.open(); err != nil {
				return err
			}
		}
		n, err := f.Write(record)
		size += int64(n)
		if err != nil {
			return errors.Join(errors.New("appending to the audit log failed"), err)
		}
	}
	return nil
}
