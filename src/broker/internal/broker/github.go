package broker

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"slices"
	"syscall"
	"time"

	"ensemble/broker/internal/audit"
	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/github"
	"ensemble/broker/internal/launcher"
	"ensemble/broker/internal/protocol"
)

// The broker-only items: a token record per container, one revocation backlog, and each App's bot user ID.
const (
	usersService       = "ensemble-github-users"
	tokensService      = "ensemble-tokens"
	revocationsService = "ensemble-revocations"
	revocationsAccount = "pending"
	tokenDir           = docker.SecretsDir + "/github"
)

// cloneScript shallow-clones branch or tag $2 of github.com/$1 into $3 with the prefetch token on stdin, kept only in
// $HOME (the helper's tmpfs). git reads it through a command-line credential helper, so the clone's config and remote
// URL never hold it, and runs none of the repo's hooks.
const cloneScript = `set -eu
umask 077
cat > "$HOME/token"
exec git -c core.hooksPath=/dev/null -c core.fsmonitor=false -c credential.helper= ` +
	`-c 'credential.helper=!f() { test "$1" = get || exit 0; echo username=x-access-token; printf "password=%s\n" "$(cat "$HOME/token")"; }; f' ` +
	`clone --quiet --depth 1 --branch "$2" -- "https://github.com/$1.git" "$3"`

// prefetchPermissions is all a prefetch token may do: read its one repository's contents.
var prefetchPermissions = map[string]string{"contents": "read"}

// writeScript replaces $1's token file with stdin; the temporary name is a dot file, which no reader matches.
const writeScript = "mkdir -p -m 0700 " + tokenDir + " && umask 0377 && rm -f " + tokenDir + `/."$1".new && cat > ` + tokenDir +
	`/."$1".new && mv -f ` + tokenDir + `/."$1".new ` + tokenDir + `/"$1"`

// botUser is what login github keeps in the users item: the App's slug and its bot user's ID.
type botUser struct {
	Slug string `json:"slug"`
	ID   int64  `json:"id"`
}

type tokenEntry struct {
	Account      string `json:"account"`
	Installation int64  `json:"installation"`
	Token        string `json:"token"`
	ExpiresAt    string `json:"expires_at"`
}

type tokenRecord struct {
	App    string       `json:"app"`
	Tokens []tokenEntry `json:"tokens"`
}

type pending struct {
	Container string `json:"container"`
	Account   string `json:"account"`
	Token     string `json:"token"`
	ExpiresAt string `json:"expires_at"`
}

// AppKeys reads a GitHub App's private-key item.
type AppKeys interface {
	AppAccount(app string) (string, error)
	AppKey(app, account string) ([]byte, error)
}

// GitHub issues, refreshes and revokes a container's installation tokens. Every token it issues is in the
// container's record or the backlog until revoked or expired; revocation uses only those values, never the
// container's files. Lock guards each read-modify-write of either item, never a GitHub request. Audit (nil: none)
// gets each token's outcome, never its value.
type GitHub struct {
	Keychain SecretKeychain
	Apps     AppKeys
	API      github.Client
	Lock     func() (unlock func(), err error)
	Audit    *audit.Recorder
}

// note gives the audit recorder e's outcome for container, keyed by a hash of its token.
func (g GitHub) note(container, result, reason string, e tokenEntry, repos *audit.Repositories) {
	if g.Audit == nil {
		return
	}
	sum := sha256.Sum256([]byte(e.Token))
	expires := e.ExpiresAt
	g.Audit.Add(container, audit.Credential{Kind: audit.GitHubToken, Result: result, Reason: reason, ExpiresAt: &expires,
		Account: e.Account, Installation: e.Installation, Repos: repos}, hex.EncodeToString(sum[:]))
}

// unlisted is a new token's repositories when it could not be recorded, so was never listed.
var unlisted = audit.Repositories{Error: "not listed: the token could not be recorded"}

