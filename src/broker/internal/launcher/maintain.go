package launcher

import (
	"context"
	"fmt"
	"os"
	"slices"
	"time"

	"ensemble/broker/internal/audit"
	"ensemble/broker/internal/credential"
	"ensemble/broker/internal/protocol"
)

const (
	RefreshAhead = 10 * time.Minute // a token provider is renewed this long before its earliest delivered expiry
	RetryAfter   = time.Minute      // a failed provider, or a failed notice check, is tried again this long after
)

// Deadline is when a caller must act without a newer confirmation: past At, it performs Action ("close": close the
// container), reporting Reason.
type Deadline struct {
	At     string `json:"at"`
	Action string `json:"action"`
	Reason string `json:"reason"`
}

type ResultError struct {
	Code    protocol.Code `json:"code"`
	Message string        `json:"message"`
	Cause   string        `json:"cause,omitempty"`
}

// ProviderResult is one maintained component: a token provider renewed (its earliest new expiry) or failed, or the
// static secret notices, which only fail.
type ProviderResult struct {
	Credential string       `json:"credential"`
	ExpiresAt  *string      `json:"expires_at"`
	Error      *ResultError `json:"error"`
}

// MaintainResult is everything a caller needs to schedule: when to call again (nil: never), the deadline it enforces
// meanwhile (nil: none), each component's outcome and the notices due.
type MaintainResult struct {
	Container string           `json:"container"`
	NextCheck *string          `json:"next_check"`
	Deadline  *Deadline        `json:"deadline"`
	Results   []ProviderResult `json:"results"`
	Notices   []Notice         `json:"notices"`
}

const noticesComponent = "notices"

// tokenProvider is one renewable kind's maintenance: renew delivers fresh material into the container and records its
// expiries through record, returning them; failed is the message for an unclassified error, %s the container;
// deadline, when set, is the reason a caller closes the container once the kind's delivered material expires.
type tokenProvider struct {
	kind     credential.Kind
	renew    func(ctx context.Context, h Host, m Manifest, container string, record func([]string) error) ([]string, error)
	failed   string
	deadline string
}

var tokenProviders = []tokenProvider{
	{
		kind:     credential.Codex,
		renew:    renewCodex,
		failed:   "Writing Codex credentials into %s failed.",
		deadline: "Client authentication expired; run /usr/local/libexec/ensemble/ensemble-broker login codex on the host, then resume.",
	},
	{kind: credential.GitHub, renew: renewGitHub, failed: "Writing a GitHub token into %s failed."},
}

// renewCodex renews only when no consumer has and records the expiry as Codex's publication, so a retry after a
// failed record delivers the login already renewed.
func renewCodex(ctx context.Context, h Host, m Manifest, container string, record func([]string) error) ([]string, error) {
	exec := h.agentExec(ctx, container)
	inject := func(argv []string, data []byte) error { return exec(append(argv, m.CodexDir), data) }
	expiry, err := h.Codex.Refresh(ctx, container, inject, func(expiry string) error { return record([]string{expiry}) })
	if err != nil {
		return nil, err
	}
	h.Audit.Add(container, audit.Credential{Kind: clientKind(m.Client), Result: audit.Renewed, ExpiresAt: &expiry}, "")
	return []string{expiry}, nil
}

func renewGitHub(ctx context.Context, h Host, m Manifest, container string, record func([]string) error) ([]string, error) {
	if err := h.GitHub.Ready(m.GitHubApp); err != nil {
		return nil, injected(err, protocol.RefreshFailed, "GitHub renewal for "+container+" failed.")
	}
	expiries, err := h.GitHub.Refresh(ctx, m.GitHubApp, m.GitHubRepo, m.GitHubPermissions, container, h.agentExec(ctx, container))
	if err != nil {
		return nil, err
	}
	times := expiryTimes(expiries)
	return times, record(times)
}

