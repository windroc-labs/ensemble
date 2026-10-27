package audit

import (
	"encoding/json"
	"errors"
	"time"

	"ensemble/broker/internal/protocol"
)

// Appender is where Flush writes; Log in the broker.
type Appender interface {
	Append(lines ...[]byte) error
}

// Recorder collects one broker invocation's lines. The launcher and the token code add to it, and the
// command's caller flushes it once. Every method is a no-op on nil.
type Recorder struct {
	Command   string
	Container string      // the command's own container; "" until known
	Start     *StartFacts // start only
	Now       func() time.Time
	Log       Appender

	own     []Credential
	notices []Line
	flushed bool

	renewing bool          // a maintain that tried a token provider
	failed   protocol.Code // its first provider failure
}

// Begin names the command's container and, for start, the facts only its line holds.
func (r *Recorder) Begin(container string, start *StartFacts) {
	if r == nil {
		return
	}
	r.Container, r.Start = container, start
}

// Add records c on the command's line when container is the command's own; any other container's is dropped.
func (r *Recorder) Add(container string, c Credential) {
	if r == nil || r.Container == "" || container != r.Container {
		return
	}
	r.own = append(r.own, c)
}

// Renewing marks a maintain that tried a token provider, so it writes a refresh line; failed ("": none) is that line's
// error when the first try failed.
func (r *Recorder) Renewing(failed protocol.Code) {
	if r == nil {
		return
	}
	r.renewing = true
	if r.failed == "" {
		r.failed = failed
	}
}

// Notice records one delivered static secret notice for container ("": the host's), on a line of its own.
func (r *Recorder) Notice(container string, c Credential) {
	if r == nil {
		return
	}
	r.notices = append(r.notices, Line{Command: r.Command, Event: noticeEvent, Container: container, Credentials: []Credential{c}})
}

// Flush writes the notice lines, then the command line for start, refresh and close, and a refresh
// line for a maintain that tried a token provider, err being the command's error.
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
	for _, l := range r.notices {
		l.Time = at
		lines = append(lines, l)
	}
	event := r.Command
	if (r.Command == maintainCommand || r.Command == "linear-maintain") && r.renewing {
		event = refreshEvent
	}
	switch event {
	case startEvent, refreshEvent, closeEvent:
		line := Line{Time: at, Command: r.Command, Event: event, Container: r.Container, Start: r.Start, Credentials: r.own,
			Error: string(r.failed)}
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