// issued notes a recorded token with the repositories it reaches; a failed listing is noted, never returned. Only
// after the record write: the listing's requests must not widen the window in which a token is untracked.
func (g GitHub) issued(ctx context.Context, container, result string, e tokenEntry) {
	if g.Audit == nil {
		return
	}
	repos := &audit.Repositories{}
	if selection, names, err := g.API.Repositories(ctx, e.Token); err != nil {
		repos.Error = err.Error()
	} else {
		repos.Selection, repos.Names = &selection, names
	}
	g.note(container, result, "", e, repos)
}

func (p pending) entry() tokenEntry {
	return tokenEntry{Account: p.Account, Token: p.Token, ExpiresAt: p.ExpiresAt}
}

// FileLock is an exclusive flock on path, created 0600 in a 0700 directory.
func FileLock(path string) func() (func(), error) {
	return func() (func(), error) {
		if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
			return nil, err
		}
		f, err := os.OpenFile(path, os.O_RDWR|os.O_CREATE, 0o600)
		if err != nil {
			return nil, err
		}
		if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX); err != nil {
			f.Close()
			return nil, err
		}
		return func() { f.Close() }, nil
	}
}

var errLock = errors.New("token lock")

func tokensError(err error) *protocol.Error {
	if errors.Is(err, errLock) {
		return protocol.New(protocol.KeychainFailed, "Taking the GitHub token lock ~/.ensemble/docker/tokens.lock failed.")
	}
	return protocol.New(protocol.KeychainFailed, "Keychain access to the GitHub tokens failed.")
}

func unrevoked(container string) *protocol.Error {
	return protocol.New(protocol.KeychainFailed, "The GitHub tokens of "+container+" could not be revoked; they expire within an hour.")
}

func apiError(err error) *protocol.Error {
	return protocol.New(protocol.GitHubFailed, "GitHub request failed: "+err.Error()+".")
}

func loginError(app string) *protocol.Error {
	return protocol.New(protocol.LoginRequired, "No usable GitHub App private key "+app+": run ensemble-broker login github "+AppKeyService+".")
}

func (g GitHub) locked(f func() error) error {
	unlock, err := g.Lock()
	if err != nil {
		return fmt.Errorf("%w: %v", errLock, err)
	}
	defer unlock()
	return f()
}

func (g GitHub) readRecord(container string) (tokenRecord, bool, error) {
	var rec tokenRecord
	data, err := g.Keychain.Secret(tokensService, container)
	defer erase(data)
	switch {
	case errors.Is(err, ErrNotFound):
		return rec, false, nil
	case err != nil:
		return rec, false, err
	}
	return rec, true, json.Unmarshal(data, &rec)
}

func (g GitHub) writeRecord(container string, rec tokenRecord) error {
	if len(rec.Tokens) == 0 {
		return g.Keychain.DeleteSecret(tokensService, container)
	}
	data, _ := json.Marshal(rec)
	defer erase(data)
	return g.Keychain.PutSecret(tokensService, container, data)
}

// editRecord changes container's record under the lock; f gets whether it exists, and a missing one is never created
// unless f adds tokens and create is set.
func (g GitHub) editRecord(container string, create bool, f func(rec *tokenRecord, found bool)) (bool, error) {
	var found bool
	err := g.locked(func() error {
		rec, ok, err := g.readRecord(container)
		if err != nil {
			return err
		}
		found = ok
		f(&rec, ok)
		if !ok && !create {
			return nil
		}
		return g.writeRecord(container, rec)
	})
	return found, err
}

func (g GitHub) readBacklog() ([]pending, error) {
	var list []pending
	data, err := g.Keychain.Secret(revocationsService, revocationsAccount)
	defer erase(data)
	switch {
	case errors.Is(err, ErrNotFound):
		return nil, nil
	case err != nil:
		return nil, err
	}
	return list, json.Unmarshal(data, &list)
}

