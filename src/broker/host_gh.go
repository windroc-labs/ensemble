package main

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"time"

	"ensemble/broker/internal/broker"
)

// hostReply is host-gh-login's output (src/broker/protocol/host-gh/protocol.schema.json): never the token.
type hostReply struct {
	Version   int    `json:"version"`
	Account   string `json:"account"`
	ExpiresAt string `json:"expires_at"`
}

// ghLoginTimeout bounds the host gh's login, which only stores the token and asks GitHub who it is.
var ghLoginTimeout = 60 * time.Second

// maxGHOutput caps what a failed gh login's output adds to the error.
const maxGHOutput = 4096

// tokenVariables are the variables gh would prefer over the token it stores: the login's gh gets none.
var tokenVariables = []string{"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"}

// hostGHSystem is host-only: it issues the host App's token into the gh at path, through its stdin, never into a
// container.
func hostGHSystem(ctx context.Context, k keys, gh string) (any, error) {
	if err := broker.CheckInstalled(); err != nil {
		return nil, err
	}
	rec := recorder(k.Home, "host-gh-login", broker.HostContainer)
	environ := os.Environ()
	return withAudit(rec, func() (any, error) {
		expiry, err := tokens(k, rec).HostLogin(ctx, func(token []byte) error { return ghLogin(ctx, gh, token, environ) })
		if err != nil {
			return nil, err
		}
		return hostReply{Version: 1, Account: expiry.Account, ExpiresAt: expiry.ExpiresAt}, nil
	})
}

// ghLogin runs gh's login with token on its stdin and environ without tokenVariables, then requires gh to have kept
// the token out of its plain-text hosts.yml.
func ghLogin(ctx context.Context, gh string, token []byte, environ []string) error {
	ctx, cancel := context.WithTimeout(ctx, ghLoginTimeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, gh, "auth", "login", "--hostname", "github.com", "--with-token")
	for _, kv := range environ {
		name, _, _ := strings.Cut(kv, "=")
		if !slices.Contains(tokenVariables, name) {
			cmd.Env = append(cmd.Env, kv)
		}
	}
	cmd.Stdin = bytes.NewReader(token)
	var out bytes.Buffer
	cmd.Stdout, cmd.Stderr = &out, &out
	if err := cmd.Run(); err != nil {
		text := out.String()
		if len(text) > maxGHOutput {
			text = text[:maxGHOutput]
		}
		return fmt.Errorf("gh auth login: %v: %s", err, strings.TrimSpace(text))
	}
	if insecureStorage(environ) {
		return errors.New("gh stored the token in plain text in hosts.yml, not in the keyring: run gh auth logout --hostname github.com, then rerun any gh")
	}
	return nil
}

// insecureStorage is whether gh's hosts.yml under environ holds an oauth_token in its github.com block: gh keeps a
// token there only when it could not use the keyring. gh's config dir is GH_CONFIG_DIR, else XDG_CONFIG_HOME/gh,
// else ~/.config/gh.
func insecureStorage(environ []string) bool {
	env := map[string]string{}
	for _, kv := range environ {
		if name, value, ok := strings.Cut(kv, "="); ok {
			env[name] = value
		}
	}
	dir := env["GH_CONFIG_DIR"]
	switch {
	case dir != "":
	case env["XDG_CONFIG_HOME"] != "":
		dir = filepath.Join(env["XDG_CONFIG_HOME"], "gh")
	default:
		dir = filepath.Join(env["HOME"], ".config", "gh")
	}
	f, err := os.Open(filepath.Join(dir, "hosts.yml"))
	if err != nil {
		return false
	}
	defer f.Close()
	block := false
	for lines := bufio.NewScanner(f); lines.Scan(); {
		line := lines.Text()
		if line != "" && line[0] != ' ' && line[0] != '\t' {
			block = strings.TrimSpace(line) == "github.com:"
			continue
		}
		if block && strings.HasPrefix(strings.TrimSpace(line), "oauth_token:") {
			return true
		}
	}
	return false
}
