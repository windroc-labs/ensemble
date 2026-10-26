package launcher

import (
	"context"
	"encoding/json"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"slices"
	"time"

	"ensemble/broker/internal/credential"
	"ensemble/broker/internal/protocol"
)

// Notice is one static secret notice a session is due: its key, to acknowledge it with, and its text.
type Notice struct {
	Key  string `json:"key"`
	Text string `json:"text"`
}

type NoticesResult struct {
	Container string   `json:"container"`
	Notices   []Notice `json:"notices"`
	NextCheck *string  `json:"next_check"` // when to check again; null: never, the container holds no static secret
}

// Notices is what the container started with path is due about its static secrets: each notice not yet delivered to
// it, and when to check again. A key in delivered that is due is recorded delivered, with one audit line, and left
// out. It reads only the host's records: no Docker call, no secret.
func (h Host) Notices(_ context.Context, container, path string, delivered []string) (NoticesResult, error) {
	h.Audit.Begin(container, nil)
	data, err := os.ReadFile(path)
	if err != nil {
		return NoticesResult{}, protocol.New(protocol.ManifestRejected, "Cannot read manifest "+path+".")
	}
	m, err := ParseManifest(data)
	if err != nil {
		return NoticesResult{}, err
	}
	if "ensemble-"+m.Name+"-"+m.Generation != container {
		return NoticesResult{}, protocol.New(protocol.ManifestRejected, "Manifest rejected: notices needs the container's manifest.")
	}
	if err := h.checkMounts(m); err != nil {
		return NoticesResult{}, err
	}
	result := NoticesResult{Container: container, Notices: []Notice{}}
	if h.Static == nil {
		return result, nil
	}
	record, found, err := h.readInjected(m)
	if err != nil || !found {
		return result, err
	}
	stored, err := h.Static.Load()
	if err != nil {
		return NoticesResult{}, storeFailed(err, protocol.InternalError)
	}
	now := h.now()
	refs := make([]string, 0, len(record.Injected))
	for ref := range record.Injected {
		refs = append(refs, ref)
	}
	slices.Sort(refs)
	told := func(key string) bool { return slices.Contains(record.Delivered, key) }
	acked := 0
	for _, ref := range refs {
		for _, n := range credential.Due(ref, record.Injected[ref], stored[ref], told, now) {
			if slices.Contains(delivered, n.Key) {
				record.Delivered = append(record.Delivered, n.Key)
				h.Audit.Notice(container, n.Audit())
				acked++
				continue
			}
			result.Notices = append(result.Notices, Notice{n.Key, n.Text})
		}
	}
	if acked > 0 {
		data, err := json.Marshal(record)
		if err == nil {
			err = h.writeState(m, staticFile(m), data)
		}
		if err != nil {
			return NoticesResult{}, protocol.New(protocol.InternalError, "Recording "+staticFile(m)+" failed.")
		}
	}
	if next := credential.NextCheck(record.Injected, now); next != nil {
		at := next.Format(time.RFC3339)
		result.NextCheck = &at
	}
	return result, nil
}

// readInjected is m's container's injected record; false when its start recorded none.
func (h Host) readInjected(m Manifest) (injectedRecord, bool, error) {
	parent := filepath.Join(h.Home, ".ensemble/docker-state", m.Name)
	if err := h.Safe(parent); err != nil {
		return injectedRecord{}, false, protocol.New(protocol.InternalError, "Reading "+staticFile(m)+" failed.")
	}
	data, err := os.ReadFile(filepath.Join(parent, staticFile(m)))
	if errors.Is(err, fs.ErrNotExist) {
		return injectedRecord{}, false, nil
	}
	var record injectedRecord
	if err == nil {
		err = json.Unmarshal(data, &record)
	}
	if err != nil {
		return injectedRecord{}, false, protocol.New(protocol.InternalError, "Reading "+staticFile(m)+" failed.")
	}
	if record.Delivered == nil {
		record.Delivered = []string{}
	}
	return record, true, nil
}