// editBacklog changes the backlog; the caller holds the lock.
func (g GitHub) editBacklog(f func([]pending) []pending) error {
	list, err := g.readBacklog()
	if err != nil {
		return err
	}
	list = f(list)
	if len(list) == 0 {
		return g.Keychain.DeleteSecret(revocationsService, revocationsAccount)
	}
	data, _ := json.Marshal(list)
	defer erase(data)
	return g.Keychain.PutSecret(revocationsService, revocationsAccount, data)
}

func tokenIn(entries []tokenEntry) func(string) bool {
	return func(t string) bool {
		return slices.ContainsFunc(entries, func(e tokenEntry) bool { return e.Token == t })
	}
}

// discard retires entries (the whole record when whole) through a durable backlog transfer, or direct revocation
// if that transfer fails. The original record keeps every token not confirmed revoked, expired or queued.
func (g GitHub) discard(ctx context.Context, container string, entries []tokenEntry, whole bool) error {
	var backlogErr error
	ran := false
	err := g.locked(func() error {
		ran = true
		var recordErr error
		if whole {
			rec, found, err := g.readRecord(container)
			recordErr = err
			if err == nil && found {
				in := tokenIn(entries)
				entries = append(slices.Clone(entries), slices.DeleteFunc(rec.Tokens, func(e tokenEntry) bool { return in(e.Token) })...)
			}
		}
		backlogErr = g.editBacklog(func(list []pending) []pending {
			for _, e := range entries {
				list = append(list, pending{container, e.Account, e.Token, e.ExpiresAt})
			}
			return list
		})
		if backlogErr != nil {
			return nil // direct revocation below must finish before the original record can change
		}
		for _, e := range entries {
			g.note(container, audit.Pending, "queued for revocation", e, nil)
		}
		if whole {
			if recordErr != nil {
				return recordErr // keep unknown original material; explicit entries are safely queued
			}
			return g.Keychain.DeleteSecret(tokensService, container)
		}
		rec, found, err := g.readRecord(container)
		if err != nil || !found {
			return err
		}
		rec.Tokens = slices.DeleteFunc(rec.Tokens, func(e tokenEntry) bool { return tokenIn(entries)(e.Token) })
		return g.writeRecord(container, rec)
	})
	if !ran && whole {
		if rec, found, err := g.readRecord(container); err == nil && found {
			in := tokenIn(entries)
			entries = append(slices.Clone(entries), slices.DeleteFunc(rec.Tokens, func(e tokenEntry) bool { return in(e.Token) })...)
		}
	}
	if backlogErr != nil || !ran {
		type miss struct {
			entry  tokenEntry
			reason string
		}
		var left []pending
		var missed []miss
		var confirmed []tokenEntry
		for _, e := range entries {
			if expires, err := time.Parse(time.RFC3339, e.ExpiresAt); err == nil && !g.API.Now().Before(expires) {
				confirmed = append(confirmed, e)
				g.note(container, audit.ExpiredUnrevoked, "", e, nil)
				continue
			}
			if err := g.API.Revoke(ctx, e.Token); err != nil {
				left = append(left, pending{container, e.Account, e.Token, e.ExpiresAt})
				missed = append(missed, miss{e, err.Error()})
			} else {
				confirmed = append(confirmed, e)
				g.note(container, audit.Revoked, "", e, nil)
			}
		}
		queued := !ran && (len(left) == 0 || g.editBacklog(func(list []pending) []pending { return append(list, left...) }) == nil)
		for _, m := range missed {
			if !queued {
				m.reason += "; not queued"
			}
			g.note(container, audit.Pending, m.reason, m.entry, nil)
		}
		if !ran {
			if !queued {
				return unrevoked(container) // the record keeps them tracked
			}
			g.untrack(container, entries)
		} else {
			if len(confirmed) > 0 {
				_, removeErr := g.editRecord(container, false, func(rec *tokenRecord, _ bool) {
					in := tokenIn(confirmed)
					rec.Tokens = slices.DeleteFunc(rec.Tokens, func(e tokenEntry) bool { return in(e.Token) })
				})
				if removeErr != nil {
					return tokensError(removeErr)
				}
			}
			if len(left) > 0 {
				return unrevoked(container)
			}
		}
	} else {
		_, _ = g.Retry(ctx)
	}
	if err != nil {
		return tokensError(err)
	}
	return nil
}

