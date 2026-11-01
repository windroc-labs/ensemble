package credential

import (
	"errors"
	"fmt"
	"regexp"
	"strings"
	"time"

	"ensemble/broker/internal/audit"
)

// A static secret's reference, its safe name: the prefix of its kind, then its name. The broker package pins each
// prefix to its keychain service.
const (
	ClaudePrefix   = "ensemble-broker-claude/"
	OpencodePrefix = "ensemble-broker-opencode/"
	EnvPrefix      = "ensemble-broker-env/"
)

// Provenance is where a static secret's expiry came from.
type Provenance string

const (
	Explicit Provenance = "explicit" // --expires
	Derived  Provenance = "derived"  // --issued plus one calendar year
	Assumed  Provenance = "assumed"  // --fresh-token: registration plus one year
	Unknown  Provenance = "unknown"
)

// Static is a static secret's record: Version counts its logins from 1 (0: never registered); a Pending one is a
// login begun and not committed, read as unknown. Notified, in the host store only, is the thresholds its host was
// told of for Version.
type Static struct {
	Version    int        `json:"version"`
	ExpiresAt  string     `json:"expires_at,omitempty"` // RFC 3339 UTC; "": unknown
	Provenance Provenance `json:"provenance"`
	Pending    bool       `json:"pending,omitempty"`
	Notified   []string   `json:"notified,omitempty"`
}

// expiry is s's known expiry; false for an unknown, pending or unreadable one.
func (s Static) expiry() (time.Time, bool) {
	if s.Pending || s.ExpiresAt == "" {
		return time.Time{}, false
	}
	t, err := time.Parse(time.RFC3339, s.ExpiresAt)
	return t, err == nil
}

// Expired is whether s's known expiry is at or before now.
func (s Static) Expired(now time.Time) bool {
	t, ok := s.expiry()
	return ok && !now.Before(t)
}

var staticPrefixes = map[Kind]string{Claude: ClaudePrefix, Opencode: OpencodePrefix, Env: EnvPrefix}

// StaticRef is the reference of kind k's secret name; a token kind has none.
func StaticRef(k Kind, name string) string {
	prefix, ok := staticPrefixes[k]
	if !ok {
		panic("not a static kind: " + string(k))
	}
	return prefix + name
}

// StaticKind is ref's kind and name; false for a string that is no static reference.
func StaticKind(ref string) (Kind, string, bool) {
	for k, prefix := range staticPrefixes {
		if name, ok := strings.CutPrefix(ref, prefix); ok && name != "" {
			return k, name, true
		}
	}
	return "", "", false
}

// LoginCommand is the broker command that registers ref's secret.
func LoginCommand(ref string) string {
	k, name, _ := StaticKind(ref)
	switch k {
	case Claude:
		return "ensemble-broker login claude " + name
	case Opencode:
		return "ensemble-broker login opencode " + name
	}
	return "ensemble-broker login env " + name
}

// Registration is a login's expiry options; a zero time was not given.
type Registration struct {
	Expires, Issued time.Time
	Fresh           bool
}

const dateLayout = "2006-01-02"

// ParseDate reads YYYY-MM-DD as that day's 00:00 UTC.
func ParseDate(s string) (time.Time, error) {
	t, err := time.Parse(dateLayout, s)
	if err != nil || t.Format(dateLayout) != s {
		return time.Time{}, fmt.Errorf("%q is not a YYYY-MM-DD date", s)
	}
	return t, nil
}

// Day is t's UTC date, YYYY-MM-DD.
func Day(t time.Time) string { return t.UTC().Format(dateLayout) }

// Resolve is the expiry (RFC 3339, "" unknown) and provenance r gives kind k's secret registered at now. --issued and
// --fresh-token are Claude's: its tokens last one year.
func (r Registration) Resolve(k Kind, now time.Time) (string, Provenance, error) {
	if k != Claude && (r.Fresh || !r.Issued.IsZero()) {
		return "", "", errors.New("--issued and --fresh-token apply only to login claude")
	}
	if r.Fresh && (!r.Expires.IsZero() || !r.Issued.IsZero()) {
		return "", "", errors.New("--fresh-token takes no date")
	}
	expires, p := r.Expires, Explicit
	if !r.Issued.IsZero() {
		if r.Issued.After(now) {
			return "", "", fmt.Errorf("--issued %s is in the future", Day(r.Issued))
		}
		derived := r.Issued.AddDate(1, 0, 0)
		if !expires.IsZero() && !expires.Equal(derived) {
			return "", "", fmt.Errorf("--issued %s means expiry %s, not %s", Day(r.Issued), Day(derived), Day(expires))
		}
		if expires.IsZero() {
			expires, p = derived, Derived
		}
	}
	if r.Fresh {
		expires, p = now.AddDate(1, 0, 0), Assumed
	}
	if expires.IsZero() {
		return "", Unknown, nil
	}
	if !expires.After(now) {
		return "", "", fmt.Errorf("%s is not in the future: register a new secret", Day(expires))
	}
	return expires.UTC().Format(time.RFC3339), p, nil
}

// A notice's thresholds, most urgent first: each is reached its days before a known expiry.
var thresholds = []struct {
	name string
	days int
}{{"expired", 0}, {"1d", 1}, {"7d", 7}, {"30d", 30}}

// Rotated is the notice of a consumer holding an older version than the one stored.
const Rotated = "rotated"

