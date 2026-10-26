package launcher

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"slices"
	"time"

	"ensemble/broker/internal/credential"
	"ensemble/broker/internal/protocol"
)

// StaticMeta is the host's record of the static secrets' versions and expiries (credential.StaticStore); the launcher
// never sees a secret through it.
type StaticMeta interface {
	Load() (map[string]credential.Static, error)
	DeliverHost(ref string, now time.Time, emit func(credential.Notice)) error
}

// staticRefs are the references of the static secrets m's container gets: its Claude token or OpenCode keys, then its
// environment secrets.
func staticRefs(m Manifest) []string {
	var refs []string
	switch m.Client {
	case "claude":
		refs = append(refs, credential.StaticRef(credential.Claude, m.ClaudeAccount))
	case "opencode":
		for _, p := range m.OpencodeProviders {
			refs = append(refs, credential.StaticRef(credential.Opencode, p))
		}
	}
	for _, s := range m.Secrets {
		refs = append(refs, s.Item)
	}
	return refs
}

// storeFailed is a static store error as a protocol error of code: its file and reason only.
func storeFailed(err error, code protocol.Code) *protocol.Error {
	var e *credential.StoreError
	if errors.As(err, &e) {
		return protocol.New(code, e.Error()+".")
	}
	return protocol.New(code, "Reading the static secret records failed.")
}

// checkStatic refuses refs when one's known expiry has passed: a configuration error naming it and its login, never
// busy. Without a store (h.Static nil) there is nothing to check.
func (h Host) checkStatic(refs []string) error {
	if h.Static == nil || len(refs) == 0 {
		return nil
	}
	records, err := h.Static.Load()
	if err != nil {
		return storeFailed(err, protocol.StartFailed)
	}
	now := h.now()
	for _, ref := range refs {
		if r := records[ref]; r.Expired(now) {
			return protocol.New(protocol.LoginRequired, credential.Text(ref, r, "expired", now)+".")
		}
	}
	return nil
}

// injectedRecord is <generation>.static.json: the static records a container was started with, and the notice keys
// delivered to it.
type injectedRecord struct {
	Injected  map[string]credential.Static `json:"injected"`
	Delivered []string                     `json:"delivered"`
}

func staticFile(m Manifest) string { return m.Generation + ".static.json" }

// injectedRecords is refs' records as a start injects them: an unrecorded one is version 0 and a pending one unknown, as is
// its expiry.
func injectedRecords(refs []string, records map[string]credential.Static) injectedRecord {
	out := injectedRecord{Injected: map[string]credential.Static{}, Delivered: []string{}}
	for _, ref := range refs {
		r := records[ref]
		if r.Pending || r.Provenance == "" {
			r = credential.Static{Version: r.Version, Provenance: credential.Unknown}
		}
		r.Notified = nil
		out.Injected[ref] = r
	}
	return out
}

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

// writeInjected records what m's container was started with.
func (h Host) writeInjected(m Manifest, record injectedRecord) error {
	data, err := json.Marshal(record)
	if err == nil {
		err = h.writeState(m, staticFile(m), data)
	}
	if err != nil {
		return protocol.New(protocol.StartFailed, "Recording "+staticFile(m)+" failed.")
	}
	return nil
}

// StaticStatus is one static secret as status shows it: never its value.
type StaticStatus struct {
	Reference  string                `json:"reference"`
	Version    int                   `json:"version"`
	ExpiresAt  *string               `json:"expires_at"`
	Provenance credential.Provenance `json:"provenance"`
	Notice     *string               `json:"notice"` // the most urgent notice reached, or a Claude token's unknown expiry
}

// StaticStatus lists every recorded static secret, plus each legacy reference that is usable and unrecorded. Only
// cleanup changes anything: it tells the host of each notice due to it once, recording and auditing it.
func (h Host) StaticStatus(cleanup bool, legacy map[string]bool) ([]StaticStatus, error) {
	if h.Static == nil {
		return nil, nil
	}
	records, err := h.Static.Load()
	if err != nil {
		return nil, storeFailed(err, protocol.InternalError)
	}
	now := h.now()
	refs := make([]string, 0, len(records)+len(legacy))
	for ref := range records {
		refs = append(refs, ref)
	}
	for ref, usable := range legacy {
		if _, ok := records[ref]; usable && !ok {
			refs = append(refs, ref)
		}
	}
	slices.Sort(refs)
	out := make([]StaticStatus, 0, len(refs))
	for _, ref := range refs {
		r := records[ref]
		if cleanup {
			if err := h.Static.DeliverHost(ref, now, func(n credential.Notice) { h.Audit.Notice("", n.Audit()) }); err != nil {
				return nil, storeFailed(err, protocol.InternalError)
			}
		}
		s := StaticStatus{Reference: ref, Version: r.Version, Provenance: credential.Unknown}
		kind, _, _ := credential.StaticKind(ref)
		if th := credential.Threshold(r, now); th != "" {
			s.ExpiresAt, s.Provenance = &r.ExpiresAt, r.Provenance
			text := credential.Text(ref, r, th, now)
			s.Notice = &text
		} else if !r.Pending && r.ExpiresAt != "" {
			s.ExpiresAt, s.Provenance = &r.ExpiresAt, r.Provenance
		} else if kind == credential.Claude {
			text := credential.UnknownText(ref)
			s.Notice = &text
		}
		out = append(out, s)
	}
	return out, nil
}