// untrack takes entries, revoked or queued, out of container's record without the lock: best effort, as the lock
// is what failed.
func (g GitHub) untrack(container string, entries []tokenEntry) {
	if rec, found, err := g.readRecord(container); err == nil && found {
		in := tokenIn(entries)
		rec.Tokens = slices.DeleteFunc(rec.Tokens, func(e tokenEntry) bool { return in(e.Token) })
		_ = g.writeRecord(container, rec)
	}
}

// SaveUser asks GitHub which App the AppKeyService key is (its slug), looks up its bot user (`<slug>[bot]`) and keeps
// both, for start to check and report; out gets its one line.
func (g GitHub) SaveUser(ctx context.Context, out io.Writer) error {
	rerun := "; rerun ensemble-broker login github " + AppKeyService
	jwt, err := g.jwt(AppKeyService)
	if err != nil {
		return err
	}
	slug, err := g.API.AppSlug(ctx, jwt)
	if err != nil {
		return fmt.Errorf("looking up the GitHub App of the %s key failed: %w%s", AppKeyService, err, rerun)
	}
	login := slug + "[bot]"
	id, err := g.API.BotUser(ctx, login)
	if err != nil {
		return fmt.Errorf("looking up GitHub user %s failed: %w%s", login, err, rerun)
	}
	data, _ := json.Marshal(botUser{slug, id})
	if err := g.Keychain.PutSecret(usersService, AppKeyService, data); err != nil {
		return fmt.Errorf("saving the bot user ID of %s failed: %w", slug, err)
	}
	_, err = fmt.Fprintf(out, "GitHub App %s bot user %s has ID %d\n", slug, login, id)
	return err
}

// Ready checks, without reading the key, that app is the App login github saved and its key item is in place.
func (g GitHub) Ready(app string) error {
	if _, err := g.BotUserID(app); err != nil {
		return err
	}
	_, err := g.appID(AppKeyService)
	return err
}

// BotUserID is app's bot user ID as login github saved it; another app than the saved one is refused.
func (g GitHub) BotUserID(app string) (int64, error) {
	rerun := ": run ensemble-broker login github " + AppKeyService + "."
	missing := protocol.New(protocol.LoginRequired, "No GitHub App login"+rerun)
	data, err := g.Keychain.Secret(usersService, AppKeyService)
	switch {
	case errors.Is(err, ErrNotFound):
		return 0, missing
	case err != nil:
		return 0, protocol.New(protocol.KeychainFailed, "Keychain access to the GitHub bot user ID failed"+rerun)
	}
	var user botUser
	if json.Unmarshal(data, &user) != nil || !github.Login.MatchString(user.Slug) || user.ID < 1 {
		return 0, missing
	}
	if app != user.Slug {
		return 0, protocol.New(protocol.ManifestRejected, "Manifest rejected: github app must be "+user.Slug+", the App login github saved.")
	}
	return user.ID, nil
}

func (g GitHub) appID(app string) (string, error) {
	account, err := g.Apps.AppAccount(app)
	switch {
	case errors.Is(err, ErrNotFound), errors.Is(err, ErrAmbiguous), err == nil && !appID.MatchString(account):
		return "", loginError(app)
	case err != nil:
		return "", protocol.New(protocol.KeychainFailed, "Keychain access to the GitHub App private key failed.")
	}
	return account, nil
}

func (g GitHub) jwt(app string) (string, error) {
	id, err := g.appID(app)
	if err != nil {
		return "", err
	}
	data, err := g.Apps.AppKey(app, id)
	defer erase(data)
	switch {
	case errors.Is(err, ErrNotFound):
		return "", loginError(app)
	case err != nil:
		return "", protocol.New(protocol.KeychainFailed, "Keychain access to the GitHub App private key failed.")
	}
	key, err := github.ParseKey(data)
	if err != nil {
		return "", protocol.New(protocol.GitHubFailed, "GitHub App private key "+app+" unusable: "+err.Error()+".")
	}
	jwt, err := g.API.JWT(id, key)
	if err != nil {
		return "", protocol.New(protocol.GitHubFailed, "Signing the GitHub App token failed.")
	}
	return jwt, nil
}

