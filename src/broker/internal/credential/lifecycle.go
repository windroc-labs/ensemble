package credential

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"slices"
	"strings"
	"time"

	"ensemble/broker/internal/audit"
	"ensemble/broker/internal/protocol"
)

// The lifecycle's errors; ErrLock and ErrStore carry no lock or store detail, which may name what they hold.
var (
	ErrLock     = errors.New("taking the credential lock failed")
	ErrStore    = errors.New("credential store access failed")
	ErrNoRecord = errors.New("no credential record")
	ErrReleased = errors.New("the credential record was released meanwhile")
)

// UnrevokedError is a retirement that left material neither confirmed revoked nor queued; what was recorded stays
// recorded for a later Release, and the rest expires. Cause (nil: none) is the failure the retirement rolled back.
type UnrevokedError struct {
	Consumer string
	Cause    error
}

func (e *UnrevokedError) Error() string {
	return "the credentials of " + e.Consumer + " could not be revoked"
}

func (e *UnrevokedError) Unwrap() error { return e.Cause }

// rolledBack is err, or the unrevoked error rollback reported with err as its cause: live tokens are what the
// caller must act on.
func rolledBack(err, rollback error) error {
	var unrevoked *UnrevokedError
	if errors.As(rollback, &unrevoked) {
		return &UnrevokedError{unrevoked.Consumer, err}
	}
	return err
}

// Store holds broker-only items; NotFound is its error for an absent one.
type Store interface {
	Secret(service, account string) ([]byte, error)
	PutSecret(service, account string, data []byte) error
	DeleteSecret(service, account string) error // an absent item is success
}

// Lifecycle issues, renews and retires one provider's tokens for its consumers. Each consumer's record (service
// Records, account Consumer.ID()) or the backlog (Backlog's service and account) holds every token issued until it
// is confirmed revoked or expired: a material leaves its record only for confirmed revocation, confirmed expiry or a
// confirmed backlog write. Lock guards each read-modify-write of either item, never a provider request. Audit (nil:
// none) gets each token's outcome, never its value. Acquire, Refresh, Release and Retry refuse a provider whose kind
// is not renewable (ErrStatic); Pending, which asks the provider nothing, only reads the backlog. An Issuer that is a
// Writer delivers its own materials, one that is an Ender ends its run; a Delivery's Publish follows each delivery. A
// shared provider's material that failed delivery or publication stays recorded prepared: there is nothing to revoke.
// A failed run never rolls back a material the record already held (rollback).
type Lifecycle struct {
	Provider Provider
	Store    Store
	NotFound error
	Records  string
	Backlog  [2]string
	Lock     func() (unlock func(), err error)
	Audit    *audit.Recorder
}

func (l Lifecycle) ready() error {
	if l.Provider == nil {
		return errors.New("no credential provider")
	}
	return Renewable(l.Provider.Kind())
}

func (l Lifecycle) shared() bool {
	s, ok := l.Provider.(Shared)
	return ok && s.SharedAccount()
}

func erase(b []byte) {
	for i := range b {
		b[i] = 0
	}
}

// redacted is an error whose text had a secret replaced; Unwrap keeps its class.
type redacted struct {
	text string
	err  error
}

func (r *redacted) Error() string { return r.text }
func (r *redacted) Unwrap() error { return r.err }

// redactErr is err with secrets replaced in its text: a protocol error as a redacted copy, never changed in place.
func redactErr(err error, secrets ...string) error {
	if err == nil {
		return nil
	}
	var pe *protocol.Error
	if errors.As(err, &pe) {
		c := *pe
		c.Message, c.Cause = Redact(c.Message, secrets...), Redact(c.Cause, secrets...)
		return &c
	}
	if text := Redact(err.Error(), secrets...); text != err.Error() {
		return &redacted{text, err}
	}
	return err
}

// note gives the audit recorder m's outcome for consumer id, keyed by a hash of its token, its reason redacted.
func (l Lifecycle) note(id, result, reason string, m Material, repos *audit.Repositories, secrets ...string) {
	if l.Audit == nil {
		return
	}
	sum := sha256.Sum256([]byte(m.Token))
	expires := m.ExpiresAt
	l.Audit.Add(id, audit.Credential{Kind: l.Provider.AuditKind(), Result: result, Reason: Redact(reason, append(secrets, m.Token)...),
		ExpiresAt: &expires, Account: m.Account, Installation: m.Target, Repos: repos}, hex.EncodeToString(sum[:]))
}

