package broker

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"ensemble/broker/internal/audit"
	"ensemble/broker/internal/credential"
	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/launcher"
	"ensemble/broker/internal/protocol"
)

// Each Codex container's grant record: its one slot's fingerprint, expiry, revision and delivery state, never a token.
const (
	codexGrantsService = "ensemble-codex-grants"
	codexSlot          = "codex"
)

var errCodexFile = errors.New("Codex material is written by its session")

// lifecycle is c's grant lifecycle. Codex is a shared provider: releasing or replacing a container's grant only
// withdraws it, so the backlog is never written. failed is the calling command's failure code.
func (c Codex) lifecycle(failed protocol.Code) credential.Lifecycle {
	return credential.Lifecycle{
		Provider: codexProvider{c, failed},
		Store:    c.grants,
		NotFound: ErrNotFound,
		Records:  codexGrantsService,
		Backlog:  [2]string{codexGrantsService, "backlog"},
		Lock:     c.grantsLock,
	}
}

func codexDelivery(container string, exec func([]string, []byte) error) credential.Delivery {
	return credential.Delivery{Consumer: credential.Consumer{Container: container}, Exec: exec}
}

// Ready checks, under the account lock, that the login is usable, renewing it when due.
func (c Codex) Ready(ctx context.Context) error {
	s, err := c.begin(ctx, protocol.StartFailed)
	if err != nil {
		return err
	}
	_, err = s.Issue(ctx, nil, credential.Material{Account: codexSlot})
	if endErr := s.End(err); endErr != nil {
		err = errors.Join(err, endErr)
	}
	return err
}

// Inject writes the access-only login into container through exec, recording its grant, and returns its expiry.
func (c Codex) Inject(ctx context.Context, container string, exec func([]string, []byte) error) (string, error) {
	expiries, err := c.lifecycle(protocol.StartFailed).Acquire(ctx, codexDelivery(container, exec), codexService, nil)
	return codexExpiry(container, expiries, err)
}

// Refresh writes the current access-only login into container, renewing the account only when no consumer has done so
// and container does not already hold it undelivered, then publishes its expiry. A container with no grant (an older
// broker started it) is granted one; one whose write never reached it keeps none.
func (c Codex) Refresh(ctx context.Context, container string, exec func([]string, []byte) error, publish func(string) error) (string, error) {
	d, written := codexDelivery(container, exec), false
	d.Publish = func(m credential.Material) error {
		written = true
		if publish == nil {
			return nil
		}
		return publish(m.ExpiresAt)
	}
	l := c.lifecycle(protocol.RefreshFailed)
	expiries, err := l.Refresh(ctx, d, nil)
	if errors.Is(err, credential.ErrNoRecord) {
		if expiries, err = l.Acquire(ctx, d, codexService, nil); err != nil && !written {
			_ = l.Release(ctx, d.Consumer)
		}
	}
	return codexExpiry(container, expiries, err)
}

// Release withdraws container's grant; the login and every other container's grant stay as they are.
func (c Codex) Release(ctx context.Context, container string) error {
	return grantsError(container, c.lifecycle(protocol.CloseFailed).Release(ctx, credential.Consumer{Container: container}))
}

func codexExpiry(container string, expiries []protocol.Expiry, err error) (string, error) {
	if err != nil {
		return "", grantsError(container, err)
	}
	if len(expiries) != 1 {
		return "", protocol.New(protocol.RefreshFailed, "No Codex credential was granted to "+container+".")
	}
	return expiries[0].ExpiresAt, nil
}