// write puts e's token into the container through exec's stdin; errors wrap docker.ErrUnreachable or are opaque.
func write(exec func([]string, []byte) error, e tokenEntry) error {
	if !github.Login.MatchString(e.Account) {
		return fmt.Errorf("account %q is not a GitHub login", e.Account)
	}
	token := []byte(e.Token)
	defer erase(token)
	err := exec([]string{"/bin/sh", "-c", writeScript, "sh", e.Account}, token)
	if errors.Is(err, docker.ErrUnreachable) {
		return fmt.Errorf("GitHub token injection: %w", docker.ErrUnreachable)
	}
	if err != nil {
		return errors.New("writing a GitHub token into the container failed")
	}
	return nil
}

func expiries(entries []tokenEntry) []protocol.Expiry {
	out := []protocol.Expiry{}
	for _, e := range entries {
		out = append(out, protocol.Expiry{Account: e.Account, ExpiresAt: e.ExpiresAt})
	}
	slices.SortFunc(out, func(a, b protocol.Expiry) int {
		if a.Account < b.Account {
			return -1
		}
		if a.Account > b.Account {
			return 1
		}
		return 0
	})
	return out
}

// Issue gives container one token per installation of app, limited to permissions, recording each before writing it
// in; on any failure it revokes every token it issued.
func (g GitHub) Issue(ctx context.Context, app string, permissions map[string]string, container string, exec func([]string, []byte) error) ([]protocol.Expiry, error) {
	jwt, err := g.jwt(AppKeyService)
	if err != nil {
		return nil, err
	}
	installs, err := g.API.Installations(ctx, jwt)
	if err != nil {
		return nil, apiError(err)
	}
	var issued []tokenEntry
	fail := func(err error) ([]protocol.Expiry, error) {
		if len(issued) > 0 {
			_ = g.discard(ctx, container, issued, true)
		}
		return nil, err
	}
	for _, in := range installs {
		token, expires, err := g.API.Issue(ctx, jwt, in.ID, permissions)
		if err != nil {
			return fail(apiError(err))
		}
		e := tokenEntry{in.Account, in.ID, token, expires.Format(time.RFC3339)}
		issued = append(issued, e)
		if _, err := g.editRecord(container, true, func(rec *tokenRecord, _ bool) {
			rec.App, rec.Tokens = app, append(rec.Tokens, e)
		}); err != nil {
			g.note(container, audit.Issued, "", e, &unlisted)
			return fail(tokensError(err))
		}
		g.issued(ctx, container, audit.Issued, e)
	}
	for _, e := range issued {
		if err := write(exec, e); err != nil {
			return fail(err)
		}
	}
	return expiries(issued), nil
}

