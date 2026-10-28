// Package credential is the broker's two credential lifecycles: the token lifecycle of one shared account published
// to host consumers (Linear's: the record it keeps of each consumer's copy) and the static-secret module (static.go:
// the versions, expiries and notices of the secrets a human registers), plus the adapters every credential is
// delivered (Deliver) and audited (Note) through. It never reads the Keychain or calls a provider itself: callers hand
// it a Store and a Provider.
package credential

import (
	"context"
	"errors"
	"fmt"
	"strings"
)

// Kind names a credential the broker holds.
type Kind string

// Token kinds the broker renews and revokes; static kinds a human registers and rotates.
const (
	GitHub Kind = "github"
	Codex  Kind = "codex"
	Linear Kind = "linear"

	Claude   Kind = "claude"
	Opencode Kind = "opencode"
	Env      Kind = "env"
)

// ErrStatic refuses a static credential where only a token kind fits: renewal and remote revocation.
var ErrStatic = errors.New("static credential: no renewal or remote revocation")

// Renewable is nil for a token kind, ErrStatic for a static one, and an error for an unknown kind.
func Renewable(k Kind) error {
	switch k {
	case GitHub, Codex, Linear:
		return nil
	case Claude, Opencode, Env:
		return fmt.Errorf("%s: %w", k, ErrStatic)
	}
	return fmt.Errorf("unknown credential kind %q", k)
}

// ErrConsumer refuses a malformed consumer.
var ErrConsumer = errors.New("credential consumer mismatch")

// Consumer is a host identity holding a grant of one shared account; it has no container.
type Consumer struct{ Host string }

// ID keys the consumer's record.
func (c Consumer) ID() string { return "host/" + c.Host }

func (c Consumer) check() error {
	if c.Host == "" || strings.Contains(c.Host, "/") {
		return ErrConsumer
	}
	return nil
}

// Delivery is a consumer and Publish (nil: none), which runs after each material is recorded, before it counts as
// delivered: the consumer's record is its only copy.
type Delivery struct {
	Consumer Consumer
	Publish  func(m Material) error
}

// State is where a material is in its consumer's lifecycle.
type State string

const (
	Prepared  State = "prepared"  // recorded, not yet confirmed delivered
	Delivered State = "delivered" // confirmed in the consumer
	Retiring  State = "retiring"  // replaced, awaiting withdrawal
)

// Material is one issued token. Account and Target are its slot, one grant position; the JSON names are those of
// the records already stored.
type Material struct {
	Account   string `json:"account"`
	Target    int64  `json:"installation"`
	Token     string `json:"token"`
	ExpiresAt string `json:"expires_at"` // RFC 3339
	Revision  int    `json:"revision,omitempty"`
	State     State  `json:"state,omitempty"` // "": an entry an older broker wrote; it recorded only tokens it delivered
}

// Record is the broker's host copy of one consumer's materials. Its desired revision is its materials' highest,
// its delivered revision the highest delivered one; neither is stored apart.
type Record struct {
	Subject string            `json:"app"` // the identity issued under
	Scope   map[string]string `json:"scope,omitempty"`
	Tokens  []Material        `json:"tokens"`
}

// File is where a material is written in a container: Dir/Name under docker.SecretsDir. Replace swaps an existing
// file atomically; otherwise one already there is refused.
type File struct {
	Dir, Name string
	Replace   bool
}

// Provider issues one token kind of a shared account.
type Provider interface {
	Kind() Kind
	AuditKind() string                                         // the audit log's kind for its tokens
	Begin(ctx context.Context, subject string) (Issuer, error) // authenticates once for a run of issues; its errors hold no secret
}

// Issuer issues materials under one authentication.
type Issuer interface {
	Targets(ctx context.Context) ([]Material, error) // the slots a new grant covers, Token empty
	Issue(ctx context.Context, scope map[string]string) (Material, error)
	Secret() string // the request credential, for redaction
}
