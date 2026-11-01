package main

import (
	"bytes"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"time"

	"ensemble/broker/internal/audit"
	"ensemble/broker/internal/broker"
	"ensemble/broker/internal/credential"
	"ensemble/broker/internal/launcher"
	"ensemble/broker/internal/protocol"
)

// staticStore is the account's record of its static secrets' versions and expiries.
func staticStore(home string) credential.StaticStore {
	dir := filepath.Join(home, ".ensemble/docker")
	return credential.StaticStore{Path: filepath.Join(dir, "static-secrets.json"), Lock: broker.FileLock(filepath.Join(dir, "static-secrets.lock"), true)}
}

// datedFlags are the login options each static kind takes beside its name.
var datedFlags = map[credential.Kind]map[string]bool{
	credential.Claude:   {"--expires": true, "--issued": true, "--fresh-token": false},
	credential.Opencode: {"--expires": true, "--auth-file": true},
	credential.Env:      {"--expires": true},
}

// datedLogin parses login claude NAME | opencode PROVIDER | env NAME with at least one of its expiry options, in
// any order: nil when args is no such login, which is usage. A malformed date or a repeated option is the returned
// login's error.
func datedLogin(args []string, a app, stdout io.Writer) func(keys) error {
	if len(args) < 3 || args[0] != "login" {
		return nil
	}
	var kind credential.Kind
	name, rest := args[2], args[3:]
	switch {
	case args[1] == "claude" && launcher.ValidClaudeAccount(name):
		kind = credential.Claude
	case args[1] == "opencode" && launcher.ValidProvider(name):
		kind = credential.Opencode
	case args[1] == "env" && launcher.ValidEnvName(name):
		kind = credential.Env
	default:
		return nil
	}
	values := map[string]string{}
	var repeated string
	dated := false
	for len(rest) > 0 {
		flag := rest[0]
		takesValue, ok := datedFlags[kind][flag]
		if !ok || takesValue && len(rest) < 2 {
			return nil
		}
		if _, seen := values[flag]; seen && repeated == "" {
			repeated = flag
		}
		values[flag], rest = "", rest[1:]
		if takesValue {
			values[flag], rest = rest[0], rest[1:]
		}
		dated = dated || flag != "--auth-file"
	}
	if authFile, ok := values["--auth-file"]; !dated || ok && !isPath(authFile) {
		return nil
	}
	return func(k keys) error {
		if repeated != "" {
			return fmt.Errorf("%s given twice", repeated)
		}
		var reg credential.Registration
		_, reg.Fresh = values["--fresh-token"]
		for flag, date := range map[string]*time.Time{"--expires": &reg.Expires, "--issued": &reg.Issued} {
			if value, ok := values[flag]; ok {
				d, err := credential.ParseDate(value)
				if err != nil {
					return err
				}
				*date = d
			}
		}
		return a.dated(k, kind, name, values["--auth-file"], reg, stdout)
	}
}

// stdinReaders read a login's secret and then the keychain password. A terminal on stdin types the secret as one
// hidden line after prompt; anything else carries it (a token: one line, its newline dropped), and the password comes
// from the controlling terminal. Neither depends on a confirmation read from stdin.
func stdinReaders(stdin *os.File, prompt string, token bool) (read, hidden func() ([]byte, error)) {
	if info, err := stdin.Stat(); err == nil && info.Mode()&os.ModeCharDevice != 0 {
		return func() ([]byte, error) {
			fmt.Fprint(os.Stderr, prompt)
			defer fmt.Fprintln(os.Stderr)
			return broker.ReadHidden()
		}, broker.ReadHidden
	}
	return func() ([]byte, error) {
		value, err := readEnvValue(stdin)
		if token && err == nil {
			value = bytes.TrimSuffix(bytes.TrimSuffix(value, []byte("\n")), []byte("\r"))
		}
		return value, err
	}, broker.ReadHiddenTTY
}

// staticLogin records a static secret's login: its version and expiry in store, and the notices due to the host in
// log.
type staticLogin struct {
	store credential.StaticStore
	log   audit.Appender
	now   func() time.Time
}

func newStaticLogin(home string) staticLogin {
	return staticLogin{store: staticStore(home), log: audit.Log{Path: audit.Path(home)}, now: time.Now}
}

// register runs login, the Keychain login of ref's secret, inside the store's registration, then prints the recorded
// expiry and any notice due to the host to out. An audit failure is a warning on stderr: the login stands.
func (s staticLogin) register(ref string, reg credential.Registration, out, stderr io.Writer, login func() error) error {
	now := s.now()
	record, err := s.store.Register(ref, reg, now, login)
	if err != nil {
		return err
	}
	line := "Expiry unknown"
	if record.ExpiresAt != "" {
		expires, _ := time.Parse(time.RFC3339, record.ExpiresAt)
		line = "Expires " + credential.Day(expires) + " (" + string(record.Provenance) + ")"
	}
	if _, err := fmt.Fprintln(out, line); err != nil {
		return err
	}
	rec := &audit.Recorder{Command: "login", Now: s.now, Log: s.log}
	var printErr error
	if err := s.store.DeliverHost(ref, now, func(n credential.Notice) {
		rec.Notice("", n.Audit())
		if _, err := fmt.Fprintln(out, n.Text); err != nil && printErr == nil {
			printErr = err
		}
	}); err != nil {
		return err
	}
	if err := rec.Flush(nil); err != nil {
		fmt.Fprintln(stderr, "ensemble-broker: "+protocol.AuditWriteFailed+": the broker audit log was not written; the login stands")
	}
	return printErr
}
