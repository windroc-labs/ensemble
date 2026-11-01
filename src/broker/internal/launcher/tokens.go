package launcher

import (
	"encoding/json"
	"os"
	"path/filepath"
	"slices"
	"syscall"
	"time"

	"ensemble/broker/internal/credential"
	"ensemble/broker/internal/protocol"
)

// TokenRecord is <generation>.tokens.json, the broker's own schedule for a container's token providers: each kind's
// delivered expiries and, after a failure, when it may be tried again. Only the broker reads or writes it.
type TokenRecord struct {
	Expires map[credential.Kind][]string `json:"expires"`
	Retry   map[credential.Kind]string   `json:"retry"`
}

// Schedule keeps each container's TokenRecord.
type Schedule interface {
	Read(m Manifest) TokenRecord // empty when absent or unreadable, so every provider is due
	// Record replaces kind's entry, leaving every other kind's as another process last wrote it: its expiries (nil
	// keeps them) and its retry time (nil clears it). Its error is a *protocol.Error, internal_error.
	Record(m Manifest, kind credential.Kind, expires []string, retry *time.Time) error
	Drop(m Manifest) // removes the record, for a rolled-back start
}

// StateSchedule keeps the token record in m's session state directory.
type StateSchedule struct {
	Home string
	Safe func(string) error
}

func tokensFile(m Manifest) string { return m.Generation + ".tokens.json" }

func tokensLock(m Manifest) string { return m.Generation + ".tokens.lock" }

func newTokenRecord() TokenRecord {
	return TokenRecord{Expires: map[credential.Kind][]string{}, Retry: map[credential.Kind]string{}}
}

func tokensFailed(m Manifest) *protocol.Error {
	return protocol.New(protocol.InternalError, "Recording "+tokensFile(m)+" failed.")
}

func (s StateSchedule) parent(m Manifest) (string, error) {
	parent := filepath.Join(s.Home, ".ensemble/docker-state", m.Name)
	return parent, s.Safe(parent)
}

func (s StateSchedule) Read(m Manifest) TokenRecord {
	record := newTokenRecord()
	parent, err := s.parent(m)
	if err != nil {
		return record
	}
	data, err := os.ReadFile(filepath.Join(parent, tokensFile(m)))
	if err != nil {
		return record
	}
	var found TokenRecord
	if json.Unmarshal(data, &found) != nil {
		return record
	}
	for kind, expires := range found.Expires {
		record.Expires[kind] = expires
	}
	for kind, at := range found.Retry {
		record.Retry[kind] = at
	}
	return record
}

// Record holds <generation>.tokens.lock, never a link, only around its read and write.
func (s StateSchedule) Record(m Manifest, kind credential.Kind, expires []string, retry *time.Time) error {
	parent, err := s.parent(m)
	if err != nil {
		return tokensFailed(m)
	}
	lock, err := os.OpenFile(filepath.Join(parent, tokensLock(m)), os.O_CREATE|os.O_RDWR|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, 0o600)
	if err != nil {
		return tokensFailed(m)
	}
	defer lock.Close()
	if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX); err != nil {
		return tokensFailed(m)
	}
	record := s.Read(m)
	if expires != nil {
		record.Expires[kind] = slices.Clone(expires)
	}
	if retry == nil {
		delete(record.Retry, kind)
	} else {
		record.Retry[kind] = retry.UTC().Format(time.RFC3339)
	}
	data, err := json.Marshal(record)
	if err == nil {
		err = Host{Home: s.Home, Safe: s.Safe}.writeState(m, tokensFile(m), data)
	}
	if err != nil {
		return tokensFailed(m)
	}
	return nil
}

func (s StateSchedule) Drop(m Manifest) {
	if parent, err := s.parent(m); err == nil {
		_ = os.Remove(filepath.Join(parent, tokensFile(m)))
	}
}

// recordTokens is Schedule.Record; without a Schedule it records nothing.
func (h Host) recordTokens(m Manifest, kind credential.Kind, expires []string, retry *time.Time) error {
	if h.Schedule == nil {
		return nil
	}
	return h.Schedule.Record(m, kind, expires, retry)
}

func (h Host) dropTokens(m Manifest) {
	if h.Schedule != nil {
		h.Schedule.Drop(m)
	}
}

// expiryTimes is expiries' times, in order.
func expiryTimes(expiries []protocol.Expiry) []string {
	out := make([]string, len(expiries))
	for i, e := range expiries {
		out[i] = e.ExpiresAt
	}
	return out
}
