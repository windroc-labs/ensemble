package main

import (
	"context"
	"fmt"
	"os"

	"ensemble/broker/internal/broker"
)

// linearSystem is host-only: it never constructs a Docker client or launcher.
func linearSystem(ctx context.Context, enroll bool) (any, error) {
	identity, err := broker.ReadLinearIdentity(os.Stdin)
	if err != nil {
		return nil, err
	}
	if err = broker.CheckInstalled(); err != nil {
		return nil, err
	}
	home, err := accountHome()
	if err != nil {
		return nil, err
	}
	l := broker.NewLinear(home, func() ([]byte, error) {
		fmt.Fprint(os.Stderr, "Login keychain password, to make this Linear client secret trust only the broker (hidden): ")
		return broker.ReadHiddenTTY()
	})
	if enroll {
		if err := l.Enroll(identity); err != nil {
			return nil, err
		}
		return map[string]any{"version": 1, "enrolled": true}, nil
	}
	rec := recorder(home, "linear-maintain", "host/"+identity.TokenService)
	l.Audit = rec
	return withAudit(rec, func() (any, error) { return l.Maintain(ctx, identity) })
}