// grantsError is the protocol's account of a lifecycle error about container's Codex grant.
func grantsError(container string, err error) error {
	var e *protocol.Error
	switch {
	case err == nil:
		return nil
	case errors.As(err, &e):
		return e
	case errors.Is(err, credential.ErrLock):
		return protocol.New(protocol.KeychainFailed, "Taking the Codex grant lock ~/.ensemble/docker/codex-grants.lock failed.")
	case errors.Is(err, credential.ErrStore):
		return protocol.New(protocol.KeychainFailed, "Keychain access to the Codex grants failed.")
	case errors.Is(err, credential.ErrReleased):
		return protocol.New(protocol.RefreshFailed, container+" was closed during the refresh.")
	case errors.Is(err, docker.ErrUnreachable):
		return fmt.Errorf("Codex credential injection: %w", docker.ErrUnreachable)
	}
	return err
}

// codexProvider is Codex's side of the lifecycle: one shared account, renewed natively, never revoked.
type codexProvider struct {
	c      Codex
	failed protocol.Code
}

func (p codexProvider) Kind() credential.Kind { return credential.Codex }
func (p codexProvider) AuditKind() string     { return audit.CodexAccessToken }
func (p codexProvider) Now() time.Time        { return p.c.now() }
func (p codexProvider) SharedAccount() bool   { return true }

func (p codexProvider) File(credential.Material) (credential.File, error) {
	return credential.File{}, errCodexFile
}

func (p codexProvider) Revoke(context.Context, credential.Material) credential.Revocation {
	return credential.Revocation{Outcome: credential.Unsupported}
}

func (p codexProvider) Begin(ctx context.Context, _ string) (credential.Issuer, error) {
	return p.c.begin(ctx, p.failed)
}

// codexSession is one run under the account lock, from Begin to End: Issue reads the login, renewing it when due,
// Write delivers its access-only carrier, End records the account revision and unlocks.
type codexSession struct {
	c                Codex
	failed           protocol.Code
	unlock           func()
	before, revision codexRevision
	pending          bool // the revision is written at End
	issueFailed      bool
	carrier          []byte
	fingerprint      string
}

func (c Codex) begin(ctx context.Context, failed protocol.Code) (*codexSession, error) {
	if _, err := c.native.Verify(); err != nil {
		return nil, protocol.New(protocol.LoginRequired, "Pinned Codex unavailable; run "+codexRepair(err))
	}
	before, err := c.readRevision()
	if err != nil {
		return nil, c.refreshError(err)
	}
	unlock, err := c.native.Lock(ctx)
	if err != nil {
		return nil, protocol.New(protocol.LoginRequired, "Cannot lock Codex login; retry "+codexLoginCommand)
	}
	revision, err := c.readRevision()
	if err != nil {
		unlock()
		return nil, c.refreshError(err)
	}
	return &codexSession{c: c, failed: failed, unlock: unlock, before: before, revision: revision}, nil
}

func (s *codexSession) Secret() string { return "" }

func (s *codexSession) Targets(context.Context) ([]credential.Material, error) {
	return []credential.Material{{Account: codexSlot}}, nil
}

// codexCarrier is the login as a container gets it: ChatGPT mode, no refresh token.
func codexCarrier(auth codexAuthDocument) ([]byte, error) {
	auth.Mode = "chatgpt"
	auth.Tokens.Refresh = ""
	return json.Marshal(auth)
}

func codexFingerprint(carrier []byte) string {
	sum := sha256.Sum256(carrier)
	return "sha256:" + hex.EncodeToString(sum[:])
}