// unlisted is a new token's description when it could not be recorded, so was never described.
var unlisted = audit.Repositories{Error: "not listed: the token could not be recorded"}

// noteUnrecorded notes a new token that could not be recorded.
func (l Lifecycle) noteUnrecorded(id, result string, m Material) {
	var repos *audit.Repositories
	if _, ok := l.Provider.(Describer); ok {
		repos = &unlisted
	}
	l.note(id, result, "", m, repos)
}

// describe notes a recorded token with what it reaches. Only after the record write: the provider's requests must
// not widen the window in which a token is untracked.
func (l Lifecycle) describe(ctx context.Context, id, result string, m Material, secret string) {
	if l.Audit == nil {
		return
	}
	var repos *audit.Repositories
	if d, ok := l.Provider.(Describer); ok {
		if repos = d.Describe(ctx, m); repos != nil {
			repos.Error = Redact(repos.Error, m.Token, secret)
		}
	}
	l.note(id, result, "", m, repos)
}

// revoke asks the provider to revoke m; reason is its failure, redacted.
func (l Lifecycle) revoke(ctx context.Context, m Material) (r Revocation, reason string) {
	r = l.Provider.Revoke(ctx, m)
	switch {
	case r.Outcome == Unsupported:
		reason = "revocation unsupported"
	case r.Err != nil:
		reason = Redact(r.Err.Error(), m.Token)
	default:
		reason = "revocation failed"
	}
	return r, reason
}

func (r Revocation) confirmed() bool { return r.Outcome == Revoked || r.Outcome == AlreadyInvalid }

func (l Lifecycle) locked(f func() error) error {
	unlock, err := l.Lock()
	if err != nil {
		return ErrLock
	}
	defer unlock()
	return f()
}

func (l Lifecycle) readRecord(id string) (Record, bool, error) {
	var rec Record
	data, err := l.Store.Secret(l.Records, id)
	defer erase(data)
	switch {
	case errors.Is(err, l.NotFound):
		return rec, false, nil
	case err != nil:
		return rec, false, ErrStore
	}
	if json.Unmarshal(data, &rec) != nil {
		return rec, true, ErrStore
	}
	return rec, true, nil
}

func (l Lifecycle) writeRecord(id string, rec Record) error {
	var err error
	if len(rec.Tokens) == 0 {
		err = l.Store.DeleteSecret(l.Records, id)
	} else {
		data, _ := json.Marshal(rec)
		err = l.Store.PutSecret(l.Records, id, data)
		erase(data)
	}
	if err != nil {
		return ErrStore
	}
	return nil
}

// editRecord changes id's record under the lock; f gets whether it exists, and a missing one is never created
// unless f adds tokens and create is set.
func (l Lifecycle) editRecord(id string, create bool, f func(rec *Record, found bool)) (bool, error) {
	var found bool
	err := l.locked(func() error {
		rec, ok, err := l.readRecord(id)
		if err != nil {
			return err
		}
		found = ok
		f(&rec, ok)
		if !ok && !create {
			return nil
		}
		return l.writeRecord(id, rec)
	})
	return found, err
}

func (l Lifecycle) readBacklog() ([]Pending, error) {
	var list []Pending
	data, err := l.Store.Secret(l.Backlog[0], l.Backlog[1])
	defer erase(data)
	switch {
	case errors.Is(err, l.NotFound):
		return nil, nil
	case err != nil:
		return nil, ErrStore
	}
	if json.Unmarshal(data, &list) != nil {
		return nil, ErrStore
	}
	return list, nil
}

// editBacklog changes the backlog; the caller holds the lock.
func (l Lifecycle) editBacklog(f func([]Pending) []Pending) error {
	list, err := l.readBacklog()
	if err != nil {
		return err
	}
	list = f(list)
	if len(list) == 0 {
		err = l.Store.DeleteSecret(l.Backlog[0], l.Backlog[1])
	} else {
		data, _ := json.Marshal(list)
		err = l.Store.PutSecret(l.Backlog[0], l.Backlog[1], data)
		erase(data)
	}
	if err != nil {
		return ErrStore
	}
	return nil
}

