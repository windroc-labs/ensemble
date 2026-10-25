package broker

import (
	"bytes"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"syscall"
)

type codexRevision struct {
	Revision string `json:"revision"`
	State    string `json:"state"`
}

var revisionID = regexp.MustCompile(`^[0-9a-f]{32}$`)
var errRevision = errors.New("Codex refresh coordination unavailable; retry or repair the broker state directory")

func (c Codex) revisionDirectory(create bool) (string, error) {
	directory := c.revisionDir
	if !filepath.IsAbs(directory) || filepath.Clean(directory) != directory {
		return "", errRevision
	}
	var parts []string
	for p := directory; p != filepath.Dir(p); p = filepath.Dir(p) {
		parts = append(parts, p)
	}
	for i := len(parts) - 1; i >= 0; i-- {
		p := parts[i]
		fi, err := os.Lstat(p)
		if os.IsNotExist(err) && create {
			err = os.Mkdir(p, 0o700)
			if os.IsExist(err) {
				err = nil
			}
			if err == nil {
				fi, err = os.Lstat(p)
			}
		}
		if os.IsNotExist(err) && !create {
			return directory, nil
		}
		if err != nil || !fi.IsDir() || fi.Mode()&os.ModeSymlink != 0 {
			return "", errRevision
		}
		if i == 0 {
			s, ok := fi.Sys().(*syscall.Stat_t)
			if !ok || s.Uid != uint32(os.Geteuid()) || fi.Mode().Perm()&0o077 != 0 {
				return "", errRevision
			}
		}
	}
	return directory, nil
}

func (c Codex) readRevision() (codexRevision, error) {
	var revision codexRevision
	directory, err := c.revisionDirectory(false)
	if err != nil {
		return revision, err
	}
	f, err := os.OpenFile(filepath.Join(directory, "revision.json"), os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
	if os.IsNotExist(err) {
		return revision, nil
	}
	if err != nil {
		return revision, errRevision
	}
	defer f.Close()
	fi, err := f.Stat()
	if err != nil {
		return revision, errRevision
	}
	s, ok := fi.Sys().(*syscall.Stat_t)
	if !ok || !fi.Mode().IsRegular() || fi.Mode().Perm()&0o077 != 0 || s.Uid != uint32(os.Geteuid()) || s.Nlink != 1 || fi.Size() > 256 {
		return revision, errRevision
	}
	data, err := io.ReadAll(io.LimitReader(f, 257))
	if err != nil {
		return revision, errRevision
	}
	d := json.NewDecoder(bytes.NewReader(data))
	d.DisallowUnknownFields()
	var extra any
	if d.Decode(&revision) != nil || d.Decode(&extra) != io.EOF || !revisionID.MatchString(revision.Revision) || (revision.State != "active" && revision.State != "complete" && revision.State != "failed") {
		return codexRevision{}, errRevision
	}
	return revision, nil
}

func (c Codex) writeRevision(revision codexRevision) error {
	directory, err := c.revisionDirectory(true)
	if err != nil {
		return err
	}
	if _, err := c.readRevision(); err != nil {
		return err
	}
	f, err := os.CreateTemp(directory, ".revision-")
	if err != nil {
		return errRevision
	}
	defer os.Remove(f.Name())
	data, _ := json.Marshal(revision)
	_, err = f.Write(data)
	if err == nil {
		err = f.Sync()
	}
	err = errors.Join(err, f.Close())
	if err == nil {
		err = os.Rename(f.Name(), filepath.Join(directory, "revision.json"))
	}
	if err != nil {
		return errRevision
	}
	d, err := os.Open(directory)
	if err != nil {
		return errRevision
	}
	defer d.Close()
	if d.Sync() != nil {
		return errRevision
	}
	return nil
}

func (c Codex) beginRevision() (codexRevision, error) {
	var id [16]byte
	if _, err := rand.Read(id[:]); err != nil {
		return codexRevision{}, errRevision
	}
	r := codexRevision{Revision: hex.EncodeToString(id[:]), State: "active"}
	return r, c.writeRevision(r)
}

func (r codexRevision) completedSince(before codexRevision) bool {
	return r.State != "active" && (r.Revision != before.Revision || before.State == "active")
}
