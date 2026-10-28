package credential

import (
	"context"
	"encoding/json"
	"errors"
	"slices"
	"strings"

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

// Store holds broker-only items; NotFound is its error for an absent one.
type Store interface {
	Secret(service, account string) ([]byte, error)
	PutSecret(service, account string, data []byte) error
	DeleteSecret(service, account string) error // an absent item is success
}

// Lifecycle issues and renews one shared account's tokens for its host consumers. Each consumer's record (service
// Records, account Consumer.ID()) holds the materials published to it; one leaves the record only by withdrawal, never
// revocation, as other consumers share the account. Lock guards each read-modify-write of a record, never a provider
// request. Audit (nil: none) gets each token's outcome, never its value. Acquire and Refresh refuse a provider whose
// kind is not renewable (ErrStatic). A material whose publication failed stays recorded prepared; a failed run
// withdraws only what it appended, never a material the record already held.
type Lifecycle struct {
	Provider Provider
	Store    Store
	NotFound error
	Records  string
	Lock     func() (unlock func(), err error)
	Audit    *audit.Recorder
}

func (l Lifecycle) ready() error {
	if l.Provider == nil {
		return errors.New("no credential provider")
	}
	return Renewable(l.Provider.Kind())
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

// Note gives rec m's outcome for consumer id under kind, reason redacted; nil rec: none.
func Note(rec *audit.Recorder, kind, id, result, reason string, m Material, repos *audit.Repositories) {
	if rec == nil {
		return
	}
	expires := m.ExpiresAt
	rec.Add(id, audit.Credential{Kind: kind, Result: result, Reason: Redact(reason, m.Token), ExpiresAt: &expires, Account: m.Account,
		Installation: m.Target, Repos: repos})
}

// note gives the audit recorder m's outcome for consumer id (Note).
func (l Lifecycle) note(id, result string, m Material) {
	Note(l.Audit, l.Provider.AuditKind(), id, result, "", m, nil)
}

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

func tokenIn(entries []Material) func(string) bool {
	return func(t string) bool {
		return slices.ContainsFunc(entries, func(m Material) bool { return m.Token == t })
	}
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

// put records m, prepared, in rec at the next revision; a token rec already holds (the account not renewed
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

// rollback is whether a failed run withdraws a material it issued, recorded as r: only when the run appended it. One
// the record already held stays usable, and a failed write leaves the rest of the record as it was.
func rollback(r recording) bool { return r == appended }

// desired is rec's highest revision.
func desired(rec *Record) int {
	n := 0
	for _, m := range rec.Tokens {
		n = max(n, m.Revision)
	}
	return n
}

// withdraw drops entries from id's record without revoking them: other consumers hold the same material.
func (l Lifecycle) withdraw(id string, entries []Material) error {
	err := l.locked(func() error {
		rec, found, err := l.readRecord(id)
		if err != nil || !found {
			return err
		}
		in := tokenIn(entries)
		rec.Tokens = slices.DeleteFunc(rec.Tokens, func(m Material) bool { return in(m.Token) })
		return l.writeRecord(id, rec)
	})
	if err != nil {
		return err
	}
	for _, m := range entries {
		l.note(id, audit.Withdrawn, m)
	}
	return nil
}

// deliver publishes m to d's consumer; its record is its copy, so there is nothing to write.
func deliver(d Delivery, m Material) error {
	if d.Publish != nil {
		return d.Publish(m)
	}
	return nil
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
// the next request, all are published, then all are marked delivered. On a failure it withdraws only what it
// appended (rollback); one whose publication failed stays prepared.
func (l Lifecycle) Acquire(ctx context.Context, d Delivery, subject string, scope map[string]string) ([]protocol.Expiry, error) {
	if err := l.ready(); err != nil {
		return nil, err
	}
	if err := d.Consumer.check(); err != nil {
		return nil, err
	}
	id := d.Consumer.ID()
	issuer, err := l.Provider.Begin(ctx, subject)
	if err != nil {
		return nil, err
	}
	secret := issuer.Secret()
	targets, err := issuer.Targets(ctx)
	if err != nil {
		return nil, redactErr(err, secret)
	}
	var issued []Material
	var how []recording // issued's
	fail := func(err error) ([]protocol.Expiry, error) {
		var appended []Material
		for i, m := range issued {
			if rollback(how[i]) {
				appended = append(appended, m)
			}
		}
		if len(appended) > 0 {
			_ = l.withdraw(id, appended)
		}
		return nil, redactErr(err, secret)
	}
	for _, target := range targets {
		m, err := issuer.Issue(ctx, scope)
		if err != nil {
			return fail(err)
		}
		m.Account, m.Target, m.State = target.Account, target.Target, Prepared
		issued, how = append(issued, m), append(how, unrecorded)
		if _, err := l.editRecord(id, true, func(rec *Record, _ bool) {
			rec.Subject, rec.Scope = subject, scope
			how[len(how)-1] = put(rec, &m)
		}); err != nil {
			l.note(id, audit.Issued, m)
			return fail(err)
		}
		issued[len(issued)-1] = m
		l.note(id, audit.Issued, m)
	}
	if len(issued) == 0 {
		return expiries(nil), nil // a subject with no targets has nothing to grant
	}
	for _, m := range issued {
		if err := deliver(d, m); err != nil {
			return nil, redactErr(err, secret)
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
// prepared, published, then marked delivered while the slot's other materials turn retiring, which are then
// withdrawn; each is recorded as put does. It issues with the record's scope when it has one, else scope (a record an
// older broker wrote).
func (l Lifecycle) Refresh(ctx context.Context, d Delivery, scope map[string]string) ([]protocol.Expiry, error) {
	if err := l.ready(); err != nil {
		return nil, err
	}
	if err := d.Consumer.check(); err != nil {
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
	secret := issuer.Secret()
	var fresh []Material
	for _, slot := range slots {
		m, err := issuer.Issue(ctx, scope)
		if err != nil {
			return nil, redactErr(err, secret)
		}
		m.Account, m.Target, m.State = slot.Account, slot.Target, Prepared
		how := unrecorded
		found, err := l.editRecord(id, false, func(rec *Record, found bool) {
			if found {
				how = put(rec, &m)
			}
		})
		if err != nil || !found {
			l.note(id, audit.Renewed, m)
			if err == nil {
				err = ErrReleased
			}
			if rollback(how) {
				_ = l.withdraw(id, []Material{m})
			}
			return nil, err
		}
		l.note(id, audit.Renewed, m)
		if err := deliver(d, m); err != nil {
			return nil, redactErr(err, secret) // the record keeps it prepared beside the slot's usable material
		}
		var old []Material
		kept := false
		found, err = l.editRecord(id, false, func(rec *Record, _ bool) {
			if kept = slices.ContainsFunc(rec.Tokens, func(t Material) bool { return t.Token == m.Token }); !kept {
				return // another command withdrew the replacement meanwhile; the slot's other materials stay as they are
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
		_ = l.withdraw(id, old)
		fresh = append(fresh, m)
	}
	return expiries(fresh), nil
}