func tokenIn(entries []Material) func(string) bool {
	return func(t string) bool {
		return slices.ContainsFunc(entries, func(m Material) bool { return m.Token == t })
	}
}

func pending(id string, m Material) Pending { return Pending{id, m.Account, m.Token, m.ExpiresAt} }

func (p Pending) material() Material {
	return Material{Account: p.Account, Token: p.Token, ExpiresAt: p.ExpiresAt}
}

// slot is m's grant position: its account and target.
func (m Material) slot() [2]any { return [2]any{m.Account, m.Target} }

// recording is how put recorded a material: not at all (its record could not be read, or was gone), as a new entry,
// or by re-marking one the record already held.
type recording int

const (
	unrecorded recording = iota
	appended
	held
)

// put records m, prepared, in rec at the next revision; a token rec already holds (a shared account's, not renewed
// meanwhile) turns prepared again, keeping its revision, rather than recorded twice.
func put(rec *Record, m *Material) recording {
	if i := slices.IndexFunc(rec.Tokens, func(t Material) bool { return t.Token == m.Token }); i >= 0 {
		rec.Tokens[i].State, m.Revision = Prepared, rec.Tokens[i].Revision
		return held
	}
	m.Revision = desired(rec) + 1
	rec.Tokens = append(rec.Tokens, *m)
	return appended
}

// rollback is whether a failed run retires a material it issued, recorded as r. Never one the record already held,
// which stays usable. A shared provider's only when the run appended it: a failed write leaves the rest of the record
// as it was, and nothing needs revoking. Any other provider's even unrecorded: a fresh token is live at the provider.
func (l Lifecycle) rollback(r recording) bool {
	if l.shared() {
		return r == appended
	}
	return r != held
}

// desired is rec's highest revision.
func desired(rec *Record) int {
	n := 0
	for _, m := range rec.Tokens {
		n = max(n, m.Revision)
	}
	return n
}

// discard retires entries (the whole record when whole) through a durable backlog transfer, or direct revocation if
// that transfer fails. The original record keeps every token not confirmed revoked, expired or queued. A shared
// provider's entries are only withdrawn.
func (l Lifecycle) discard(ctx context.Context, id string, entries []Material, whole bool) error {
	if l.shared() {
		return l.withdraw(id, entries, whole)
	}
	var backlogErr, recordErr error
	ran := false
	err := l.locked(func() error {
		ran = true
		if whole {
			entries, recordErr = l.withRecord(id, entries)
		}
		backlogErr = l.editBacklog(func(list []Pending) []Pending {
			for _, m := range entries {
				list = append(list, pending(id, m))
			}
			return list
		})
		if backlogErr != nil {
			return nil // direct revocation must finish before the original record can change
		}
		for _, m := range entries {
			l.note(id, audit.Pending, "queued for revocation", m, nil)
		}
		if whole {
			if recordErr != nil {
				return recordErr // keep unknown original material; explicit entries are safely queued
			}
			if err := l.Store.DeleteSecret(l.Records, id); err != nil {
				return ErrStore
			}
			return nil
		}
		rec, found, err := l.readRecord(id)
		if err != nil || !found {
			return err
		}
		rec.Tokens = slices.DeleteFunc(rec.Tokens, func(m Material) bool { return tokenIn(entries)(m.Token) })
		return l.writeRecord(id, rec)
	})
	if ran && backlogErr == nil {
		_, _ = l.Retry(ctx)
		return err
	}
	if !ran && whole {
		entries, _ = l.withRecord(id, entries)
	}
	if revokeErr := l.revokeDirect(ctx, id, entries, ran); revokeErr != nil {
		return revokeErr
	}
	if recordErr != nil {
		return &UnrevokedError{Consumer: id, Cause: recordErr} // the unread record keeps whatever it holds
	}
	return err
}

// withRecord is entries plus every other material of id's record; err is the record read's.
func (l Lifecycle) withRecord(id string, entries []Material) ([]Material, error) {
	rec, found, err := l.readRecord(id)
	if err == nil && found {
		in := tokenIn(entries)
		entries = append(slices.Clone(entries), slices.DeleteFunc(rec.Tokens, func(m Material) bool { return in(m.Token) })...)
	}
	return entries, err
}

