package main

import (
	"bytes"
	"fmt"
	"os"
	"path/filepath"

	"ensemble/broker/internal/broker"
	"ensemble/broker/internal/credential"
)

// staticStore is the account's record of its static secrets' versions and expiries.
func staticStore(home string) credential.StaticStore {
	dir := filepath.Join(home, ".ensemble/docker")
	return credential.StaticStore{Path: filepath.Join(dir, "static-secrets.json"), Lock: broker.FileLock(filepath.Join(dir, "static-secrets.lock"), true)}
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
