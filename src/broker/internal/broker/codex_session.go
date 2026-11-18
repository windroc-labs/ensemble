package broker

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"ensemble/broker/internal/credential"
	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/launcher"
	"ensemble/broker/internal/protocol"
)

// Ready checks, under the account lock, that the login is usable, renewing it when due.
func (c Codex) Ready(ctx context.Context) error {
	s, err := c.begin(ctx, protocol.StartFailed)
	if err != nil {
		return err
	}
	_, err = s.read(ctx)
	if endErr := s.end(); endErr != nil {
		err = errors.Join(err, endErr)
	}
	return err
}

// Inject prepares an access-only login and keeps the account locked until the caller finishes delivery.
func (c Codex) Inject(ctx context.Context) (launcher.CodexInjection, error) {
	s, err := c.begin(ctx, protocol.StartFailed)
	if err != nil {
		return launcher.CodexInjection{}, codexError(err)
	}
	expires, err := s.read(ctx)
	if err != nil {
		return launcher.CodexInjection{}, codexError(errors.Join(err, s.end()))
	}
	return launcher.CodexInjection{
		Command: credential.Command{Argv: []string{launcher.Python, "-I", launcher.Bootstrap, "codex-auth"}, Stdin: s.carrier},
		Expiry:  expires.Format(time.RFC3339),
		Finish:  s.end,
	}, nil
}

// Refresh writes the current access-only login into a container through exec, then publishes its expiry.
func (c Codex) Refresh(ctx context.Context, exec func([]string, []byte) error, publish func(string) error) (string, error) {
	return c.deliver(ctx, protocol.RefreshFailed, exec, publish)
}

// deliver writes the current access-only login into a container under the account lock, renewing it only when due
// (codexRefreshAhead) and no concurrent run renewed it since this run began (revision.json); publish (nil: none) gets
// its expiry after the write. failed is the calling command's failure code.
func (c Codex) deliver(ctx context.Context, failed protocol.Code, exec func([]string, []byte) error, publish func(string) error) (_ string, err error) {
	s, err := c.begin(ctx, failed)
	if err != nil {
		return "", codexError(err)
	}
	defer func() {
		if endErr := s.end(); endErr != nil {
			err = errors.Join(err, endErr)
		}
		err = codexError(err)
	}()
	expires, err := s.read(ctx)
	if err != nil {
		return "", err
	}
	if err := s.write(exec); err != nil {
		return "", err
	}
	expiry := expires.Format(time.RFC3339)
	if publish != nil {
		if err := publish(expiry); err != nil {
			return "", err
		}
	}
	return expiry, nil
}

// codexError is the protocol's account of a Codex delivery error: a protocol error as it is, an unreachable Docker
// daemon wrapped, anything else opaque.
func codexError(err error) error {
	var e *protocol.Error
	switch {
	case err == nil:
		return nil
	case errors.As(err, &e):
		return e
	case errors.Is(err, docker.ErrUnreachable):
		return fmt.Errorf("Codex credential injection: %w", docker.ErrUnreachable)
	}
	return err
}

// codexSession is one run under the account lock, from begin to end: read reads the login, renewing it when due,
// write delivers its access-only carrier, end records the account revision and unlocks.
type codexSession struct {
	c                Codex
	failed           protocol.Code
	unlock           func()
	before, revision codexRevision
	pending          bool // the revision is written at end
	readFailed       bool
	carrier          []byte
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

// codexCarrier is the login as a container gets it: ChatGPT mode, no refresh token.
func codexCarrier(auth codexAuthDocument) ([]byte, error) {
	auth.Mode = "chatgpt"
	auth.Tokens.Refresh = ""
	return json.Marshal(auth)
}

// read reads the login and renews it when due, unless a concurrent run completed a renewal meanwhile or the last
// renewal is being repaired; it returns the login's expiry.
func (s *codexSession) read(ctx context.Context) (_ time.Time, err error) {
	defer func() { s.readFailed = s.readFailed || err != nil }()
	c := s.c
	if c.trusted() != nil {
		return time.Time{}, protocol.New(protocol.LoginRequired, "Codex login unavailable; run "+c.loginRepair())
	}
	data, err := c.keychain.Secret(codexService, codexAccount)
	defer func() { erase(data) }()
	if err != nil {
		return time.Time{}, protocol.New(protocol.LoginRequired, "Codex login unavailable; run "+codexLoginCommand)
	}
	expires, err := parseCodexAuth(data)
	if err != nil {
		return time.Time{}, protocol.New(protocol.LoginRequired, "Invalid Codex login; run "+codexLoginCommand)
	}
	var auth codexAuthDocument
	if json.Unmarshal(data, &auth) != nil {
		return time.Time{}, protocol.New(protocol.LoginRequired, "Invalid Codex login; run "+codexLoginCommand)
	}
	stamp, _ := time.Parse(time.RFC3339, auth.LastRefresh)
	if stamp.After(c.now()) {
		return time.Time{}, protocol.New(protocol.LoginRequired, "Invalid Codex login timestamp; run "+codexLoginCommand)
	}
	if err := s.carry(auth); err != nil {
		return time.Time{}, err
	}
	reused := s.revision.completedSince(s.before)
	if reused && (s.revision.State == "failed" || !c.now().Before(expires)) {
		return time.Time{}, c.refreshError(errors.New("Concurrent Codex authentication renewal failed; retry or run " + codexLoginCommand))
	}
	repair := (s.revision.State == "active" || s.revision.State == "failed") && c.now().Before(expires) && c.now().Sub(stamp) < time.Minute
	if !reused && !repair && !expires.After(c.now().Add(codexRefreshAhead)) {
		if s.revision, err = c.beginRevision(); err != nil {
			return time.Time{}, c.refreshError(err)
		}
		s.pending = true
		next, err := c.renew(ctx, data, auth, expires)
		if err != nil {
			return time.Time{}, c.refreshError(err)
		}
		erase(data)
		data = next
		expires, _ = parseCodexAuth(data)
		_ = json.Unmarshal(data, &auth)
		if err := s.carry(auth); err != nil {
			return time.Time{}, err
		}
	} else if repair {
		s.pending = true
	}
	if !c.now().Before(expires) {
		return time.Time{}, protocol.New(protocol.LoginRequired, "Codex authentication expired; run "+codexLoginCommand)
	}
	return expires, nil
}

// carry keeps auth's carrier for write.
func (s *codexSession) carry(auth codexAuthDocument) error {
	carrier, err := codexCarrier(auth)
	if err != nil {
		return protocol.New(s.failed, "Cannot encode Codex container credentials")
	}
	erase(s.carrier)
	s.carrier = carrier
	return nil
}

// write gives the container the carrier read got, through bootstrap.py's codex-auth writer.
func (s *codexSession) write(exec func([]string, []byte) error) error {
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

// end records a renewal or repair complete, or failed when read failed: a later delivery or publication failure
// fails only its own run, never a sibling's queued on the lock. It then unlocks.
func (s *codexSession) end() error {
	defer s.unlock()
	erase(s.carrier)
	s.carrier = nil
	if !s.pending {
		return nil
	}
	s.revision.State = "complete"
	if s.readFailed {
		s.revision.State = "failed"
	}
	if err := s.c.writeRevision(s.revision); err != nil {
		return s.c.refreshError(err)
	}
	return nil
}