// revokeDirect is discard's fallback when the backlog transfer failed (locked) or the lock did: it revokes each
// entry itself and takes the confirmed ones out of the record; without the lock it queues the rest. A token left
// neither confirmed nor queued is an *UnrevokedError, whatever else failed.
func (l Lifecycle) revokeDirect(ctx context.Context, id string, entries []Material, locked bool) error {
	type miss struct {
		entry  Material
		reason string
	}
	var left []Pending
	var missed []miss
	var confirmed []Material
	now := l.Provider.Now()
	for _, m := range entries {
		if m.Expired(now) {
			confirmed = append(confirmed, m)
			l.note(id, audit.ExpiredUnrevoked, "", m, nil)
			continue
		}
		if r, reason := l.revoke(ctx, m); !r.confirmed() {
			left = append(left, pending(id, m))
			missed = append(missed, miss{m, reason})
		} else {
			confirmed = append(confirmed, m)
			l.note(id, audit.Revoked, "", m, nil)
		}
	}
	queued := !locked && (len(left) == 0 || l.editBacklog(func(list []Pending) []Pending { return append(list, left...) }) == nil)
	for _, m := range missed {
		if !queued {
			m.reason += "; not queued"
		}
		l.note(id, audit.Pending, m.reason, m.entry, nil)
	}
	if !locked {
		if !queued {
			return &UnrevokedError{Consumer: id} // the record keeps them tracked
		}
		l.untrack(id, entries)
		return nil
	}
	var removeErr error
	if len(confirmed) > 0 {
		_, removeErr = l.editRecord(id, false, func(rec *Record, _ bool) {
			in := tokenIn(confirmed)
			rec.Tokens = slices.DeleteFunc(rec.Tokens, func(m Material) bool { return in(m.Token) })
		})
	}
	if len(left) > 0 {
		return &UnrevokedError{Consumer: id, Cause: removeErr}
	}
	return removeErr
}

// untrack takes entries, revoked or queued, out of id's record without the lock: best effort, as the lock is what
// failed.
func (l Lifecycle) untrack(id string, entries []Material) {
	if rec, found, err := l.readRecord(id); err == nil && found {
		in := tokenIn(entries)
		rec.Tokens = slices.DeleteFunc(rec.Tokens, func(m Material) bool { return in(m.Token) })
		_ = l.writeRecord(id, rec)
	}
}

// withdraw drops a shared provider's entries (the whole record when whole) from id's record without revoking them:
// other consumers hold the same material.
func (l Lifecycle) withdraw(id string, entries []Material, whole bool) error {
	gone := slices.Clone(entries)
	err := l.locked(func() error {
		rec, found, err := l.readRecord(id)
		if err != nil || !found {
			return err
		}
		in := tokenIn(entries)
		rec.Tokens = slices.DeleteFunc(rec.Tokens, func(m Material) bool {
			drop := whole || in(m.Token)
			if drop && !in(m.Token) {
				gone = append(gone, m)
			}
			return drop
		})
		return l.writeRecord(id, rec)
	})
	if err != nil {
		return err
	}
	for _, m := range gone {
		l.note(id, audit.Withdrawn, "", m, nil)
	}
	return nil
}

// deliver writes m into d's consumer, through issuer when it is a Writer, then publishes it; a host consumer's record
// is its copy, so it has nothing to write.
func (l Lifecycle) deliver(d Delivery, issuer Issuer, m Material) error {
	if err := l.write(d, issuer, m); err != nil {
		return err
	}
	if d.Publish != nil {
		return d.Publish(m)
	}
	return nil
}

func (l Lifecycle) write(d Delivery, issuer Issuer, m Material) error {
	if d.Exec == nil {
		return nil
	}
	if w, ok := issuer.(Writer); ok {
		return w.Write(d.Exec, m)
	}
	f, err := l.Provider.File(m)
	if err != nil {
		return err
	}
	token := []byte(m.Token)
	defer erase(token)
	return Deliver(d.Exec, f, token)
}

// end ends issuer's run, when it is an Ender, joining End's error to *err.
func end(issuer Issuer, err *error) {
	if e, ok := issuer.(Ender); ok {
		if endErr := e.End(); endErr != nil {
			*err = errors.Join(*err, endErr)
		}
	}
}

