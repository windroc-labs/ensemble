package launcher

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"syscall"

	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/protocol"
)

const (
	lockLabel  = "windroc.lock"
	ownerLabel = "windroc.lock.owner"
)

var containerIDPattern = regexp.MustCompile(`^[0-9a-f]{64}$`)

type LockResult struct {
	Holder *protocol.Holder `json:"holder"`
}

type lockContainer struct {
	ID     string `json:"Id"`
	Name   string
	Config struct{ Labels map[string]string }
	State  struct{ Status string }
}

func busy(holder *protocol.Holder) *protocol.Error {
	e := protocol.Newf(protocol.Busy, "Lock held by %s (%s).", holder.Container, holder.Owner)
	e.Holder = holder
	return e
}

// inspectLock inspects one immutable ID.
func (h Host) inspectLock(ctx context.Context, ref string) (lockContainer, error) {
	out, err := h.Docker.Run(ctx, []string{"container", "inspect", ref}, nil)
	if err != nil {
		return lockContainer{}, err
	}
	var found []lockContainer
	if json.Unmarshal(out, &found) != nil || len(found) != 1 || !containerIDPattern.MatchString(found[0].ID) ||
		found[0].Name == "" || hasControl(found[0].Name) || found[0].State.Status == "" || found[0].ID != ref {
		return lockContainer{}, protocol.New(protocol.StartFailed, "Cannot decode lock container inspection.")
	}
	found[0].Name = strings.TrimPrefix(found[0].Name, "/")
	return found[0], nil
}

// lockHolder observes active run containers only.
func (h Host) lockHolder(ctx context.Context, lock, exclude string) (*protocol.Holder, error) {
	out, err := h.Docker.Run(ctx, []string{"ps", "--all", "--quiet", "--no-trunc", "--filter", "label=" + lockLabel + "=" + lock}, nil)
	if err != nil {
		return nil, failure(err, protocol.StartFailed, "Cannot query lock "+lock+".")
	}
	ids := strings.Fields(string(out))
	slices.Sort(ids)
	for _, id := range ids {
		if !containerIDPattern.MatchString(id) {
			return nil, protocol.New(protocol.StartFailed, "Invalid container ID in lock query.")
		}
		c, err := h.inspectLock(ctx, id)
		if docker.Absent(err) {
			continue
		}
		if err != nil {
			return nil, failure(err, protocol.StartFailed, "Cannot inspect holder of lock "+lock+".")
		}
		if c.Name == exclude || c.Config.Labels[lockLabel] != lock {
			continue
		}
		switch c.State.Status {
		case "running", "paused", "restarting":
			return &protocol.Holder{Container: c.Name, Owner: protocol.Text(c.Config.Labels[ownerLabel])}, nil
		}
	}
	return nil, nil
}

// LockQuery is read-only; it does not take the start lock or check credentials.
func (h Host) LockQuery(ctx context.Context, lock string) (LockResult, error) {
	if !ValidLock(lock) {
		return LockResult{}, protocol.New(protocol.ManifestRejected, "Invalid lock name.")
	}
	holder, err := h.lockHolder(ctx, lock, "")
	return LockResult{Holder: holder}, err
}

// startLock takes lock's start lock without waiting, from before the holder check until the start returns; another
// start holding it is busy. Closing the file, once this process and every command holding it have ended, releases it.
func (h Host) startLock(lock string) (*os.File, error) {
	shown := "~/.ensemble/docker/locks/" + lock + ".lock"
	path := h.dockerPath("locks/" + lock + ".lock")
	failed := protocol.New(protocol.StartFailed, "Taking the start lock "+shown+" failed.")
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return nil, failed
	}
	f, err := os.OpenFile(path, os.O_RDWR|os.O_CREATE, 0o600)
	if err != nil {
		return nil, failed
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		_ = f.Close()
		if errors.Is(err, syscall.EWOULDBLOCK) {
			return nil, busy(&protocol.Holder{Container: shown, Owner: "starting"})
		}
		return nil, failed
	}
	return f, nil
}