// Maintain renews each token provider of the container started with path that is due (every one with renew), works out
// its static secret notices, acknowledging delivered, and says when to call again. A provider's or the notices' failure
// is its own result, never the command's: only a manifest that is unreadable, not the container's or not safely
// mounted fails it.
func (h Host) Maintain(ctx context.Context, container, path string, delivered []string, renew bool) (MaintainResult, error) {
	h.Audit.Begin(container, nil)
	data, err := os.ReadFile(path)
	if err != nil {
		return MaintainResult{}, protocol.New(protocol.ManifestRejected, "Cannot read manifest "+path+".")
	}
	m, err := ParseManifest(data)
	if err != nil {
		return MaintainResult{}, err
	}
	if "ensemble-"+m.Name+"-"+m.Generation != container {
		return MaintainResult{}, protocol.New(protocol.ManifestRejected, "Manifest rejected: maintain needs the container's manifest.")
	}
	if err := h.checkMounts(m); err != nil {
		return MaintainResult{}, err
	}
	now := h.now()
	record := newTokenRecord()
	if h.Schedule != nil {
		record = h.Schedule.Read(m)
	}
	result := MaintainResult{Container: container, Results: []ProviderResult{}, Notices: []Notice{}}
	var next *time.Time
	consider := func(t time.Time) {
		if next == nil || t.Before(*next) {
			next = &t
		}
	}
	held := heldKinds(m)
	for _, p := range tokenProviders {
		if !slices.Contains(held, p.kind) {
			continue
		}
		if at := dueAt(record, p.kind, now); !renew && at.After(now) {
			consider(at)
			continue
		}
		expires, err := p.renew(ctx, h, m, container, func(expires []string) error {
			if earliest(expires) == nil {
				return tokensFailed(m)
			}
			return h.recordTokens(m, p.kind, expires, nil)
		})
		if err != nil {
			e := injected(err, protocol.RefreshFailed, fmt.Sprintf(p.failed, container))
			h.Audit.Renewing(e.Code)
			retry := now.Add(RetryAfter)
			_ = h.recordTokens(m, p.kind, nil, &retry) // a lost retry time only makes the provider due sooner
			result.Results = append(result.Results, ProviderResult{Credential: string(p.kind), Error: resultError(e)})
			consider(retry)
			continue
		}
		h.Audit.Renewing("")
		record.Expires[p.kind] = expires
		delete(record.Retry, p.kind)
		at := earliest(expires).UTC().Format(time.RFC3339)
		result.Results = append(result.Results, ProviderResult{Credential: string(p.kind), ExpiresAt: &at})
		consider(dueAt(record, p.kind, now))
	}
	result.Deadline = deadlineOf(record, held)
	if notices, static, err := h.dueNotices(container, m, delivered, now); err != nil {
		result.Results = append(result.Results, ProviderResult{Credential: noticesComponent, Error: resultError(injected(err, protocol.InternalError, "Checking static secret notices failed."))})
		consider(now.Add(RetryAfter))
	} else {
		result.Notices = notices
		if static != nil {
			consider(*static)
		}
	}
	if next != nil {
		if next.Before(now) {
			next = &now
		}
		at := next.UTC().Format(time.RFC3339)
		result.NextCheck = &at
	}
	return result, nil
}

// deadlineOf is the first held kind's with a deadline whose expiry is recorded; nil for none.
func deadlineOf(record TokenRecord, held []credential.Kind) *Deadline {
	for _, p := range tokenProviders {
		if p.deadline == "" || !slices.Contains(held, p.kind) {
			continue
		}
		if at := earliest(record.Expires[p.kind]); at != nil {
			return &Deadline{At: at.UTC().Format(time.RFC3339), Action: "close", Reason: p.deadline}
		}
	}
	return nil
}

// dueAt is when kind is next due: once its retry time has passed, RefreshAhead before its earliest recorded expiry;
// now without a record.
func dueAt(record TokenRecord, kind credential.Kind, now time.Time) time.Time {
	at := now
	if expiry := earliest(record.Expires[kind]); expiry != nil {
		at = expiry.Add(-RefreshAhead)
	}
	if retry, err := time.Parse(time.RFC3339, record.Retry[kind]); err == nil && retry.After(at) {
		at = retry
	}
	return at
}

// earliest is expires' earliest time; nil when it is empty or holds a time that is not RFC 3339.
func earliest(expires []string) *time.Time {
	var first *time.Time
	for _, e := range expires {
		t, err := time.Parse(time.RFC3339, e)
		if err != nil {
			return nil
		}
		if first == nil || t.Before(*first) {
			first = &t
		}
	}
	return first
}

// resultError is e as a result carries it: its diagnostics single-line and capped, as an error envelope's are.
func resultError(e *protocol.Error) *ResultError {
	r := &ResultError{Code: e.Code, Message: protocol.Text(e.Message)}
	if e.Cause != "" {
		r.Cause = protocol.Text(e.Cause)
	}
	return r
}