func expiries(entries []Material) []protocol.Expiry {
	out := []protocol.Expiry{}
	for _, m := range entries {
		out = append(out, protocol.Expiry{Account: m.Account, ExpiresAt: m.ExpiresAt})
	}
	slices.SortFunc(out, func(a, b protocol.Expiry) int { return strings.Compare(a.Account, b.Account) })
	return out
}

// Acquire gives d's consumer one token per target of subject, limited to scope: each is recorded prepared (put) before
// the next request, all are delivered and published, then all are marked delivered. On any failure it retires every token
// it issued, except a shared provider's that failed delivery or publication, which stay prepared.
func (l Lifecycle) Acquire(ctx context.Context, d Delivery, subject string, scope map[string]string) (_ []protocol.Expiry, err error) {
	if err := l.ready(); err != nil {
		return nil, err
	}
	if err := d.check(); err != nil {
		return nil, err
	}
	id := d.Consumer.ID()
	issuer, err := l.Provider.Begin(ctx, subject)
	if err != nil {
		return nil, err
	}
	defer end(issuer, &err)
	secret := issuer.Secret()
	targets, err := issuer.Targets(ctx)
	if err != nil {
		return nil, redactErr(err, secret)
	}
	var issued []Material
	var how []recording // issued's
	fail := func(err error) ([]protocol.Expiry, error) {
		err = redactErr(err, secret)
		var retired []Material
		for i, m := range issued {
			if l.rollback(how[i]) {
				retired = append(retired, m)
			}
		}
		if len(retired) > 0 {
			err = rolledBack(err, l.discard(ctx, id, retired, !l.shared()))
		}
		return nil, err
	}
	for _, target := range targets {
		m, err := issuer.Issue(ctx, scope, target)
		if err != nil {
			return fail(err)
		}
		m.Account, m.Target, m.State = target.Account, target.Target, Prepared
		issued, how = append(issued, m), append(how, unrecorded)
		if _, err := l.editRecord(id, true, func(rec *Record, _ bool) {
			rec.Subject, rec.Scope = subject, scope
			how[len(how)-1] = put(rec, &m)
		}); err != nil {
			l.noteUnrecorded(id, audit.Issued, m)
			return fail(err)
		}
		issued[len(issued)-1] = m
		l.describe(ctx, id, audit.Issued, m, secret)
	}
	if len(issued) == 0 {
		return expiries(nil), nil // a subject with no targets has nothing to grant
	}
	for _, m := range issued {
		if err := l.deliver(d, issuer, m); err != nil {
			if l.shared() {
				return nil, redactErr(err, secret)
			}
			return fail(err)
		}
	}
	found, err := l.editRecord(id, false, func(rec *Record, _ bool) {
		in := tokenIn(issued)
		for i := range rec.Tokens {
			if in(rec.Tokens[i].Token) {
				rec.Tokens[i].State = Delivered
			}
		}
	})
	switch {
	case err != nil:
		return fail(err)
	case !found:
		return fail(ErrReleased)
	}
	return expiries(issued), nil
}