// Prefetch clones ref ("": the default branch) of repo into dir through run, which starts the helper container with
// argv and stdin. The token it gives run reads only repo's contents; it sits in container's record while in use, so
// closing container retires it should the broker die, and is revoked once run returns.
func (g GitHub) Prefetch(ctx context.Context, repo, ref, dir, container string, run func(argv []string, stdin []byte) error) error {
	jwt, err := g.jwt(AppKeyService)
	if err != nil {
		return err
	}
	in, err := g.API.RepoInstallation(ctx, jwt, repo)
	var se *github.StatusError
	if errors.As(err, &se) && se.Status == http.StatusNotFound {
		return protocol.New(protocol.GitHubFailed, "GitHub App "+AppKeyService+" is not installed on "+repo+"; install it there or drop prefetch.")
	}
	if err != nil {
		return apiError(err)
	}
	token, expires, err := g.API.IssueRepo(ctx, jwt, in.ID, repo, prefetchPermissions)
	if err != nil {
		return apiError(err)
	}
	e := tokenEntry{in.Account, in.ID, token, expires.Format(time.RFC3339)}
	if _, err := g.editRecord(container, true, func(rec *tokenRecord, _ bool) { rec.Tokens = append(rec.Tokens, e) }); err != nil {
		g.note(container, audit.Issued, "", e, &unlisted)
		_ = g.discard(context.WithoutCancel(ctx), container, []tokenEntry{e}, false)
		return tokensError(err)
	}
	g.issued(ctx, container, audit.Issued, e)
	defer g.retire(ctx, container, e)
	if ref == "" {
		if ref, err = g.API.DefaultBranch(ctx, token, repo); err != nil {
			return apiError(err)
		}
		if !launcher.ValidRef(ref) {
			return protocol.Newf(protocol.GitHubFailed, "GitHub named %q the default branch of %s, not a usable branch name.", ref[:min(len(ref), 80)], repo)
		}
	} else {
		kind, err := g.API.RefKind(ctx, token, repo, ref)
		if err != nil {
			return apiError(err)
		}
		if kind == "" {
			return protocol.Newf(protocol.ManifestRejected, "Manifest rejected: prefetch ref %s is no branch or tag of %s.", ref, repo)
		}
	}
	stdin := []byte(token)
	defer erase(stdin)
	if err := run([]string{"/bin/sh", "-c", cloneScript, "sh", repo, ref, dir}, stdin); err != nil {
		if errors.Is(err, docker.ErrUnreachable) {
			return protocol.New(protocol.DockerUnreachable, "Cannot reach Docker Desktop; start it and retry.")
		}
		failed := protocol.Newf(protocol.StartFailed, "Prefetching %s at %s failed.", repo, ref)
		failed.Cause = github.Redact(docker.Diagnostic(err), token)
		return failed
	}
	return nil
}

// retire revokes a prefetch token on a context of its own, so a cancelled start still revokes it; a token GitHub does
// not confirm revoked goes to the backlog.
func (g GitHub) retire(ctx context.Context, container string, e tokenEntry) {
	rctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 30*time.Second)
	defer cancel()
	if g.API.Revoke(rctx, e.Token) != nil {
		_ = g.discard(rctx, container, []tokenEntry{e}, false)
		return
	}
	g.note(container, audit.Revoked, "", e, nil)
	_, _ = g.editRecord(container, false, func(rec *tokenRecord, _ bool) {
		rec.Tokens = slices.DeleteFunc(rec.Tokens, func(t tokenEntry) bool { return t.Token == e.Token })
	})
}

// Refresh replaces each of container's tokens with one limited to permissions: a new one is recorded and written in
// before the old is revoked.
func (g GitHub) Refresh(ctx context.Context, app string, permissions map[string]string, container string, exec func([]string, []byte) error) ([]protocol.Expiry, error) {
	var rec tokenRecord
	var found bool
	if err := g.locked(func() (err error) {
		rec, found, err = g.readRecord(container)
		return err
	}); err != nil {
		return nil, tokensError(err)
	}
	if !found {
		return nil, protocol.New(protocol.RefreshFailed, "No GitHub tokens recorded for "+container+".")
	}
	jwt, err := g.jwt(AppKeyService)
	if err != nil {
		return nil, err
	}
	var fresh []tokenEntry
	for _, old := range rec.Tokens {
		token, expires, err := g.API.Issue(ctx, jwt, old.Installation, permissions)
		if err != nil {
			return nil, apiError(err)
		}
		e := tokenEntry{old.Account, old.Installation, token, expires.Format(time.RFC3339)}
		found, err := g.editRecord(container, false, func(rec *tokenRecord, found bool) {
			if found {
				rec.Tokens = append(rec.Tokens, e)
			}
		})
		if err != nil || !found {
			g.note(container, audit.Renewed, "", e, &unlisted)
			_ = g.discard(ctx, container, []tokenEntry{e}, false)
			if err != nil {
				return nil, tokensError(err)
			}
			return nil, protocol.New(protocol.RefreshFailed, container+" was closed during the refresh.")
		}
		g.issued(ctx, container, audit.Renewed, e)
		if err := write(exec, e); err != nil {
			_ = g.discard(ctx, container, []tokenEntry{e}, false)
			return nil, err
		}
		if g.API.Revoke(ctx, old.Token) != nil {
			_ = g.discard(ctx, container, []tokenEntry{old}, false)
		} else {
			g.note(container, audit.Revoked, "", old, nil)
			_, _ = g.editRecord(container, false, func(rec *tokenRecord, _ bool) {
				rec.Tokens = slices.DeleteFunc(rec.Tokens, func(t tokenEntry) bool { return t.Token == old.Token })
			})
		}
		fresh = append(fresh, e)
	}
	return expiries(fresh), nil
}

