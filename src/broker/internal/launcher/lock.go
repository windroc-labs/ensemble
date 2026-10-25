package launcher

import (
	"context"
	"encoding/json"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"

	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/protocol"
)

const (
	lockLabel  = "windroc.lock"
	ownerLabel = "windroc.lock.owner"
	mutexLabel = "windroc.lock.mutex"
	sinceLabel = "windroc.lock.since"
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

// inspectLock inspects one immutable ID (or the mutex name on a create conflict).
func (h Host) inspectLock(ctx context.Context, ref string) (lockContainer, error) {
	out, err := h.Docker.Run(ctx, []string{"container", "inspect", ref}, nil)
	if err != nil {
		return lockContainer{}, err
	}
	var found []lockContainer
	if json.Unmarshal(out, &found) != nil || len(found) != 1 || !containerIDPattern.MatchString(found[0].ID) ||
		found[0].Name == "" || hasControl(found[0].Name) || found[0].State.Status == "" ||
		containerIDPattern.MatchString(ref) && found[0].ID != ref {
		return lockContainer{}, protocol.New(protocol.StartFailed, "Cannot decode lock container inspection.")
	}
	found[0].Name = strings.TrimPrefix(found[0].Name, "/")
	return found[0], nil
}

// lockHolder observes active run containers only; the temporary mutex has a different label.
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

// LockQuery is read-only; it does not take the mutex or check credentials.
func (h Host) LockQuery(ctx context.Context, lock string) (LockResult, error) {
	if !ValidLock(lock) {
		return LockResult{}, protocol.New(protocol.ManifestRejected, "Invalid lock name.")
	}
	holder, err := h.lockHolder(ctx, lock, "")
	return LockResult{Holder: holder}, err
}

// acquireMutex makes an unstarted container; only the returned ID belongs to this start.
func (h Host) acquireMutex(ctx context.Context, lock, image string) (string, error) {
	now := time.Now
	if h.Now != nil {
		now = h.Now
	}
	name := "windroc-lock-" + lock
	starting := func() error { return busy(&protocol.Holder{Container: name, Owner: "starting"}) }
	create := func() (string, error) {
		out, err := h.Docker.Run(ctx, []string{"create", "--pull", "never", "--network", "none", "--restart", "no", "--name", name,
			"--label", mutexLabel + "=" + lock, "--label", sinceLabel + "=" + strconv.FormatInt(now().Unix(), 10), image}, nil)
		if err != nil {
			return "", err
		}
		id := strings.TrimSpace(string(out))
		if !containerIDPattern.MatchString(id) {
			return "", protocol.New(protocol.StartFailed, "Creating mutex "+name+" returned no container ID.")
		}
		return id, nil
	}
	id, err := create()
	if err == nil {
		return id, nil
	}
	if !docker.Conflict(err) {
		return "", failure(err, protocol.StartFailed, "Cannot create mutex "+name+".")
	}
	c, err := h.inspectLock(ctx, name)
	if docker.Absent(err) {
		return "", starting()
	}
	if err != nil {
		return "", failure(err, protocol.StartFailed, "Cannot inspect mutex "+name+".")
	}
	since, err := strconv.ParseInt(c.Config.Labels[sinceLabel], 10, 64)
	// A missing/malformed timestamp or a foreign container cannot prove expiry.
	if err != nil || since < 0 || c.Config.Labels[mutexLabel] != lock || since >= now().Unix()-120 {
		return "", starting()
	}
	if _, err := h.remove(ctx, c.ID); err != nil {
		return "", failure(err, protocol.StartFailed, "Cannot remove stale mutex "+name+" ("+c.ID+").")
	}
	id, err = create()
	if docker.Conflict(err) {
		return "", starting()
	}
	if err != nil {
		return "", failure(err, protocol.StartFailed, "Cannot retry mutex "+name+".")
	}
	return id, nil
}

func (h Host) releaseMutex(ctx context.Context, id string) *protocol.Error {
	rctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 30*time.Second)
	defer cancel()
	if _, err := h.remove(rctx, id); err != nil {
		return failure(err, protocol.StartFailed, "Cannot remove lock mutex "+id+".")
	}
	return nil
}