// Refresh replaces each slot of d's consumer's record, whatever its materials' states, with a new token: recorded
// prepared, delivered and published, then marked delivered while the slot's other materials turn retiring, which are
// then retired; each is recorded as put does. It issues with the record's scope when it has one, else scope (a record an older broker wrote).
func (l Lifecycle) Refresh(ctx context.Context, d Delivery, scope map[string]string) (_ []protocol.Expiry, err error) {
	if err := l.ready(); err != nil {
		return nil, err
	}
	if err := d.check(); err != nil {
		return nil, err
	}
	id := d.Consumer.ID()
	var rec Record
	var found bool
	if err := l.locked(func() (err error) {
		rec, found, err = l.readRecord(id)
		return err
	}); err != nil {
		return nil, err
	}
	if !found {
		return nil, ErrNoRecord
	}
	if len(rec.Scope) > 0 {
		scope = rec.Scope
	}
	var slots []Material // each slot's newest material
	for _, m := range rec.Tokens {
		switch i := slices.IndexFunc(slots, func(s Material) bool { return s.slot() == m.slot() }); {
		case i < 0:
			slots = append(slots, m)
		case m.Revision >= slots[i].Revision:
			slots[i] = m
		}
	}
	issuer, err := l.Provider.Begin(ctx, rec.Subject)
	if err != nil {
		return nil, err
	}
	defer end(issuer, &err)
	secret := issuer.Secret()
	var fresh []Material
	for _, target := range slots {
		m, err := issuer.Issue(ctx, scope, target)
		if err != nil {
			return nil, redactErr(err, secret)
		}
		m.Account, m.Target, m.State = target.Account, target.Target, Prepared
		how := unrecorded
		found, err := l.editRecord(id, false, func(rec *Record, found bool) {
			if found {
				how = put(rec, &m)
			}
		})
		if err != nil || !found {
			l.noteUnrecorded(id, audit.Renewed, m)
			if err == nil {
				err = ErrReleased
			}
			if l.rollback(how) {
				err = rolledBack(err, l.discard(ctx, id, []Material{m}, false))
			}
			return nil, err
		}
		l.describe(ctx, id, audit.Renewed, m, secret)
		if err := l.deliver(d, issuer, m); err != nil {
			if err = redactErr(err, secret); l.shared() || !l.rollback(how) {
				return nil, err // the record keeps it prepared beside the slot's usable material
			}
			return nil, rolledBack(err, l.discard(ctx, id, []Material{m}, false))
		}
		var old []Material
		kept := false
		found, err = l.editRecord(id, false, func(rec *Record, _ bool) {
			if kept = slices.ContainsFunc(rec.Tokens, func(t Material) bool { return t.Token == m.Token }); !kept {
				return // another command retired the replacement meanwhile; the slot's other materials stay as they are
			}
			for i := range rec.Tokens {
				switch t := &rec.Tokens[i]; {
				case t.Token == m.Token:
					t.State = Delivered
				case t.slot() == m.slot():
					t.State = Retiring
					old = append(old, *t)
				}
			}
		})
		switch {
		case err != nil:
			return nil, err // the record still has the slot's usable material; a retry finishes this one
		case !found, !kept:
			return nil, ErrReleased
		}
		l.retire(ctx, id, old)
		fresh = append(fresh, m)
	}
	return expiries(fresh), nil
}

// retire revokes replaced materials, each confirmed one leaving the record; an unconfirmed one goes the backlog
// way. A shared provider's are only withdrawn.
func (l Lifecycle) retire(ctx context.Context, id string, old []Material) {
	if l.shared() {
		_ = l.withdraw(id, old, false)
		return
	}
	for _, o := range old {
		if r, _ := l.revoke(ctx, o); !r.confirmed() {
			_ = l.discard(ctx, id, []Material{o}, false)
			continue
		}
		l.note(id, audit.Revoked, "", o, nil)
		_, _ = l.editRecord(id, false, func(rec *Record, _ bool) {
			rec.Tokens = slices.DeleteFunc(rec.Tokens, func(t Material) bool { return t.Token == o.Token })
		})
	}
}

// Hold records m, a token the provider issued outside Acquire for the broker's own one-off use (GitHub: a prefetch
// token), prepared in c's record before it is used, so a Release of c retires it should the broker die. A failed
// record write retires m at once and returns the error.
func (l Lifecycle) Hold(ctx context.Context, c Consumer, m Material) error {
	if err := l.holdable(c); err != nil {
		return l.refused(ctx, c, m, err)
	}
	id := c.ID()
	m.State = Prepared
	if _, err := l.editRecord(id, true, func(rec *Record, _ bool) {
		m.Revision = desired(rec) + 1
		rec.Tokens = append(rec.Tokens, m)
	}); err != nil {
		l.noteUnrecorded(id, audit.Issued, m)
		return rolledBack(err, l.discard(context.WithoutCancel(ctx), id, []Material{m}, false))
	}
	l.describe(ctx, id, audit.Issued, m, "")
	return nil
}

// Retire revokes m, which Hold recorded for c: confirmed, it leaves the record; else it goes the backlog way.
func (l Lifecycle) Retire(ctx context.Context, c Consumer, m Material) {
	if err := l.holdable(c); err != nil {
		_ = l.refused(ctx, c, m, err)
		return
	}
	l.retire(ctx, c.ID(), []Material{m})
}

// errShared refuses a one-off token from a shared provider: the broker's own material is revoked, never withdrawn.
var errShared = errors.New("a shared provider's material is not held for one use")

// holdable is nil when c may hold a one-off token of l's provider.
func (l Lifecycle) holdable(c Consumer) error {
	if err := l.ready(); err != nil {
		return err
	}
	if err := c.check(); err != nil {
		return err
	}
	if l.shared() {
		return errShared
	}
	return nil
}