// Issue reads the login and renews it when due, unless a concurrent run completed a renewal meanwhile, the last
// renewal is being repaired, or target, the consumer's newest grant, holds the current login undelivered.
func (s *codexSession) Issue(ctx context.Context, _ map[string]string, target credential.Material) (_ credential.Material, err error) {
	defer func() { s.issueFailed = s.issueFailed || err != nil }()
	c := s.c
	if c.trusted() != nil {
		return credential.Material{}, protocol.New(protocol.LoginRequired, "Codex login unavailable; run "+codexLoginCommand)
	}
	data, err := c.keychain.Secret(codexService, codexAccount)
	defer func() { erase(data) }()
	if err != nil {
		return credential.Material{}, protocol.New(protocol.LoginRequired, "Codex login unavailable; run "+codexLoginCommand)
	}
	expires, err := parseCodexAuth(data)
	if err != nil {
		return credential.Material{}, protocol.New(protocol.LoginRequired, "Invalid Codex login; run "+codexLoginCommand)
	}
	var auth codexAuthDocument
	if json.Unmarshal(data, &auth) != nil {
		return credential.Material{}, protocol.New(protocol.LoginRequired, "Invalid Codex login; run "+codexLoginCommand)
	}
	stamp, _ := time.Parse(time.RFC3339, auth.LastRefresh)
	if stamp.After(c.now()) {
		return credential.Material{}, protocol.New(protocol.LoginRequired, "Invalid Codex login timestamp; run "+codexLoginCommand)
	}
	if err := s.carry(auth); err != nil {
		return credential.Material{}, err
	}
	reused := s.revision.completedSince(s.before)
	if reused && (s.revision.State == "failed" || !c.now().Before(expires)) {
		return credential.Material{}, c.refreshError(errors.New("Concurrent Codex authentication renewal failed; retry or run " + codexLoginCommand))
	}
	repair := (s.revision.State == "active" || s.revision.State == "failed") && c.now().Before(expires) && c.now().Sub(stamp) < time.Minute
	held := target.State == credential.Prepared && target.Token == s.fingerprint && c.now().Before(expires)
	if !reused && !repair && !held && !expires.After(c.now().Add(codexRefreshAhead)) {
		if s.revision, err = c.beginRevision(); err != nil {
			return credential.Material{}, c.refreshError(err)
		}
		s.pending = true
		next, err := c.renew(ctx, data, auth, expires)
		if err != nil {
			return credential.Material{}, c.refreshError(err)
		}
		erase(data)
		data = next
		expires, _ = parseCodexAuth(data)
		_ = json.Unmarshal(data, &auth)
		if err := s.carry(auth); err != nil {
			return credential.Material{}, err
		}
	} else if repair {
		s.pending = true
	}
	if !c.now().Before(expires) {
		return credential.Material{}, protocol.New(protocol.LoginRequired, "Codex authentication expired; run "+codexLoginCommand)
	}
	return credential.Material{Account: codexSlot, Token: s.fingerprint, ExpiresAt: expires.Format(time.RFC3339)}, nil
}

// carry keeps auth's carrier and fingerprint for Write.
func (s *codexSession) carry(auth codexAuthDocument) error {
	carrier, err := codexCarrier(auth)
	if err != nil {
		return protocol.New(s.failed, "Cannot encode Codex container credentials")
	}
	erase(s.carrier)
	s.carrier, s.fingerprint = carrier, codexFingerprint(carrier)
	return nil
}

// Write gives the container the carrier Issue read, through bootstrap.py's codex-auth writer.
func (s *codexSession) Write(exec func([]string, []byte) error, m credential.Material) error {
	if m.Token != s.fingerprint || len(s.carrier) == 0 {
		return protocol.New(s.failed, "Codex container credentials are not this run's")
	}
	err := exec([]string{launcher.Python, "-I", launcher.Bootstrap, "codex-auth"}, s.carrier)
	if errors.Is(err, docker.ErrUnreachable) {
		return docker.ErrUnreachable
	}
	if err != nil {
		e := protocol.New(s.failed, "Writing Codex credentials into the container failed")
		e.Cause = writerCause(err)
		return e
	}
	return nil
}

// End records a renewal or repair complete, or failed when Issue failed: a later delivery or publication failure
// stays in its consumer's grant, never failing a sibling's run queued on the lock. It then unlocks.
func (s *codexSession) End(error) error {
	defer s.unlock()
	erase(s.carrier)
	s.carrier = nil
	if !s.pending {
		return nil
	}
	s.revision.State = "complete"
	if s.issueFailed {
		s.revision.State = "failed"
	}
	if err := s.c.writeRevision(s.revision); err != nil {
		return s.c.refreshError(err)
	}
	return nil
}
