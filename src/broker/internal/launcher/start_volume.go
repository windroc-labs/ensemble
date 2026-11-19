package launcher

import (
	"context"

	"ensemble/broker/internal/protocol"
)

// startVolume is a volume a start ensures on d; one it created goes on failure until its root is the agent's, as a later
// start would skip the chown.
type startVolume struct {
	d       Daemon
	name    string
	created bool
}

// undo fails a start with e.
func (h Host) undo(ctx context.Context, sv startVolume, e *protocol.Error) (bool, error) {
	if sv.created {
		h.removeVolume(ctx, sv.d, sv.name, e)
	}
	return false, e
}
