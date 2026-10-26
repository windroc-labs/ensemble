package audit

import (
	"encoding/json"
	"errors"
	"slices"
	"time"

	"ensemble/broker/internal/protocol"
)

// Appender is where Flush writes; Log in the broker.
type Appender interface {
	Append(lines ...[]byte) error
}

type entry struct {
	Credential
	key string // a token's sha256, in memory only: matches its later outcomes
}

// Recorder collects one broker invocation's lines. The launcher and the GitHub token store add to it, and the
// command's caller flushes it once. Every method is a no-op on nil.
type Recorder struct {
	Command   string
	Container string      // the command's own container; "" until known
	Start     *StartFacts // start only
	Now       func() time.Time
	Log       Appender

	own     []entry
	others  map[string][]entry
	order   []string
	notices []Line
	flushed bool
}

// Begin names the command's container and, for start, the facts only its line holds.
func (r *Recorder) Begin(container string, start *StartFacts) {
	if r == nil {
		return
	}
	r.Container, r.Start = container, start
}

// merge appends c, or replaces the pending entry of the same token: its revocation resolves it.
func merge(list []entry, c Credential, key string) []entry {
	if key != "" {
		if i := slices.IndexFunc(list, func(e entry) bool { return e.key == key && e.Result == Pending }); i >= 0 {
			list[i] = entry{c, key}
			return list
		}
	}
	return append(list, entry{c, key})
}

// Add records c for container: on the command's line when it is the command's own, else on that container's retry
// line, which keeps only revoked and expired outcomes. key ("": none) is the token's in-memory identity.
func (r *Recorder) Add(container string, c Credential, key string) {
	if r == nil {
		return
	}
	if r.Container != "" && container == r.Container {
		r.own = merge(r.own, c, key)
		return
	}
	if container == "" || c.Result != Revoked && c.Result != ExpiredUnrevoked {
		return
	}
	if r.others == nil {
		r.others = map[string][]entry{}
	}
	if _, ok := r.others[container]; !ok {
		r.order = append(r.order, container)
	}
	r.others[container] = merge(r.others[container], c, key)
}

// Notice records one delivered static secret notice for container ("": the host's), on a line of its own.
func (r *Recorder) Notice(container string, c Credential) {
	if r == nil {
		return
	}
	r.notices = append(r.notices, Line{Command: r.Command, Event: noticeEvent, Container: container, Credentials: []Credential{c}})
}

func credentials(list []entry) []Credential {
	out := make([]Credential, 0, len(list))
	for _, e := range list {
		out = append(out, e.Credential)
	}
	return out
}

// Flush writes the retry lines, the notice lines, then the command line for start, refresh and close, err being the command's error.
// Its error describes only audit persistence; the caller keeps the command's result independently.
func (r *Recorder) Flush(err error) error {
	if r == nil || r.flushed {
		return nil
	}
	r.flushed = true
	now := time.Now
	if r.Now != nil {
		now = r.Now
	}
	at := now()
	var lines []Line
	for _, c := range r.order {
		lines = append(lines, Line{Time: at, Command: r.Command, Event: retryEvent, Container: c, Credentials: credentials(r.others[c])})
	}
	for _, l := range r.notices {
		l.Time = at
		lines = append(lines, l)
	}
	switch r.Command {
	case startEvent, refreshEvent, closeEvent:
		line := Line{Time: at, Command: r.Command, Event: r.Command, Container: r.Container, Start: r.Start, Credentials: credentials(r.own)}
		if err != nil {
			line.Error = string(protocol.InternalError)
			var e *protocol.Error
			if errors.As(err, &e) {
				line.Error = string(e.Code)
			}
		}
		lines = append(lines, line)
	}
	if len(lines) == 0 || r.Log == nil {
		return nil
	}
	data := make([][]byte, 0, len(lines))
	for _, l := range lines {
		b, err := json.Marshal(l)
		if err != nil {
			return err
		}
		data = append(data, b)
	}
	return r.Log.Append(data...)
}
