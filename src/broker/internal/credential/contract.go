// Package credential is the broker's two credential lifecycles: the provider-neutral token lifecycle (the records it
// keeps of each consumer's tokens) and the static-secret module (static.go: the versions, expiries and notices of the
// secrets a human registers), plus the shared adapters every credential is delivered and audited through. It never
// reads the Keychain or calls a provider itself: callers hand it a Store and a Provider.
package credential

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	"ensemble/broker/internal/audit"
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

// ErrConsumer refuses a consumer that is malformed or does not match where its material would go.
var ErrConsumer = errors.New("credential consumer mismatch")

// Consumer holds a grant: a container, by name, or a host identity with no container. Exactly one is set.
type Consumer struct{ Container, Host string }

// ID keys the consumer's record and backlog entries: a container's name carries its generation.
func (c Consumer) ID() string {
	if c.Host != "" {
		return "host/" + c.Host
	}
	return c.Container
}

func (c Consumer) check() error {
	name := c.Container + c.Host
	if (c.Container == "") == (c.Host == "") || strings.Contains(name, "/") {
		return ErrConsumer
	}
	return nil
}

// Delivery is a consumer and the exec bound to it: a container's runs argv as agent there; a host consumer has none.
type Delivery struct {
	Consumer Consumer
	Exec     func(argv []string, stdin []byte) error
}

func (d Delivery) check() error {
	if err := d.Consumer.check(); err != nil {
		return err
	}
	if (d.Consumer.Container != "") != (d.Exec != nil) {
		return ErrConsumer
	}
	return nil
}

// State is where a material is in its consumer's lifecycle.
type State string

const (
	Prepared  State = "prepared"  // recorded, not yet confirmed delivered
	Delivered State = "delivered" // confirmed in the consumer
	Retiring  State = "retiring"  // replaced, awaiting revocation
)

// Material is one issued token. Account and Target are its slot, one grant position (GitHub: an installation's owner
// and ID); the JSON names are the GitHub record's, which older brokers read.
type Material struct {
	Account   string `json:"account"`
	Target    int64  `json:"installation"`
	Token     string `json:"token"`
	ExpiresAt string `json:"expires_at"` // RFC 3339; one that does not parse is never confirmed expired
	Revision  int    `json:"revision,omitempty"`
	State     State  `json:"state,omitempty"` // "": an entry an older broker wrote; it recorded only tokens it delivered
}

// Expired is whether m's expiry is confirmed at now.
func (m Material) Expired(now time.Time) bool {
	expires, err := time.Parse(time.RFC3339, m.ExpiresAt)
	return err == nil && !now.Before(expires)
}

// Record is the broker's host copy of one consumer's materials. Its desired revision is its materials' highest,
// its delivered revision the highest delivered one; neither is stored apart.
type Record struct {
	Subject string            `json:"app"` // the identity issued under (GitHub: the App)
	Scope   map[string]string `json:"scope,omitempty"`
	Tokens  []Material        `json:"tokens"`
}

// Pending is one backlog entry: a token whose record entry was released before its revocation was confirmed.
type Pending struct {
	Container string `json:"container"` // the consumer's ID
	Account   string `json:"account"`
	Token     string `json:"token"`
	ExpiresAt string `json:"expires_at"`
}

// Outcome is what a revocation request confirmed.
type Outcome string

const (
	Revoked        Outcome = "revoked"
	AlreadyInvalid Outcome = "already-invalid" // as good as revoked
	Retryable      Outcome = "retryable"
	Unsupported    Outcome = "unsupported" // the provider cannot revoke: kept until expiry
)

// Revocation is one revocation request's answer; Unreachable stops a backlog retry run.
type Revocation struct {
	Outcome     Outcome
	Err         error
	Unreachable bool
}

// File is where a material is written in a container: Dir/Name under docker.SecretsDir. Replace swaps an existing
// file atomically; otherwise one already there is refused.
type File struct {
	Dir, Name string
	Replace   bool
}

// Provider issues and revokes one token kind.
type Provider interface {
	Kind() Kind
	AuditKind() string                                         // the audit log's kind for its tokens
	Begin(ctx context.Context, subject string) (Issuer, error) // authenticates once for a run of issues; its errors hold no secret
	File(m Material) (File, error)                             // where m goes in a container, validating m
	Revoke(ctx context.Context, m Material) Revocation
	Now() time.Time
}

// Issuer issues materials under one authentication.
type Issuer interface {
	Targets(ctx context.Context) ([]Material, error) // the slots a new grant covers, Token empty
	Issue(ctx context.Context, scope map[string]string, target Material) (Material, error)
	Secret() string // the request credential, for redaction
}

// Shared is a provider whose material other consumers share (an account's): releasing or replacing one consumer's
// copy never revokes it; the account's own revision stays the provider's.
type Shared interface{ SharedAccount() bool }

// Describer adds to a newly recorded material's audit what it reaches, after its record write.
type Describer interface {
	Describe(ctx context.Context, m Material) *audit.Repositories
}
