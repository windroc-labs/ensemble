package broker

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"syscall"
	"time"

	"ensemble/broker/internal/codex"
	"ensemble/broker/internal/credential"
	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/protocol"
)

const (
	codexService        = "ensemble-broker-codex"
	codexAccount        = "auth"
	maxCodexAuth        = 64 * 1024
	codexLoginCommand   = InstalledPath + " login codex"
	codexInstallCommand = "sudo " + InstalledPath + " install-codex <version>"
	codexUpdateCommand  = "sudo " + InstalledPath + " update-codex <version>"
)

func codexRepair(err error) string {
	if errors.Is(err, codex.ErrNotInstalled) {
		return codexInstallCommand
	}
	return codexUpdateCommand
}

type codexNative interface {
	Verify() (codex.Installation, error)
	Run(context.Context, string, ...string) error
	Refresh(context.Context, string) error
	Lock(context.Context) (func(), error)
}

type codexKeychain interface {
	AppKeychain
	Secret(string, string) ([]byte, error)
	CreateCodex() error
	WriteCodex([]byte) error
}

// Codex holds the one Codex login and each container's grant of it (codex_grants.go); grantsLock guards the grant
// records only, taken while the account lock (native.Lock) is held, never around it.
type Codex struct {
	native      codexNative
	keychain    codexKeychain
	grants      credential.Store
	grantsLock  func() (unlock func(), err error)
	password    func() ([]byte, error)
	revisionDir string
	tempRoot    string
	now         func() time.Time
	uid         func() int
}

// NewCodex is the Codex login of the account at home, its items in k.
func NewCodex(home string, k SystemKeychain, password func() ([]byte, error)) Codex {
	return Codex{native: codex.New(), keychain: k, grants: k,
		grantsLock: FileLock(filepath.Join(home, ".ensemble/docker/codex-grants.lock")), password: password,
		revisionDir: filepath.Join(home, ".ensemble/docker/codex-refresh"), tempRoot: "/private/tmp", now: time.Now, uid: os.Geteuid}
}

type codexAuthTokens struct {
	ID      string `json:"id_token"`
	Access  string `json:"access_token"`
	Refresh string `json:"refresh_token"`
	Account string `json:"account_id"`
}

type codexAuthDocument struct {
	Mode        string          `json:"auth_mode"`
	APIKey      *string         `json:"OPENAI_API_KEY"`
	Tokens      codexAuthTokens `json:"tokens"`
	LastRefresh string          `json:"last_refresh"`
}

func parseCodexAuth(data []byte) (time.Time, error) {
	invalid := errors.New("invalid Codex subscription credentials; rerun " + codexLoginCommand)
	if len(data) == 0 || len(data) > maxCodexAuth {
		return time.Time{}, invalid
	}
	var auth codexAuthDocument
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&auth) != nil {
		return time.Time{}, invalid
	}
	var extra any
	if decoder.Decode(&extra) != io.EOF {
		return time.Time{}, invalid
	}
	if auth.APIKey != nil || (auth.Mode != "" && auth.Mode != "chatgpt") {
		return time.Time{}, invalid
	}
	for _, s := range []string{auth.Tokens.ID, auth.Tokens.Access, auth.Tokens.Refresh, auth.Tokens.Account} {
		if len(s) == 0 || len(s) > maxToken || strings.ContainsFunc(s, func(r rune) bool { return r <= 0x20 || r == 0x7f }) {
			return time.Time{}, invalid
		}
	}
	if _, err := time.Parse(time.RFC3339, auth.LastRefresh); err != nil {
		return time.Time{}, invalid
	}
	parts := strings.Split(auth.Tokens.Access, ".")
	if len(parts) != 3 || parts[0] == "" || parts[2] == "" {
		return time.Time{}, invalid
	}
	claims, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return time.Time{}, invalid
	}
	defer erase(claims)
	var token struct {
		Exp int64 `json:"exp"`
	}
	if json.Unmarshal(claims, &token) != nil || token.Exp <= 0 || token.Exp > 253402300799 {
		return time.Time{}, invalid
	}
	return time.Unix(token.Exp, 0).UTC(), nil
}