// NoticeKeyPattern matches every notice key Due can produce.
var NoticeKeyPattern = regexp.MustCompile(`^ensemble-broker-(claude|opencode|env)/[A-Za-z0-9._-]{1,64}@[0-9]{1,9}:(` + labels() + `)$`)

// labels are the thresholds, least urgent first, then Rotated: the schema's order.
func labels() string {
	var names []string
	for i := len(thresholds) - 1; i >= 0; i-- {
		names = append(names, thresholds[i].name)
	}
	return strings.Join(append(names, Rotated), "|")
}

// thresholdAt is when a threshold days before expires is reached.
func thresholdAt(expires time.Time, days int) time.Time {
	return expires.Add(-time.Duration(days) * 24 * time.Hour)
}

// Notice is one notice for one consumer; Key, <reference>@<version>:<Label>, deduplicates it. Label is the threshold
// or Rotated; ExpiresAt the expiry it warns of ("": none).
type Notice struct{ Key, Reference, Label, ExpiresAt, Text string }

// auditKinds are the audit log's kinds of the static secrets.
var auditKinds = map[Kind]string{Claude: audit.ClaudeSetupToken, Opencode: audit.OpencodeAPIKey, Env: audit.EnvSecret}

// Audit is n's audit record: its reference and label, never a secret.
func (n Notice) Audit() audit.Credential {
	k, _, _ := StaticKind(n.Reference)
	c := audit.Credential{Kind: auditKinds[k], Item: n.Reference, Result: audit.Notified, Notice: n.Label}
	if n.ExpiresAt != "" {
		expires := n.ExpiresAt
		c.ExpiresAt = &expires
	}
	return c
}

func noticeKey(ref string, version int, label string) string {
	return fmt.Sprintf("%s@%d:%s", ref, version, label)
}

func (s Static) reached(days int, now time.Time) bool {
	expires, ok := s.expiry()
	return ok && !now.Before(thresholdAt(expires, days))
}

// Due is what a consumer holding injected is due at now, delivered being its own record of keys: the most urgent
// threshold reached, unless it or a more urgent one was delivered for that version; and a rotated notice when stored is
// committed and newer. The host consumer passes its stored record as both.
func Due(ref string, injected, stored Static, delivered func(key string) bool, now time.Time) []Notice {
	var out []Notice
	for i, th := range thresholds {
		if !injected.reached(th.days, now) {
			continue
		}
		told := false
		for _, more := range thresholds[:i+1] {
			told = told || delivered(noticeKey(ref, injected.Version, more.name))
		}
		if !told {
			out = append(out, Notice{noticeKey(ref, injected.Version, th.name), ref, th.name, injected.ExpiresAt, Text(ref, injected, th.name, now)})
		}
		break
	}
	if !stored.Pending && stored.Version > injected.Version {
		if key := noticeKey(ref, stored.Version, Rotated); !delivered(key) {
			text := fmt.Sprintf("%s was replaced (version %d); this session holds version %d until it restarts", ref, stored.Version, injected.Version)
			out = append(out, Notice{Key: key, Reference: ref, Label: Rotated, Text: text})
		}
	}
	return out
}

// Threshold is the most urgent threshold s has reached at now; "" for none.
func Threshold(s Static, now time.Time) string {
	for _, th := range thresholds {
		if s.reached(th.days, now) {
			return th.name
		}
	}
	return ""
}

// rotationPoll bounds the wait for a check, so a replaced secret is announced within it.
const rotationPoll = time.Hour

// NextCheck is when a consumer holding records should next check, in whole seconds: the earliest threshold still
// ahead, at most rotationPoll away; nil for no records.
func NextCheck(records map[string]Static, now time.Time) *time.Time {
	if len(records) == 0 {
		return nil
	}
	next := now.Add(rotationPoll)
	for _, s := range records {
		expires, ok := s.expiry()
		if !ok {
			continue
		}
		for _, th := range thresholds {
			if at := thresholdAt(expires, th.days); at.After(now) && at.Before(next) {
				next = at
			}
		}
	}
	if whole := next.Truncate(time.Second); whole.Before(next) {
		next = whole.Add(time.Second)
	}
	next = next.UTC()
	return &next
}

// guidance is how to replace ref's secret.
func guidance(ref string) string {
	k, _, _ := StaticKind(ref)
	login := LoginCommand(ref)
	switch k {
	case Claude:
		return "run claude setup-token, then " + login + " --fresh-token"
	case Opencode:
		return "issue a new key, then " + login + " --expires <date>"
	}
	return "rotate it at its source, then " + login + " [--expires <date>]"
}

// Text is threshold's notice of ref's secret s at now, one printable line.
func Text(ref string, s Static, threshold string, now time.Time) string {
	expires, _ := s.expiry()
	if threshold == "expired" {
		return fmt.Sprintf("%s expired %s (%s): %s; new starts are refused until then", ref, Day(expires), s.Provenance, guidance(ref))
	}
	days := int((expires.Sub(now) + 24*time.Hour - 1) / (24 * time.Hour))
	in := fmt.Sprintf("in %d days", days)
	if days == 1 {
		in = "in 1 day"
	}
	return fmt.Sprintf("%s expires %s (%s), %s: %s; running sessions keep their copy until they restart", ref, Day(expires), s.Provenance, in, guidance(ref))
}

// UnknownText is status's prompt for a Claude token of unknown expiry.
func UnknownText(ref string) string {
	return ref + " expiry unknown: a date is recorded only with the token; after claude setup-token, rerun " + LoginCommand(ref) +
		" --fresh-token, or paste the current token with --expires or --issued"
}
