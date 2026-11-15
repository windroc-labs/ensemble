package main

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"ensemble/broker/internal/protocol"
)

func TestHostGHLoginDispatch(t *testing.T) {
	var gotGH string
	var interactive []bool
	a := app{hostGH: func(_ context.Context, _ keys, gh string) (any, error) {
		gotGH = gh
		return hostReply{Version: 1, Account: "windroc-labs", ExpiresAt: "2026-11-15T13:00:00Z"}, nil
	}}
	a.keychain = func(i bool) (keys, error) {
		interactive = append(interactive, i)
		return keys{Home: "/nonexistent"}, nil
	}
	code, out, errOut := invoke(a, "host-gh-login", "--gh", "/opt/homebrew/bin/gh")
	if code != 0 || errOut != "" || gotGH != "/opt/homebrew/bin/gh" || !slices.Equal(interactive, []bool{false}) {
		t.Fatalf("%d %q %q gh=%q interactive=%v", code, out, errOut, gotGH, interactive)
	}
	var reply map[string]any
	if err := json.Unmarshal([]byte(out), &reply); err != nil || len(reply) != 3 || reply["version"] != 1.0 ||
		reply["account"] != "windroc-labs" || reply["expires_at"] != "2026-11-15T13:00:00Z" {
		t.Fatalf("reply %q", out)
	}
}

func TestHostGHLoginUsage(t *testing.T) {
	a := app{hostGH: func(context.Context, keys, string) (any, error) {
		t.Fatal("dispatched")
		return nil, nil
	}}
	for _, args := range [][]string{
		{"host-gh-login"},
		{"host-gh-login", "--gh"},
		{"host-gh-login", "--gh", "gh"},
		{"host-gh-login", "--gh", "bin/gh"},
		{"host-gh-login", "--gh", "/opt/homebrew/bin/gh", "x"},
		{"host-gh-login", "--path", "/opt/homebrew/bin/gh"},
	} {
		code, out, errOut := invoke(a, args...)
		if code != 2 || out != "" || errOut != "ensemble-broker: usage: ensemble-broker host-gh-login --gh PATH\n" {
			t.Fatalf("%q: %d %q %q", args, code, out, errOut)
		}
	}
}

func TestHostGHLoginFailurePrintsOneLine(t *testing.T) {
	a := app{hostGH: func(context.Context, keys, string) (any, error) {
		return nil, protocol.New(protocol.LoginRequired, "No usable GitHub App private key ensemble-broker-host-bot: run ensemble-broker login github ensemble-broker-host-bot.")
	}}
	code, out, errOut := invoke(a, "host-gh-login", "--gh", "/opt/homebrew/bin/gh")
	want := "ensemble-broker: login_required: No usable GitHub App private key ensemble-broker-host-bot: run ensemble-broker login github ensemble-broker-host-bot.\n"
	if code != 1 || out != "" || errOut != want {
		t.Fatalf("%d %q %q", code, out, errOut)
	}
}

// fakeGH is a gh that records its argv, stdin and environment in dir, then prints what and exits as code says.
func fakeGH(t *testing.T, dir, code string) string {
	t.Helper()
	path := filepath.Join(dir, "gh")
	script := "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"" + dir + "/args\"\ncat > \"" + dir + "/stdin\"\nenv > \"" + dir +
		"/env\"\necho 'gh says no' >&2\nexit " + code + "\n"
	if err := os.WriteFile(path, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	return path
}

func read(t *testing.T, path string) string {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

func TestGHLoginHandsTheTokenOnStdinOnly(t *testing.T) {
	dir := t.TempDir()
	gh := fakeGH(t, dir, "0")
	environ := []string{"HOME=" + dir, "PATH=/usr/bin:/bin", "GH_CONFIG_DIR=" + dir, "GH_TOKEN=old", "GITHUB_TOKEN=old",
		"GH_ENTERPRISE_TOKEN=old", "GITHUB_ENTERPRISE_TOKEN=old", "KEEP=1"}
	if err := ghLogin(context.Background(), gh, []byte("ghs_SECRET"), environ); err != nil {
		t.Fatal(err)
	}
	if got := read(t, dir+"/args"); got != "auth\nlogin\n--hostname\ngithub.com\n--with-token\n" {
		t.Fatalf("argv %q", got)
	}
	if got := read(t, dir+"/stdin"); got != "ghs_SECRET" {
		t.Fatalf("stdin %q", got)
	}
	env := read(t, dir+"/env")
	if strings.Contains(env, "ghs_SECRET") || strings.Contains(env, "TOKEN=") || !strings.Contains(env, "KEEP=1") ||
		!strings.Contains(env, "GH_CONFIG_DIR="+dir) {
		t.Fatalf("env %q", env)
	}
}

func TestGHLoginFailures(t *testing.T) {
	dir := t.TempDir()
	environ := []string{"HOME=" + dir, "GH_CONFIG_DIR=" + dir}
	err := ghLogin(context.Background(), fakeGH(t, dir, "1"), []byte("ghs_SECRET"), environ)
	if err == nil || !strings.Contains(err.Error(), "gh says no") {
		t.Fatalf("a failing gh: %v", err)
	}
	if err := os.WriteFile(filepath.Join(dir, "hosts.yml"), []byte("github.com:\n    user: host-bot[bot]\n    oauth_token: ghs_SECRET\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	err = ghLogin(context.Background(), fakeGH(t, dir, "0"), []byte("ghs_SECRET"), environ)
	if err == nil || !strings.Contains(err.Error(), "plain text") || !strings.Contains(err.Error(), "gh auth logout --hostname github.com") {
		t.Fatalf("insecure storage: %v", err)
	}
}

func TestInsecureStorageIsAnOAuthTokenInGitHubsBlock(t *testing.T) {
	home := t.TempDir()
	write := func(dir, text string) {
		t.Helper()
		if err := os.MkdirAll(dir, 0o700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, "hosts.yml"), []byte(text), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	keyring := "github.com:\n    git_protocol: https\n    users:\n        host-bot[bot]:\n    user: host-bot[bot]\n"
	plain := "github.com:\n    users:\n        host-bot[bot]:\n            oauth_token: ghs_x\n    user: host-bot[bot]\n"
	other := "ghe.example:\n    oauth_token: x\ngithub.com:\n    user: me\n"
	for name, c := range map[string]struct {
		environ []string
		dir     string
		text    string
		want    bool
	}{
		"keyring":              {[]string{"HOME=" + home}, home + "/.config/gh", keyring, false},
		"plain text":           {[]string{"HOME=" + home}, home + "/.config/gh", plain, true},
		"another host's token": {[]string{"HOME=" + home}, home + "/.config/gh", other, false},
		"GH_CONFIG_DIR":        {[]string{"HOME=" + home, "GH_CONFIG_DIR=" + home + "/c"}, home + "/c", plain, true},
		"XDG_CONFIG_HOME":      {[]string{"HOME=" + home, "XDG_CONFIG_HOME=" + home + "/x"}, home + "/x/gh", plain, true},
		"no file":              {[]string{"HOME=" + home + "/none"}, "", "", false},
	} {
		if c.dir != "" {
			write(c.dir, c.text)
		}
		if got := insecureStorage(c.environ); got != c.want {
			t.Errorf("%s: %v", name, got)
		}
		if c.dir != "" {
			_ = os.RemoveAll(c.dir)
		}
	}
}