// Revoke retires every token of container's record. An unreadable record is kept for recovery.
func (g GitHub) Revoke(ctx context.Context, container string) error {
	var found bool
	err := g.locked(func() (err error) {
		_, found, err = g.readRecord(container)
		return err
	})
	if errors.Is(err, errLock) {
		return g.discard(ctx, container, nil, true)
	}
	if err != nil {
		return unrevoked(container)
	}
	if !found {
		_, _ = g.Retry(ctx)
		return nil
	}
	return g.discard(ctx, container, nil, true)
}

// Pending projects a single Keychain snapshot without locking, mutation, remote requests or audit.
func (g GitHub) Pending() ([]protocol.Pending, error) {
	list, err := g.readBacklog()
	if err != nil {
		return nil, tokensError(err)
	}
	out := make([]protocol.Pending, 0, len(list))
	for _, p := range list {
		out = append(out, protocol.Pending{Container: p.Container, Account: p.Account, ExpiresAt: p.ExpiresAt})
	}
	return out, nil
}

// Retry revokes the backlog: expired entries are dropped unrevoked, and the first request that gets no answer
// stops the run, keeping the rest for next time. Audit gets every entry's outcome; it keeps what it needs.
func (g GitHub) Retry(ctx context.Context) ([]protocol.Pending, error) {
	var list []pending
	if err := g.locked(func() (err error) {
		list, err = g.readBacklog()
		return err
	}); err != nil {
		return nil, tokensError(err)
	}
	now := g.API.Now()
	type outcome struct {
		entry  pending
		result string
	}
	var done []string
	var resolved []outcome // done's: noted once the backlog no longer holds them, so a failed removal notes nothing twice
	for i, p := range list {
		expires, err := time.Parse(time.RFC3339, p.ExpiresAt)
		if err == nil && !now.Before(expires) {
			done, resolved = append(done, p.Token), append(resolved, outcome{p, audit.ExpiredUnrevoked})
			continue
		}
		err = g.API.Revoke(ctx, p.Token)
		if err == nil {
			done, resolved = append(done, p.Token), append(resolved, outcome{p, audit.Revoked})
			continue
		}
		if github.Transport(err) {
			for _, rest := range list[i:] {
				g.note(rest.Container, audit.Pending, err.Error(), rest.entry(), nil)
			}
			break
		}
		g.note(p.Container, audit.Pending, err.Error(), p.entry(), nil)
	}
	if len(done) > 0 {
		if err := g.locked(func() error {
			return g.editBacklog(func(list []pending) []pending {
				return slices.DeleteFunc(list, func(p pending) bool { return slices.Contains(done, p.Token) })
			})
		}); err != nil {
			return nil, tokensError(err)
		}
	}
	for _, o := range resolved {
		g.note(o.entry.Container, o.result, "", o.entry.entry(), nil)
	}
	out := []protocol.Pending{}
	for _, p := range list {
		if !slices.Contains(done, p.Token) {
			out = append(out, protocol.Pending{Container: p.Container, Account: p.Account, ExpiresAt: p.ExpiresAt})
		}
	}
	return out, nil
}
