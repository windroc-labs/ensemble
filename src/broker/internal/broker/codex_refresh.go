package broker

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"time"

	"ensemble/broker/internal/codex"
	"ensemble/broker/internal/protocol"
)

const codexRefreshAhead = 600 * time.Second

func (c Codex) renew(ctx context.Context, previous []byte, old codexAuthDocument, expiry time.Time) ([]byte, error) {
	home, err := os.MkdirTemp(c.tempRoot, "ensemble-codex-refresh-")
	if err != nil {
		return nil, errors.New("cannot create private Codex refresh home")
	}
	defer os.RemoveAll(home)
	if os.Chmod(home, 0o700) != nil || os.WriteFile(filepath.Join(home, "auth.json"), previous, 0o600) != nil {
		return nil, errors.New("cannot stage private Codex login")
	}
	if err := c.native.Refresh(ctx, home); err != nil {
		if errors.Is(err, codex.ErrRefreshVersion) {
			return nil, errors.New(codex.ErrRefreshVersion.Error() + "; run " + strings.Replace(codexUpdateCommand, "<version>", codex.MinVersion, 1))
		}
		return nil, errors.New("native Codex refresh failed; retry or run " + codexLoginCommand)
	}
	data, err := readCodexAuth(home)
	if err != nil {
		return nil, err
	}
	valid := false
	defer func() {
		if !valid {
			erase(data)
		}
	}()
	var auth codexAuthDocument
	next, err := parseCodexAuth(data)
	if err != nil || json.Unmarshal(data, &auth) != nil {
		return nil, errors.New("native Codex refresh returned invalid credentials")
	}
	stamp, _ := time.Parse(time.RFC3339, auth.LastRefresh)
	prior, _ := time.Parse(time.RFC3339, old.LastRefresh)
	if auth.Tokens.Account != old.Tokens.Account || auth.Tokens.Access == old.Tokens.Access || !next.After(expiry) || !next.After(c.now()) || stamp.Before(prior) || stamp.After(c.now()) {
		return nil, errors.New("native Codex refresh did not preserve and renew the account; run " + codexLoginCommand)
	}
	if ctx.Err() != nil {
		return nil, errors.New("Codex refresh canceled")
	}
	if os.RemoveAll(home) != nil {
		return nil, errors.New("cannot remove private Codex refresh home")
	}
	if c.trusted() != nil || c.keychain.WriteCodex(data) != nil || c.trusted() != nil || !c.matches(data) {
		return nil, errors.New("cannot verify saved Codex refresh; run " + codexLoginCommand)
	}
	valid = true
	return data, nil
}

func (c Codex) refreshError(err error) error {
	return protocol.New(protocol.RefreshFailed, err.Error())
}