// refused retires m, already issued, that Hold or Retire cannot keep: the provider, if any, revokes it directly on a
// context of its own, as there is no record or backlog to keep it in. A revocation it does not confirm is an
// *UnrevokedError; else cause is returned.
func (l Lifecycle) refused(ctx context.Context, c Consumer, m Material, cause error) error {
	if l.Provider == nil {
		return &UnrevokedError{Consumer: c.ID(), Cause: cause}
	}
	rctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 30*time.Second)
	defer cancel()
	if r, reason := l.revoke(rctx, m); !r.confirmed() {
		l.note(c.ID(), audit.Pending, reason+"; not queued", m, nil)
		return &UnrevokedError{Consumer: c.ID(), Cause: cause}
	}
	l.note(c.ID(), audit.Revoked, "", m, nil)
	return cause
}

// Release retires every material of c's record, its grant: for a shared provider by withdrawal only. An
// unreadable record is kept for recovery.
func (l Lifecycle) Release(ctx context.Context, c Consumer) error {
	if err := l.ready(); err != nil {
		return err
	}
	if err := c.check(); err != nil {
		return err
	}
	id := c.ID()
	var found bool
	err := l.locked(func() (err error) {
		_, found, err = l.readRecord(id)
		return err
	})
	switch {
	case errors.Is(err, ErrLock):
		return l.discard(ctx, id, nil, true)
	case err != nil && l.shared():
		return err // a withdrawal revokes nothing, so nothing is left unrevoked
	case err != nil:
		return &UnrevokedError{Consumer: id, Cause: err}
	case !found:
		if !l.shared() {
			_, _ = l.Retry(ctx)
		}
		return nil
	}
	return l.discard(ctx, id, nil, true)
}

// listed is list as status reports it, without its tokens.
func listed(list []Pending) []protocol.Pending {
	out := []protocol.Pending{}
	for _, p := range list {
		out = append(out, protocol.Pending{Container: p.Container, Account: p.Account, ExpiresAt: p.ExpiresAt})
	}
	return out
}

// Pending projects a single backlog snapshot without locking, mutation, provider requests or audit.
func (l Lifecycle) Pending() ([]protocol.Pending, error) {
	list, err := l.readBacklog()
	if err != nil {
		return nil, err
	}
	return listed(list), nil
}

// Retry revokes the backlog: expired entries are dropped unrevoked, and the first request that gets no answer stops
// the run, keeping the rest for next time. Audit gets every entry's outcome; it keeps what it needs.
func (l Lifecycle) Retry(ctx context.Context) ([]protocol.Pending, error) {
	if err := l.ready(); err != nil {
		return nil, err
	}
	var list []Pending
	if err := l.locked(func() (err error) {
		list, err = l.readBacklog()
		return err
	}); err != nil {
		return nil, err
	}
	now := l.Provider.Now()
	type outcome struct {
		entry  Pending
		result string
	}
	var done []string
	var resolved []outcome // done's: noted once the backlog no longer holds them, so a failed removal notes nothing twice
	for i, p := range list {
		if p.material().Expired(now) {
			done, resolved = append(done, p.Token), append(resolved, outcome{p, audit.ExpiredUnrevoked})
			continue
		}
		r, reason := l.revoke(ctx, p.material())
		if r.confirmed() {
			done, resolved = append(done, p.Token), append(resolved, outcome{p, audit.Revoked})
			continue
		}
		if r.Unreachable {
			for _, rest := range list[i:] {
				l.note(rest.Container, audit.Pending, reason, rest.material(), nil)
			}
			break
		}
		l.note(p.Container, audit.Pending, reason, p.material(), nil)
	}
	if len(done) > 0 {
		if err := l.locked(func() error {
			return l.editBacklog(func(list []Pending) []Pending {
				return slices.DeleteFunc(list, func(p Pending) bool { return slices.Contains(done, p.Token) })
			})
		}); err != nil {
			return nil, err
		}
	}
	for _, o := range resolved {
		l.note(o.entry.Container, o.result, "", o.entry.material(), nil)
	}
	return listed(slices.DeleteFunc(list, func(p Pending) bool { return slices.Contains(done, p.Token) })), nil
}
