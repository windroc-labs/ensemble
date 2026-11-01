// Package audit is the broker's credential lifecycle log: one JSON line per start, refresh and close.
// No type here holds a token, key or other secret.
package audit

import (
	"encoding/json"
	"regexp"
	"time"
)

// Kinds and results, the log's only vocabulary for them.
const (
	ClaudeSetupToken  = "claude-setup-token"
	CodexAccessToken  = "codex-access-token"
	GitHubToken       = "github-installation-token"
	LinearAccessToken = "linear-access-token"
	EnvSecret         = "env-secret"
	OpencodeAPIKey    = "opencode-api-key"

	Issued    = "issued"
	Renewed   = "renewed"
	Withdrawn = "withdrawn" // deleted with its container, nothing to revoke
	Revoked   = "revoked"
	Unrevoked = "unrevoked" // an in-command revocation that failed, with a reason; never retried

	// Events: a command line's is its command.
	startEvent   = "start"
	refreshEvent = "refresh"
	closeEvent   = "close"

	maintainCommand = "maintain" // writes a refresh line only when it tried a token provider
)

var containerName = regexp.MustCompile(`^ensemble-([A-Za-z0-9_-]+)-([0-9a-f]{32})$`)

// Split is a container name's session and generation, from ensemble-<session>-<generation>; else "", "".
func Split(container string) (session, generation string) {
	m := containerName.FindStringSubmatch(container)
	if m == nil {
		return "", ""
	}
	return m[1], m[2]
}

type Mount struct {
	Type   string `json:"type"`             // volume, bind or tmpfs
	Source string `json:"source,omitempty"` // volume name or host path; none for tmpfs
	Target string `json:"target"`
	Mode   string `json:"mode"` // rw or ro
}

// Repositories is what an issued or renewed GitHub token reaches; Error set means the listing failed.
type Repositories struct {
	Selection *string  // all or selected
	Names     []string // sorted full names
	Error     string
}

type Credential struct {
	Kind         string
	Result       string
	Reason       string // unrevoked only
	ExpiresAt    *string
	Account      string // GitHub only
	Item         string // environment secrets: the reference, never the value
	Installation int64  // GitHub; 0 when unknown
	Repos        *Repositories
}

var null = json.RawMessage("null")

func raw(v any) json.RawMessage {
	data, err := json.Marshal(v)
	if err != nil {
		panic(err)
	}
	return data
}

func (c Credential) MarshalJSON() ([]byte, error) {
	out := struct {
		Kind                string          `json:"kind"`
		Result              string          `json:"result"`
		Reason              *string         `json:"reason,omitempty"`
		ExpiresAt           *string         `json:"expires_at"`
		Account             string          `json:"account,omitempty"`
		Item                string          `json:"item,omitempty"`
		Installation        int64           `json:"installation,omitempty"`
		RepositorySelection json.RawMessage `json:"repository_selection,omitempty"`
		Repositories        json.RawMessage `json:"repositories,omitempty"`
		RepositoriesError   string          `json:"repositories_error,omitempty"`
	}{Kind: c.Kind, Result: c.Result, ExpiresAt: c.ExpiresAt, Account: c.Account, Item: c.Item, Installation: c.Installation}
	if c.Result == Unrevoked {
		reason := c.Reason
		if reason == "" {
			reason = "unknown"
		}
		out.Reason = &reason
	}
	if r := c.Repos; r != nil {
		out.RepositorySelection, out.Repositories, out.RepositoriesError = null, null, r.Error
		if r.Error == "" && r.Selection != nil {
			names := r.Names
			if names == nil {
				names = []string{}
			}
			out.RepositorySelection, out.Repositories = raw(*r.Selection), raw(names)
		}
	}
	return json.Marshal(out)
}

// StartFacts are what only a start line holds; "" and nil are unknown, written as null.
type StartFacts struct {
	Image, ImageDigest string
	Mounts             []Mount
}

// Line is one log line; Session and Generation come from Container.
type Line struct {
	Time        time.Time
	Command     string // the broker subcommand that wrote it
	Event       string // start, refresh, or close
	Container   string
	Start       *StartFacts // read only when Event is start
	Credentials []Credential
	Error       string // the command's protocol code, or a maintain's first provider failure; "" on success
}

func orNull(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}

func (l Line) MarshalJSON() ([]byte, error) {
	session, generation := Split(l.Container)
	out := struct {
		Time        string          `json:"time"`
		Command     string          `json:"command"`
		Event       string          `json:"event"`
		Session     *string         `json:"session"`
		Generation  *string         `json:"generation"`
		Container   *string         `json:"container"`
		Image       json.RawMessage `json:"image,omitempty"`
		ImageDigest json.RawMessage `json:"image_digest,omitempty"`
		Mounts      json.RawMessage `json:"mounts,omitempty"`
		Credentials []Credential    `json:"credentials"`
		Error       *string         `json:"error"`
	}{Time: l.Time.UTC().Format(time.RFC3339Nano), Command: l.Command, Event: l.Event, Session: orNull(session),
		Generation: orNull(generation), Container: orNull(l.Container), Credentials: l.Credentials, Error: orNull(l.Error)}
	if out.Credentials == nil {
		out.Credentials = []Credential{}
	}
	if l.Event == startEvent {
		facts := StartFacts{}
		if l.Start != nil {
			facts = *l.Start
		}
		out.Image, out.ImageDigest, out.Mounts = raw(orNull(facts.Image)), raw(orNull(facts.ImageDigest)), null
		if facts.Mounts != nil {
			out.Mounts = raw(facts.Mounts)
		}
	}
	return json.Marshal(out)
}
