// Package protocol holds the broker protocol's error contract (src/broker/protocol/docker-broker/protocol.schema.json).
package protocol

import (
	"encoding/json"
	"fmt"
	"strings"
	"unicode/utf8"
)

const Version = "39"

const AuditWriteFailed = "audit_write_failed"

type Code string

const (
	Usage             Code = "usage"
	ManifestRejected  Code = "manifest_rejected"
	MountRejected     Code = "mount_rejected"
	LoginRequired     Code = "login_required"
	ImageRejected     Code = "image_rejected"
	DockerUnreachable Code = "docker_unreachable"
	BuildFailed       Code = "build_failed"
	StartFailed       Code = "start_failed"
	CloseFailed       Code = "close_failed"
	KeychainFailed    Code = "keychain_failed"
	WorkspaceDirty    Code = "workspace_dirty"
	WorkspaceRejected Code = "workspace_rejected"
	WorkspaceFailed   Code = "workspace_failed"
	GitHubFailed      Code = "github_failed"
	RefreshFailed     Code = "refresh_failed"
	EgressUnavailable Code = "egress_unavailable"
	InternalError     Code = "internal_error"
	Busy              Code = "busy"
)

var exits = map[Code]int{
	Usage: 2, ManifestRejected: 3, MountRejected: 4, LoginRequired: 5, ImageRejected: 6,
	DockerUnreachable: 7, BuildFailed: 8, StartFailed: 9, CloseFailed: 10, KeychainFailed: 11,
	WorkspaceDirty: 12, WorkspaceRejected: 13, WorkspaceFailed: 14, GitHubFailed: 15, RefreshFailed: 16,
	EgressUnavailable: 17, InternalError: 70, Busy: 75,
}

func Exit(c Code) int {
	if n, ok := exits[c]; ok {
		return n
	}
	return exits[InternalError]
}

const (
	NotNeeded = "not_needed"
	Removed   = "removed"
	Failed    = "failed"
)

// Expiry is one installation token's account and expiry, as start and refresh report it: never the token.
type Expiry struct {
	Account   string `json:"account"`
	ExpiresAt string `json:"expires_at"`
}

type CodexStatus struct {
	Version   string `json:"version"`
	Path      string `json:"path"`
	SHA256    string `json:"sha256"`
	LoggedIn  bool   `json:"logged_in"`
	State     string `json:"state"`
	ExpiresAt string `json:"expires_at,omitempty"`
	Repair    string `json:"repair"`
}

type Holder struct {
	Container string `json:"container"`
	Owner     string `json:"owner"`
}

type Error struct {
	Code         Code    `json:"code"`
	Message      string  `json:"message"`
	Cleanup      string  `json:"cleanup"`
	Cause        string  `json:"cause,omitempty"`
	CleanupError string  `json:"cleanup_error,omitempty"`
	Container    string  `json:"container,omitempty"`
	Holder       *Holder `json:"holder,omitempty"`
	AuditWarning string  `json:"-"` // independent envelope metadata, never part of the operation error
}

func New(code Code, message string) *Error {
	return &Error{Code: code, Message: message, Cleanup: NotNeeded}
}

func Newf(code Code, format string, args ...any) *Error {
	return New(code, fmt.Sprintf(format, args...))
}

func (e *Error) Error() string { return string(e.Code) + ": " + e.Message }

const maxText = 1000

// Text makes s a protocol diagnostic: valid UTF-8, single line, control characters escaped.
func Text(s string) string {
	s = strings.ToValidUTF8(s, "�")
	var b strings.Builder
	for _, r := range s {
		if r < 0x20 || r == 0x7f {
			fmt.Fprintf(&b, `\x%02x`, r)
		} else {
			b.WriteRune(r)
		}
	}
	out := strings.TrimSpace(b.String())
	if len(out) > maxText {
		cut := maxText
		for cut > 0 && !utf8.RuneStart(out[cut]) {
			cut--
		}
		out = out[:cut]
	}
	if out == "" {
		return "-"
	}
	return out
}

type wire struct {
	Command string `json:"command"`
	*Error
}

// Envelope serializes e as the protocol's stderr object, followed by LF.
func Envelope(command string, e *Error) []byte {
	clean := *e
	clean.Message = Text(e.Message)
	if clean.Cause != "" {
		clean.Cause = Text(clean.Cause)
	}
	if clean.Cleanup == "" {
		clean.Cleanup = NotNeeded
	}
	if clean.Cleanup == Failed {
		clean.CleanupError = Text(clean.CleanupError)
	} else {
		clean.CleanupError = ""
	}
	data, err := json.Marshal(struct {
		ExitCode     int    `json:"exit_code"`
		Error        wire   `json:"error"`
		AuditWarning string `json:"audit_warning,omitempty"`
	}{Exit(clean.Code), wire{command, &clean}, clean.AuditWarning})
	if err != nil {
		panic(err)
	}
	return append(data, '\n')
}