func readCodexAuth(home string) ([]byte, error) {
	f, err := os.OpenFile(filepath.Join(home, "auth.json"), os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
	if err != nil {
		return nil, errors.New("Codex did not create a private credential file")
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return nil, errors.New("cannot inspect Codex credential file")
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || !info.Mode().IsRegular() || info.Mode().Perm()&0o077 != 0 || stat.Uid != uint32(os.Geteuid()) || stat.Nlink != 1 || info.Size() > maxCodexAuth {
		return nil, errors.New("Codex credential file is not private")
	}
	data, err := io.ReadAll(io.LimitReader(f, maxCodexAuth+1))
	if err != nil || len(data) > maxCodexAuth {
		erase(data)
		return nil, errors.New("cannot read Codex credential file")
	}
	return data, nil
}

func (c Codex) Login(ctx context.Context, out io.Writer) (result error) {
	if c.uid() == 0 {
		return errors.New("run " + codexLoginCommand + " as your ordinary user, without sudo")
	}
	if _, err := c.native.Verify(); err != nil {
		return errors.New("pinned Codex unavailable; run " + codexRepair(err))
	}
	unlock, err := c.native.Lock(ctx)
	if err != nil {
		return errors.New("cannot lock Codex login; install pinned Codex or retry")
	}
	defer unlock()
	revision, err := c.beginRevision()
	if err != nil {
		return err
	}
	defer func() {
		revision.State = "failed"
		if result == nil {
			revision.State = "complete"
		}
		if err := c.writeRevision(revision); err != nil {
			result = errors.Join(result, err)
		}
	}()
	_, err = c.keychain.AppAccess(codexService, codexAccount)
	missing := errors.Is(err, ErrNotFound)
	if err != nil && !missing {
		return errors.New("cannot inspect Codex keychain access; retry " + codexLoginCommand)
	}
	// Every terminal question comes before the browser, which the user may not come back from: the item is
	// created and made to trust only the broker first, and after the browser the password is never asked.
	if missing {
		if c.keychain.CreateCodex() != nil {
			return errors.New("cannot create Codex keychain item")
		}
	}
	password := onePassword{read: c.password}
	defer password.erase()
	secure := func() error {
		err := secureItem(codexService, codexAccount, "retry "+codexLoginCommand, c.keychain, password.get)
		switch {
		case errors.Is(err, ErrInvalidOwnerEdit):
			return err
		case errors.Is(err, errAccessUnread):
			return errors.New("cannot inspect Codex keychain access")
		case err != nil:
			return errors.New("Codex keychain trust incomplete; retry " + codexLoginCommand)
		}
		return nil
	}
	if err := secure(); err != nil {
		return err
	}
	password.close()
	var previous []byte
	defer func() { erase(previous) }()
	if !missing {
		old, err := c.keychain.Secret(codexService, codexAccount)
		if err != nil {
			erase(old)
			return errors.New("cannot read existing Codex login; retry " + codexLoginCommand)
		}
		if expiry, err := parseCodexAuth(old); err == nil && c.now().Before(expiry) {
			previous = old
		} else {
			erase(old)
		}
	}
	home, err := os.MkdirTemp(c.tempRoot, "ensemble-codex-")
	if err != nil {
		return errors.New("cannot create private Codex login directory")
	}
	defer os.RemoveAll(home)
	if err := os.Chmod(home, 0o700); err != nil {
		return errors.New("cannot protect Codex login directory")
	}
	if _, err := fmt.Fprintln(out, "Opening Codex browser login..."); err != nil {
		return err
	}
	if err := c.native.Run(ctx, home, "-c", `cli_auth_credentials_store="file"`, "-c", `forced_login_method="chatgpt"`, "login"); err != nil {
		return errors.New("Codex browser login failed or was canceled; retry " + codexLoginCommand)
	}
	data, err := readCodexAuth(home)
	defer erase(data)
	if err != nil {
		return err
	}
	expires, err := parseCodexAuth(data)
	if err != nil {
		return err
	}
	if !c.now().Before(expires) {
		return errors.New("Codex returned expired credentials; retry " + codexLoginCommand)
	}
	if err := ctx.Err(); err != nil {
		return errors.New("Codex login canceled")
	}
	if err := os.RemoveAll(home); err != nil {
		return errors.New("cannot remove private Codex login directory; login was not saved")
	}
	if ctx.Err() != nil {
		return errors.New("Codex login canceled")
	}
	if err := c.keychain.WriteCodex(data); err != nil {
		if errors.Is(err, ErrInvalidOwnerEdit) {
			return ownershipError(codexService, codexAccount, err)
		}
		return errors.New("cannot save Codex login; retry " + codexLoginCommand)
	}
	if c.trusted() != nil || !c.matches(data) {
		if len(previous) > 0 {
			if secure() != nil || c.keychain.WriteCodex(previous) != nil || secure() != nil || !c.matches(previous) {
				return errors.New("Codex login verification failed and previous login could not be restored; retry " + codexLoginCommand)
			}
			return errors.New("Codex login verification failed; previous login restored; retry " + codexLoginCommand)
		}
		return errors.New("Codex login verification failed; retry " + codexLoginCommand)
	}
	_, err = fmt.Fprintln(out, "Codex login saved; keychain trusts only the broker")
	return err
}

// onePassword reads the keychain password at most once, and never once closed: login codex closes it before the
// browser. get returns a copy, since secureItem erases what it gets.
type onePassword struct {
	read  func() ([]byte, error)
	value []byte
	err   error
	done  bool
}

func (p *onePassword) get() ([]byte, error) {
	if !p.done {
		p.done = true
		p.value, p.err = p.read()
	}
	return bytes.Clone(p.value), p.err
}

func (p *onePassword) close() {
	if !p.done {
		p.done, p.err = true, errors.New("no password question after the browser")
	}
}

func (p *onePassword) erase() { erase(p.value) }

func (c Codex) matches(expected []byte) bool {
	saved, err := c.keychain.Secret(codexService, codexAccount)
	defer erase(saved)
	return err == nil && bytes.Equal(saved, expected)
}

func (c Codex) trusted() error {
	a, err := c.keychain.AppAccess(codexService, codexAccount)
	if err != nil || onlyBroker(a) != nil {
		return errors.New("Codex keychain trust incomplete; retry " + codexLoginCommand)
	}
	return nil
}

// loginRepair is the command that makes the login usable when trusted fails: retrust for an earlier broker build's
// item, else login codex.
func (c Codex) loginRepair() string {
	if _, earlier, err := checkItem(c.keychain, codexService, codexAccount); err == nil && earlier {
		return RetrustCommand
	}
	return codexLoginCommand
}

// codexWriterMessages are bootstrap.py's fixed codex-auth exit messages. The writer runs in the untrusted container,
// so writerCause passes on only these, or a write failure's errno text.
var (
	codexWriterMessages = []string{
		"bootstrap: invalid Codex home",
		"bootstrap: CODEX_HOME does not match the manifest",
		"bootstrap: invalid managed Codex credentials",
		"bootstrap: managed Codex secret directory is a link",
		"bootstrap: CODEX_HOME/auth.json links elsewhere",
		"bootstrap: CODEX_HOME/auth.json is not a regular file",
		"bootstrap: managed Codex credential is not a regular file",
		"bootstrap: existing Codex credentials are unreadable",
		"bootstrap: existing Codex credentials belong to another account",
	}
	codexWriteFailure = regexp.MustCompile(`^bootstrap: cannot write managed Codex credentials: [A-Za-z '/-]{1,64}$`)
)

func writerCause(err error) string {
	var ce *docker.CommandError
	if !errors.As(err, &ce) {
		return "credential writer failed"
	}
	if line := docker.Diagnostic(err); slices.Contains(codexWriterMessages, line) || codexWriteFailure.MatchString(line) {
		return line
	}
	return fmt.Sprintf("credential writer exited %d without a known diagnostic", ce.Exit)
}

func (c Codex) Status(ctx context.Context) protocol.CodexStatus {
	s := protocol.CodexStatus{State: "installation_invalid"}
	installed, err := c.native.Verify()
	if err != nil {
		s.Repair = codexRepair(err)
		return s
	}
	s.Version, s.Path, s.SHA256 = installed.Version, installed.Path, installed.SHA256
	s.State, s.Repair = "login_unavailable", codexLoginCommand
	ctx, cancel := context.WithTimeout(ctx, 250*time.Millisecond)
	defer cancel()
	unlock, err := c.native.Lock(ctx)
	if err != nil {
		s.State = "busy"
		s.Repair = "retry ensemble-broker status"
		return s
	}
	defer unlock()
	if c.trusted() != nil {
		s.Repair = c.loginRepair()
		return s
	}
	data, err := c.keychain.Secret(codexService, codexAccount)
	defer erase(data)
	if err != nil {
		return s
	}
	expires, err := parseCodexAuth(data)
	if err != nil {
		return s
	}
	s.ExpiresAt = expires.Format(time.RFC3339)
	if !c.now().Before(expires) {
		s.State = "expired"
		return s
	}
	s.State, s.Repair, s.LoggedIn = "ready", "", true
	return s
}
