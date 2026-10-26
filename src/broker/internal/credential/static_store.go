package credential

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"time"
)

// StoreError is a static store failure: its file and a fixed reason, never the underlying error, which may quote
// what the file holds.
type StoreError struct{ Path, Reason string }

func (e *StoreError) Error() string { return e.Path + ": " + e.Reason }

// StaticStore is the host's record of each static secret, keyed by reference: no secret, only versions, expiries and
// what the host was told. Lock (nil: none) guards each read-modify-write; a write replaces the file atomically, so a
// read needs no lock.
type StaticStore struct {
	Path string
	Lock func() (unlock func(), err error)
}

type staticFile struct {
	Secrets map[string]Static `json:"secrets"`
}

func (s StaticStore) fail(reason string) error { return &StoreError{s.Path, reason} }

func (s Static) valid() bool {
	_, expires := s.expiry()
	switch {
	case s.Version < 0:
		return false
	case s.Provenance == Unknown:
		return s.ExpiresAt == ""
	case s.Provenance == Explicit, s.Provenance == Derived, s.Provenance == Assumed:
		return expires && !s.Pending
	}
	return false
}

// Load is every record; an absent file holds none.
func (s StaticStore) Load() (map[string]Static, error) {
	data, err := os.ReadFile(s.Path)
	switch {
	case errors.Is(err, fs.ErrNotExist):
		return map[string]Static{}, nil
	case err != nil:
		return nil, s.fail("cannot read")
	}
	var f staticFile
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&f) != nil || decoder.More() {
		return nil, s.fail("invalid content")
	}
	for ref, r := range f.Secrets {
		if _, _, ok := StaticKind(ref); !ok || !r.valid() {
			return nil, s.fail("invalid content")
		}
	}
	if f.Secrets == nil {
		f.Secrets = map[string]Static{}
	}
	return f.Secrets, nil
}

// update runs change on the records under the lock and writes them when it reports a change.
func (s StaticStore) update(change func(map[string]Static) bool) error {
	if s.Lock != nil {
		unlock, err := s.Lock()
		if err != nil {
			return s.fail("cannot lock")
		}
		defer unlock()
	}
	records, err := s.Load()
	if err != nil {
		return err
	}
	if !change(records) {
		return nil
	}
	return s.write(records)
}

func (s StaticStore) write(records map[string]Static) error {
	data, err := json.Marshal(staticFile{records})
	if err != nil {
		return s.fail("cannot write")
	}
	dir := filepath.Dir(s.Path)
	if os.MkdirAll(dir, 0o700) != nil {
		return s.fail("cannot write")
	}
	temporary, err := os.CreateTemp(dir, ".static-secrets-")
	if err != nil {
		return s.fail("cannot write")
	}
	defer os.Remove(temporary.Name())
	_, err = temporary.Write(data)
	err = errors.Join(err, temporary.Sync(), temporary.Close())
	if err == nil {
		err = os.Rename(temporary.Name(), s.Path)
	}
	if err != nil {
		return s.fail("cannot write")
	}
	if d, err := os.Open(dir); err == nil {
		defer d.Close()
		_ = d.Sync()
	}
	return nil
}

// Register is a login's seam: it resolves reg for ref's kind, records ref pending at the next version, runs store
// (the Keychain login), then commits the expiry, or restores the prior record when store fails; an unfinished prior
// restores as unknown, never pending. Commit and restore apply only while ref is still that pending version, so a
// concurrent login's newer record wins, and is what a superseded login returns.
func (s StaticStore) Register(ref string, reg Registration, now time.Time, store func() error) (Static, error) {
	kind, _, ok := StaticKind(ref)
	if !ok {
		return Static{}, fmt.Errorf("%q is not a static secret", ref)
	}
	expires, provenance, err := reg.Resolve(kind, now)
	if err != nil {
		return Static{}, err
	}
	var prior *Static
	version := 1
	if err := s.update(func(m map[string]Static) bool {
		if r, ok := m[ref]; ok {
			if r.Pending {
				r = Static{Version: r.Version, Provenance: Unknown}
			}
			prior, version = &r, r.Version+1
		}
		m[ref] = Static{Version: version, Provenance: Unknown, Pending: true}
		return true
	}); err != nil {
		return Static{}, err
	}
	ours := func(m map[string]Static) bool {
		r, ok := m[ref]
		return ok && r.Pending && r.Version == version
	}
	if err := store(); err != nil {
		if restoreErr := s.update(func(m map[string]Static) bool {
			if !ours(m) {
				return false
			}
			if prior == nil {
				delete(m, ref)
			} else {
				m[ref] = *prior
			}
			return true
		}); restoreErr != nil {
			return Static{}, fmt.Errorf("%w; restoring its expiry record failed: %v", err, restoreErr)
		}
		return Static{}, err
	}
	record := Static{Version: version, ExpiresAt: expires, Provenance: provenance}
	if err := s.update(func(m map[string]Static) bool {
		if !ours(m) {
			record = m[ref]
			return false
		}
		m[ref] = record
		return true
	}); err != nil {
		reason := "cannot write"
		var e *StoreError
		if errors.As(err, &e) {
			reason = e.Reason
		}
		return Static{}, fmt.Errorf("%s saved; recording its expiry in %s failed (%s); rerun the login", ref, s.Path, reason)
	}
	return record, nil
}

// DeliverHost tells the host of ref's due expiry notice once per version and threshold: it records the delivery,
// then hands each notice to emit. An unrecorded ref has none.
func (s StaticStore) DeliverHost(ref string, now time.Time, emit func(Notice)) error {
	var due []Notice
	err := s.update(func(m map[string]Static) bool {
		r, ok := m[ref]
		if !ok {
			return false
		}
		prefix := noticeKey(ref, r.Version, "")
		told := func(key string) bool {
			th, ok := strings.CutPrefix(key, prefix)
			return ok && slices.Contains(r.Notified, th)
		}
		due = Due(ref, r, r, told, now)
		for _, n := range due {
			r.Notified = append(r.Notified, n.Label)
		}
		m[ref] = r
		return len(due) > 0
	})
	if err != nil {
		return err
	}
	for _, n := range due {
		emit(n)
	}
	return nil
}
