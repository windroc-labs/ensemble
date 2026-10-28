package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"testing"
	"testing/iotest"
	"time"

	"ensemble/broker/internal/audit"
	"ensemble/broker/internal/broker"
	"ensemble/broker/internal/credential"
	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/fixtures"
	"ensemble/broker/internal/github"
	"ensemble/broker/internal/launcher"
	"ensemble/broker/internal/protocol"
)

type call struct{ command, arg string }

func recording(calls *[]call) app {
	result := func(name string) step {
		return func(_ context.Context, _ keys, arg string, _ io.Writer) (any, error) {
			*calls = append(*calls, call{name, arg})
			return map[string]string{"ok": name}, nil
		}
	}
	build, workspaceRm := result("build"), result("workspace-rm")
	return app{build: func(ctx context.Context, k keys, arg string, _ launcher.BuildSpec, _ time.Duration, p io.Writer) (any, error) {
		return build(ctx, k, arg, p)
	},
		start: result("start"), close: result("close"),
		lockQuery: func(ctx context.Context, k keys, name string) (any, error) {
			return result("lock-query")(ctx, k, name, nil)
		},
		maintain: func(ctx context.Context, k keys, container, _ string, _ []string, _ bool) (any, error) {
			return result("maintain")(ctx, k, container, nil)
		},
		status: func(ctx context.Context, k keys, _ bool) (any, error) { return result("status")(ctx, k, "", nil) },
		probeOpencode: func(ctx context.Context, k keys, provider, base string) (any, error) {
			return result("probe-provider")(ctx, k, provider+" "+base, nil)
		},
		workspaceRm: func(ctx context.Context, k keys, volume string, _ bool) (any, error) {
			return workspaceRm(ctx, k, volume, nil)
		},
		checkClients: func(ctx context.Context, k keys, p io.Writer) (any, error) {
			return result("check-clients")(ctx, k, "", p)
		}}
}

// invoke runs args on a, with a keychain and an old-name check that find nothing unless a sets its own.
func invoke(a app, args ...string) (int, string, string) {
	if a.keychain == nil {
		a.keychain = func(bool) (keys, error) { return keys{Home: "/nonexistent"}, nil }
	}
	if a.legacy == nil {
		a.legacy = func() ([]string, error) { return nil, nil }
	}
	var out, errOut bytes.Buffer
	code := run(context.Background(), args, &out, &errOut, a)
	return code, out.String(), errOut.String()
}

func TestVersion(t *testing.T) {
	code, out, errOut := invoke(app{}, "version")
	if code != 0 || out != `{"protocol_version":"34"}`+"\n" || errOut != "" {
		t.Fatalf("%d %q %q", code, out, errOut)
	}
	for _, c := range fixtures.Cases(t, "version_output") {
		if c.Valid && !fixtures.SameJSON(t, []byte(out), c.Data) {
			t.Fatalf("%s: %s", c.File, out)
		}
	}
}

func TestLockQueryCommand(t *testing.T) {
	calls := 0
	a := app{lockQuery: func(_ context.Context, _ keys, name string) (any, error) {
		calls++
		if name != "dummy" {
			t.Fatalf("query %q", name)
		}
		return launcher.LockResult{}, nil
	}}
	code, out, errOut := invoke(a, "lock-query", "--lock", "dummy")
	if code != 0 || out != "{\"holder\":null}\n" || errOut != "" || calls != 1 {
		t.Fatalf("%d %q %q calls=%d", code, out, errOut, calls)
	}
	for _, args := range [][]string{{"lock-query"}, {"lock-query", "--lock", "A B"}, {"lock-query", "--lock", ""}, {"lock-query", "--lock", "dummy", "extra"}} {
		code, out, errOut = invoke(a, args...)
		if code != 2 || out != "" || calls != 1 {
			t.Fatalf("%q: %d %q %q", args, code, out, errOut)
		}
	}
	a.start = func(context.Context, keys, string, io.Writer) (any, error) {
		e := protocol.New(protocol.Busy, "Lock held by container (worker_1).")
		e.Holder = &protocol.Holder{Container: "container", Owner: "worker_1"}
		return nil, e
	}
	code, out, errOut = invoke(a, "start", "--manifest", "/tmp/manifest.json")
	if code != 75 || out != "" || !strings.Contains(errOut, `"holder":{"container":"container","owner":"worker_1"}`) {
		t.Fatalf("%d %q %q", code, out, errOut)
	}
}

func TestCheckClientsCommand(t *testing.T) {
	calls := 0
	a := app{checkClients: func(_ context.Context, _ keys, progress io.Writer) (any, error) {
		calls++
		fmt.Fprintln(progress, "ensemble-broker: removed ensemble-worker:x")
		return launcher.CheckResult{Latest: launcher.ClientVersions{Claude: "2.1.7", Codex: "0.170.0", Opencode: "1.20.1"},
			Rebuilt: []string{"ensemble-worker:" + strings.Repeat("a", 64)}}, nil
	}}
	code, out, errOut := invoke(a, "check-clients")
	if code != 0 || errOut != "ensemble-broker: removed ensemble-worker:x\n" || calls != 1 {
		t.Fatalf("%d %q %q calls=%d", code, out, errOut, calls)
	}
	for _, c := range fixtures.Cases(t, "check_clients_output") {
		if c.File == "check-clients-output.json" && !fixtures.SameJSON(t, []byte(out), c.Data) {
			t.Fatalf("%s: %s", c.File, out)
		}
	}
	code, out, errOut = invoke(a, "check-clients", "--now")
	if code != 2 || calls != 1 {
		t.Fatalf("extra args: %d %q %q", code, out, errOut)
	}
	assertError(t, errOut, out, "check-clients", protocol.Usage)
	a.checkClients = func(context.Context, keys, io.Writer) (any, error) {
		return nil, protocol.New(protocol.BuildFailed, "Cannot look up the latest client releases.")
	}
	code, out, errOut = invoke(a, "check-clients")
	assertError(t, errOut, out, "check-clients", protocol.BuildFailed)
	if code != 8 {
		t.Fatalf("exit %d", code)
	}
	_, _, errOut = invoke(app{}, "frobnicate")
	if !strings.Contains(errOut, " | check-clients") {
		t.Fatalf("usage %q", errOut)
	}
}

func TestLoginOwnershipFailureExit(t *testing.T) {
	a := app{loginOpencode: func(keys, string, string, io.Writer) error {
		return fmt.Errorf("cannot save item: %w", broker.ErrInvalidOwnerEdit)
	}}
	code, out, errOut := invoke(a, "login", "opencode", "strata")
	if code != 11 || out != "" || !strings.Contains(errOut, "keychain status -25244") {
		t.Fatalf("%d %q %q", code, out, errOut)
	}
}

func TestProbeOpencodeCommand(t *testing.T) {
	calls := 0
	a := app{probeOpencode: func(_ context.Context, _ keys, provider, base string) (any, error) {
		calls++
		if provider != "strata" || base != "https://strata.test/v1" {
			t.Fatal("wrong probe input")
		}
		return map[string]bool{"available": true}, nil
	}}
	code, out, errOut := invoke(a, "probe-provider", "--provider", "strata", "--base-url", "https://strata.test/v1")
	if code != 0 || out != "{\"available\":true}\n" || errOut != "" || calls != 1 {
		t.Fatalf("%d %s %s", code, out, errOut)
	}
	for _, args := range [][]string{{"probe-provider"}, {"probe-provider", "--provider", "../bad", "--base-url", "https://strata.test/v1"}, {"probe-provider", "--provider", "strata", "--base-url", "file:///tmp/key"}} {
		code, _, _ = invoke(a, args...)
		if code != 2 || calls != 1 {
			t.Fatal("invalid probe dispatched")
		}
	}
}

func TestInputFixtures(t *testing.T) {
	for _, c := range fixtures.Cases(t, "version_input", "build_input", "start_input", "maintain_input", "close_input", "status_input", "workspace_rm_input", "probe_provider_input", "lock_query_input", "check_clients_input") {
		t.Run(c.File, func(t *testing.T) {
			var in struct{ Argv []string }
			if err := json.Unmarshal(c.Data, &in); err != nil {
				t.Fatal(err)
			}
			var calls []call
			code, out, errOut := invoke(recording(&calls), in.Argv...)
			if code != *c.Expected.ExitCode {
				t.Fatalf("exit %d want %d: %s", code, *c.Expected.ExitCode, errOut)
			}
			if c.Valid {
				want := call{in.Argv[0], ""}
				switch in.Argv[0] {
				case "probe-provider":
					want.arg = in.Argv[2] + " " + in.Argv[4]
				case "version", "status", "check-clients":
				default:
					want.arg = in.Argv[2]
				}
				if errOut != "" || out == "" || (in.Argv[0] != "version" && (len(calls) != 1 || calls[0] != want)) {
					t.Fatalf("%q %q %v", out, errOut, calls)
				}
				return
			}
			assertError(t, errOut, out, in.Argv[0], protocol.Code(*c.Expected.ErrorCode))
			if len(calls) != 0 {
				t.Fatalf("dispatched %v", calls)
			}
		})
	}
}

func assertError(t *testing.T, errOut, out, command string, code protocol.Code) {
	t.Helper()
	var got struct {
		ExitCode int `json:"exit_code"`
		Error    struct {
			Command string        `json:"command"`
			Code    protocol.Code `json:"code"`
			Cleanup string        `json:"cleanup"`
		} `json:"error"`
	}
	if out != "" || strings.Count(errOut, "\n") != 1 || json.Unmarshal([]byte(errOut), &got) != nil {
		t.Fatalf("stdout %q stderr %q", out, errOut)
	}
	if got.Error.Command != command || got.Error.Code != code || got.ExitCode != protocol.Exit(code) || got.Error.Cleanup != protocol.NotNeeded {
		t.Fatalf("%+v", got)
	}
}

func TestUsageErrors(t *testing.T) {
	var calls []call
	a := recording(&calls)
	for _, tc := range []struct {
		command string
		args    []string
	}{
		{"unknown", nil},
		{"unknown", []string{"frobnicate"}},
		{"build", []string{"build", "--ensemble"}},
		{"build", []string{"build", "--package", "/x"}},
		{"build", []string{"build", "--ensemble", "relative"}},
		{"build", []string{"build", "--ensemble", "/x", "--max-age"}},
		{"build", []string{"build", "--ensemble", "/x", "--max-age", "-1"}},
		{"build", []string{"build", "--ensemble", "/x", "--max-age", "1.5"}},
		{"build", []string{"build", "--ensemble", "/x", "--max-age", "1234567890"}},
		{"build", []string{"build", "--ensemble", "/x", "--max-age", "1", "x"}},
		{"build", []string{"build", "--ensemble", "/x", "--max-ages", "1"}},
		{"build", []string{"build", "--ensemble", "/x", "--plugin"}},
		{"build", []string{"build", "--ensemble", "/x", "--plugin", "../x"}},
		{"build", []string{"build", "--ensemble", "/x", "--plugin", "a@"}},
		{"build", []string{"build", "--ensemble", "/x", "--max-age", "1", "--plugin", "a"}},
		{"build", []string{"build", "--ensemble", "/x", "--image-plugin"}},
		{"build", []string{"build", "--ensemble", "/x", "--image-plugin", "../x"}},
		{"build", []string{"build", "--ensemble", "/x", "--image-plugin", ".x"}},
		{"build", []string{"build", "--ensemble", "/x", "--image-plugin", "a@o/r#aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}},
		{"build", []string{"build", "--ensemble", "/x", "--image-plugin", "a", "--plugin", "b@o/r#aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}},
		{"build", []string{"build", "--ensemble", "/x", "--max-age", "1", "--image-plugin", "a"}},
		{"build", []string{"build", "--ensemble", "/x", "--base-image"}},
		{"build", []string{"build", "--ensemble", "/x", "--base-image", "-x"}},
		{"build", []string{"build", "--ensemble", "/x", "--base-image", "Dummy:test"}},
		{"build", []string{"build", "--ensemble", "/x", "--base-image", "localhost:5000/env:1"}},
		{"build", []string{"build", "--ensemble", "/x", "--base-image", "env@sha256:" + strings.Repeat("c", 64)}},
		{"build", []string{"build", "--ensemble", "/x", "--base-image", strings.Repeat("a", 256)}},
		{"build", []string{"build", "--ensemble", "/x", "--base-image", "a", "--base-image", "b"}},
		{"build", []string{"build", "--ensemble", "/x", "--plugin", "b@o/r#aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "--base-image", "a"}},
		{"start", []string{"start", "--manifest", "/bad\npath"}},
		{"start", []string{"start", "--manifest", "/bad\xffpath"}},
		{"close", []string{"close", "--container", "other"}},
		{"maintain", []string{"maintain", "--container", "ensemble-x-0123456789abcdef0123456789abcdef"}},
		{"maintain", []string{"maintain", "--container", "other", "--manifest", "/m.json"}},
		{"maintain", []string{"maintain", "--manifest", "/m.json", "--container", "ensemble-x-0123456789abcdef0123456789abcdef"}},
		{"maintain", []string{"maintain", "--container", "ensemble-x-0123456789abcdef0123456789abcdef", "--manifest", "m.json"}},
		{"status", []string{"status", "x"}},
		{"close", []string{"close", "--container", "ensemble-x-0123"}},
		{"version", []string{"version", "x"}},
		{"workspace-rm", []string{"workspace-rm"}},
		{"workspace-rm", []string{"workspace-rm", "--volume"}},
		{"workspace-rm", []string{"workspace-rm", "--volume", ".hidden"}},
		{"workspace-rm", []string{"workspace-rm", "--volume", "a/b"}},
		{"workspace-rm", []string{"workspace-rm", "--volume", strings.Repeat("v", 256)}},
		{"workspace-rm", []string{"workspace-rm", "--volume", "v", "--forced"}},
		{"workspace-rm", []string{"workspace-rm", "--force", "--volume", "v"}},
		{"unknown", []string{"login"}},
		{"unknown", []string{"login", "github"}},
		{"unknown", []string{"login", "github", "ensemble-other"}},
		{"unknown", []string{"login", "github", "ensemble-engineer"}},
		{"unknown", []string{"login", "github", "ensemble-docs"}},
		{"unknown", []string{"login", "github", "ensemble-broker-bot", "x"}},
		{"unknown", []string{"login", "github", "ensemble-broker-bot", "--key-file", "/k.pem"}},
		{"unknown", []string{"login", "github", "ensemble-broker-bot", "--key-file", "/k.pem", "--app-id"}},
		{"unknown", []string{"login", "github", "ensemble-broker-bot", "--app-id", "1", "--key-file", "/k.pem"}},
		{"unknown", []string{"login", "github", "ensemble-broker-bot", "--key-file", "/k.pem", "--app", "1"}},
		{"unknown", []string{"login", "github", "ensemble-other", "--key-file", "/k.pem", "--app-id", "1"}},
		{"unknown", []string{"login", "claude", "Work"}},
		{"unknown", []string{"login", "claude", "setup-token"}},
		{"unknown", []string{"login", "claude", "a", "b"}},
		{"unknown", []string{"login", "claude", ""}},
		{"unknown", []string{"login", "opencode"}},
		{"unknown", []string{"login", "opencode", "ollama"}},
		{"unknown", []string{"login", "opencode", "a/b"}},
		{"unknown", []string{"login", "opencode", "strata", "x"}},
		{"unknown", []string{"login", "env"}},
		{"unknown", []string{"login", "env", ""}},
		{"unknown", []string{"login", "env", "Dummy"}},
		{"unknown", []string{"login", "env", "a/b"}},
		{"unknown", []string{"login", "env", "dummy", "value"}},
		{"unknown", []string{"retrust", "x"}},
	} {
		code, out, errOut := invoke(a, tc.args...)
		if code != 2 {
			t.Fatalf("%q: exit %d", tc.args, code)
		}
		assertError(t, errOut, out, tc.command, protocol.Usage)
	}
	if len(calls) != 0 {
		t.Fatalf("dispatched %v", calls)
	}
}

func TestLoginGitHubDispatch(t *testing.T) {
	for _, name := range []string{"ensemble-broker-bot"} {
		var got string
		a := app{loginGitHub: func(ctx context.Context, _ keys, app string, key *keyFile, out io.Writer) error {
			if ctx == nil {
				t.Error("no context")
			}
			if key != nil {
				t.Errorf("key %+v without flags", key)
			}
			got = app
			_, err := io.WriteString(out, "ok\n")
			return err
		}}
		if code, out, errOut := invoke(a, "login", "github", name); code != 0 || got != name || out != "ok\n" || errOut != "" {
			t.Fatalf("%s: exit %d app %q %q %q", name, code, got, out, errOut)
		}
	}
	a := app{loginGitHub: func(context.Context, keys, string, *keyFile, io.Writer) error { return errors.New("no item") }}
	if code, out, errOut := invoke(a, "login", "github", "ensemble-broker-bot"); code != 1 || out != "" || errOut != "ensemble-broker: no item\n" {
		t.Fatalf("exit %d %q %q", code, out, errOut)
	}
}

func TestLoginClaudeDispatch(t *testing.T) {
	for want, args := range map[string][]string{"default": {"login", "claude"}, "work": {"login", "claude", "work"}, "a.b-1_2": {"login", "claude", "a.b-1_2"}} {
		var got string
		a := app{login: func(_ keys, account string, out io.Writer) error {
			got = account
			_, err := io.WriteString(out, "ok\n")
			return err
		}}
		if code, out, errOut := invoke(a, args...); code != 0 || got != want || out != "ok\n" || errOut != "" {
			t.Fatalf("%q: exit %d account %q %q %q", args, code, got, out, errOut)
		}
	}
	a := app{login: func(keys, string, io.Writer) error { return errors.New("no terminal") }}
	if code, out, errOut := invoke(a, "login", "claude", "work"); code != 1 || out != "" || errOut != "ensemble-broker: no terminal\n" {
		t.Fatalf("exit %d %q %q", code, out, errOut)
	}
	_, _, errOut := invoke(app{}, "login", "claude", "Work")
	var got struct{ Error struct{ Message string } }
	if err := json.Unmarshal([]byte(errOut), &got); err != nil || !strings.Contains(got.Error.Message, " | login claude [NAME] | ") {
		t.Fatalf("usage %q", errOut)
	}
}

func TestLoginOpencodeDispatch(t *testing.T) {
	var got, file string
	a := app{loginOpencode: func(_ keys, provider, authFile string, out io.Writer) error {
		got, file = provider, authFile
		_, err := io.WriteString(out, "ok\n")
		return err
	}}
	if code, out, errOut := invoke(a, "login", "opencode", "strata"); code != 0 || got != "strata" || out != "ok\n" || errOut != "" {
		t.Fatalf("exit %d provider %q %q %q", code, got, out, errOut)
	}
	if code, out, errOut := invoke(a, "login", "opencode", "strata", "--auth-file", "/tmp/auth.json"); code != 0 || got != "strata" || file != "/tmp/auth.json" || out != "ok\n" || errOut != "" {
		t.Fatalf("exit %d provider %q path %q %q %q", code, got, file, out, errOut)
	}
	for _, args := range [][]string{
		{"strata", "--auth-file"}, {"strata", "--auth-file", "relative"},
		{"ollama", "--auth-file", "/tmp/auth.json"}, {"strata", "--other", "/tmp/auth.json"},
		{"strata", "--auth-file", "/tmp/auth.json", "extra"},
	} {
		if code, _, _ := invoke(a, append([]string{"login", "opencode"}, args...)...); code != 2 {
			t.Fatalf("accepted %q", args)
		}
	}
	a = app{loginOpencode: func(keys, string, string, io.Writer) error { return errors.New("no terminal") }}
	if code, out, errOut := invoke(a, "login", "opencode", "strata"); code != 1 || out != "" || errOut != "ensemble-broker: no terminal\n" {
		t.Fatalf("exit %d %q %q", code, out, errOut)
	}
}

func TestReadOpencodeAuth(t *testing.T) {
	path := filepath.Join(t.TempDir(), "auth.json")
	key := "sk-SENTINEL-import"
	for name, tc := range map[string]struct {
		data string
		ok   bool
	}{
		"api":              {`{"other":{"type":"oauth","access":"ignored"},"strata":{"type":"api","key":"` + key + `"}}`, true},
		"missing provider": {`{"other":{"type":"api","key":"` + key + `"}}`, false},
		"oauth":            {`{"strata":{"type":"oauth","key":"` + key + `"}}`, false},
		"empty":            {`{"strata":{"type":"api","key":""}}`, false},
		"missing key":      {`{"strata":{"type":"api"}}`, false},
		"wrong key type":   {`{"strata":{"type":"api","key":["` + key + `"]}}`, false},
		"wrong entry":      {`{"strata":"` + key + `"}`, false},
		"null entry":       {`{"strata":null}`, false},
		"array":            {`["` + key + `"]`, false},
		"null":             {`null`, false},
		"invalid":          {`{"strata":` + key, false},
		"trailing":         {`{"strata":{"type":"api","key":"` + key + `"}} {}`, false},
		"too large":        {strings.Repeat(" ", (1<<20)+1), false},
	} {
		t.Run(name, func(t *testing.T) {
			if err := os.WriteFile(path, []byte(tc.data), 0o600); err != nil {
				t.Fatal(err)
			}
			data, err := readOpencodeAuth(path, "strata")
			defer clear(data)
			if tc.ok {
				if err != nil || string(data) != key {
					t.Fatal("API entry not read")
				}
			} else if err == nil || len(data) != 0 || strings.Contains(err.Error(), key) {
				t.Fatal("invalid auth entry accepted or exposed")
			}
		})
	}
	for _, file := range []string{path + ".missing", filepath.Dir(path)} {
		if data, err := readOpencodeAuth(file, "strata"); err == nil || len(data) != 0 {
			t.Fatal("unreadable file accepted")
		}
	}
}

func TestCodexCommands(t *testing.T) {
	for _, command := range []string{"install-codex", "update-codex"} {
		called := false
		a := app{installCodex: func(ctx context.Context, version string, update bool) (any, error) {
			called = true
			if version != "0.162.1" || update != (command == "update-codex") {
				t.Fatal("wrong install arguments")
			}
			return map[string]string{"version": version}, nil
		}}
		code, out, errOut := invoke(a, command, "0.162.1")
		if code != 0 || !called || out != "{\"version\":\"0.162.1\"}\n" || errOut != "" {
			t.Fatalf("%d %q %q", code, out, errOut)
		}
		for _, args := range [][]string{{command}, {command, "latest"}, {command, "0.162.1", "extra"}, {command, "0.162.1;touch /tmp/no"}} {
			called = false
			code, _, _ := invoke(a, args...)
			if code != 2 || called {
				t.Fatal("invalid admin command dispatched")
			}
		}
	}
	called := false
	a := app{loginCodex: func(ctx context.Context, _ keys, out io.Writer) error {
		called = true
		_, err := io.WriteString(out, "saved\n")
		return err
	}}
	if code, out, errOut := invoke(a, "login", "codex"); code != 0 || !called || out != "saved\n" || errOut != "" {
		t.Fatalf("%d %q %q", code, out, errOut)
	}
	a.loginCodex = func(context.Context, keys, io.Writer) error { return errors.New("login failed") }
	if code, out, errOut := invoke(a, "login", "codex"); code != 1 || out != "" || errOut != "ensemble-broker: login failed\n" {
		t.Fatalf("%d %q %q", code, out, errOut)
	}
	a.installCodex = func(context.Context, string, bool) (any, error) { return nil, errors.New("install failed") }
	if code, out, errOut := invoke(a, "install-codex", "0.162.1"); code != 1 || out != "" || errOut != "ensemble-broker: install failed\n" {
		t.Fatalf("%d %q %q", code, out, errOut)
	}
}

func TestCodexStatusFixtures(t *testing.T) {
	for _, c := range fixtures.Cases(t, "status_output") {
		if !c.Valid {
			continue
		}
		var result launcher.StatusResult
		if err := json.Unmarshal(c.Data, &result); err != nil {
			t.Fatal(err)
		}
		var metadata struct {
			AuditWarning string `json:"audit_warning"`
		}
		_ = json.Unmarshal(c.Data, &metadata)
		var response any = result
		if metadata.AuditWarning != "" {
			response = auditWarningResult{result}
		}
		var out bytes.Buffer
		if emit(&out, response) != 0 {
			t.Fatal("status encoding failed")
		}
		var original, encoded any
		if json.Unmarshal(c.Data, &original) != nil || json.Unmarshal(out.Bytes(), &encoded) != nil || !reflect.DeepEqual(original, encoded) {
			t.Fatalf("status changed %s", out.String())
		}
	}
}

func TestBuildMaxAge(t *testing.T) {
	for want, args := range map[time.Duration][]string{
		-1:                      nil,
		0:                       {"--max-age", "0"},
		7 * 24 * time.Hour:      {"--max-age", "604800"},
		999999999 * time.Second: {"--max-age", "999999999"},
	} {
		got := time.Duration(-2)
		a := app{build: func(_ context.Context, _ keys, _ string, _ launcher.BuildSpec, maxAge time.Duration, _ io.Writer) (any, error) {
			got = maxAge
			return map[string]string{}, nil
		}}
		if code, _, errOut := invoke(a, append([]string{"build", "--ensemble", "/pkg"}, args...)...); code != 0 || got != want {
			t.Fatalf("%q: exit %d max age %v want %v: %s", args, code, got, want, errOut)
		}
	}
}

func TestBuildPlugins(t *testing.T) {
	widget := "widget@vendor/plugin#aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa::vendor/catalogue#cccccccccccccccccccccccccccccccccccccccc"
	b, a := "b@o/r#aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "a@o/m.x#bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
	for _, c := range []struct {
		args   []string
		spec   launcher.BuildSpec
		maxAge time.Duration
	}{
		{nil, launcher.BuildSpec{}, -1},
		{[]string{"--plugin", widget}, launcher.BuildSpec{Plugins: []string{widget}}, -1},
		{[]string{"--plugin", b, "--plugin", a, "--max-age", "60"}, launcher.BuildSpec{Plugins: []string{b, a}}, time.Minute},
		{[]string{"--image-plugin", "dummy-plugin"}, launcher.BuildSpec{ImagePlugins: []string{"dummy-plugin"}}, -1},
		{[]string{"--plugin", b, "--image-plugin", "z", "--image-plugin", "a.b_c-d", "--max-age", "0"}, launcher.BuildSpec{Plugins: []string{b}, ImagePlugins: []string{"z", "a.b_c-d"}}, 0},
		{[]string{"--base-image", "dummy-env:test"}, launcher.BuildSpec{BaseImage: "dummy-env:test"}, -1},
		{[]string{"--base-image", "sha256:" + strings.Repeat("c", 64), "--plugin", b, "--image-plugin", "z", "--max-age", "60"}, launcher.BuildSpec{BaseImage: "sha256:" + strings.Repeat("c", 64), Plugins: []string{b}, ImagePlugins: []string{"z"}}, time.Minute},
	} {
		var spec launcher.BuildSpec
		maxAge := time.Duration(-2)
		app := app{build: func(_ context.Context, _ keys, _ string, s launcher.BuildSpec, m time.Duration, _ io.Writer) (any, error) {
			spec, maxAge = s, m
			return map[string]string{}, nil
		}}
		if code, _, errOut := invoke(app, append([]string{"build", "--ensemble", "/pkg"}, c.args...)...); code != 0 || !slices.Equal(spec.Plugins, c.spec.Plugins) ||
			!slices.Equal(spec.ImagePlugins, c.spec.ImagePlugins) || spec.BaseImage != c.spec.BaseImage || maxAge != c.maxAge {
			t.Fatalf("%q: exit %d spec %+v max age %v: %s", c.args, code, spec, maxAge, errOut)
		}
	}
}

func TestWorkspaceRmForce(t *testing.T) {
	for want, args := range map[bool][]string{false: {"workspace-rm", "--volume", "v"}, true: {"workspace-rm", "--volume", "v", "--force"}} {
		got := !want
		a := app{workspaceRm: func(_ context.Context, _ keys, volume string, force bool) (any, error) {
			got = force
			return map[string]any{"volume": volume, "removed": true}, nil
		}}
		if code, out, errOut := invoke(a, args...); code != 0 || got != want || out != `{"removed":true,"volume":"v"}`+"\n" {
			t.Fatalf("%q: exit %d force %v: %q %s", args, code, got, out, errOut)
		}
	}
}

func TestChannels(t *testing.T) {
	failing := app{build: func(context.Context, keys, string, launcher.BuildSpec, time.Duration, io.Writer) (any, error) {
		return nil, protocol.New(protocol.BuildFailed, "stage failed")
	}, close: func(context.Context, keys, string, io.Writer) (any, error) {
		return nil, errors.New("surprise")
	}}
	code, out, errOut := invoke(failing, "build", "--ensemble", "/pkg")
	if code != 8 {
		t.Fatal(code)
	}
	assertError(t, errOut, out, "build", protocol.BuildFailed)
	code, out, errOut = invoke(failing, "close", "--container", "ensemble-w-0123456789abcdef0123456789abcdef")
	if code != 70 {
		t.Fatal(code)
	}
	assertError(t, errOut, out, "close", protocol.InternalError)
}

func TestPanicIsInternalError(t *testing.T) {
	a := app{start: func(context.Context, keys, string, io.Writer) (any, error) { panic("boom") }}
	code, out, errOut := invoke(a, "start", "--manifest", "/m.json")
	if code != 70 {
		t.Fatal(code)
	}
	assertError(t, errOut, out, "start", protocol.InternalError)
	if strings.Contains(errOut, "cause") || strings.Contains(errOut, "boom") {
		t.Fatal(errOut)
	}
}

func TestAccountHomeIgnoresHOME(t *testing.T) {
	t.Setenv("HOME", "/hostile")
	home, err := accountHome()
	if err != nil || home == "/hostile" || !strings.HasPrefix(home, "/") {
		t.Fatalf("%q %v", home, err)
	}
}

type memKeychain struct{ token []byte }

// AppAccess is an item trusting only the broker.
func (k *memKeychain) AppAccess(string, string) (broker.AppAccess, error) {
	return broker.AppAccess{Broker: "b", Partition: "p", Entries: []broker.ACLEntry{
		{Authorizations: []string{"decrypt"}, Apps: []string{"b"}},
		{Authorizations: []string{"partition_id"}, Partitions: []string{"p"}},
	}}, nil
}
func (k *memKeychain) Get(string) ([]byte, error) { return append([]byte(nil), k.token...), nil }

type scripted struct {
	calls [][]string
	stdin [][]byte
	fail  string
}

func (s *scripted) Run(_ context.Context, args []string, stdin []byte) ([]byte, error) {
	s.calls = append(s.calls, args)
	s.stdin = append(s.stdin, append([]byte(nil), stdin...))
	joined := strings.Join(args, " ")
	if s.fail != "" && strings.Contains(joined, s.fail) {
		return nil, &docker.CommandError{Exit: 1, Stderr: "failed: " + string(stdin)}
	}
	proxyID := strings.Repeat("e", 64)
	switch {
	case strings.HasPrefix(joined, "ps --all") && strings.Contains(joined, "--filter label=windroc.egress.contract "):
		return []byte(proxyID + "\n"), nil
	case joined == "container inspect "+proxyID:
		return []byte(`[{"Id":"` + proxyID + `","Name":"/egress-proxy","State":{"Running":true,"Health":{"Status":"healthy"}},` +
			`"Config":{"Labels":{"windroc.egress.contract":"1","windroc.egress.networks":"workers",` +
			`"windroc.egress.network.workers.name":"egress-workers","windroc.egress.network.workers.proxy":"http://egress-proxy-workers:4750"}},` +
			`"NetworkSettings":{"Networks":{"egress-workers":{"NetworkID":"` + strings.Repeat("f", 64) + `"}}}}]`), nil
	case strings.HasPrefix(joined, "network inspect"):
		return []byte(`[{"Name":"egress-workers","Id":"` + strings.Repeat("f", 64) + `","Internal":true}]`), nil
	case strings.HasPrefix(joined, "image inspect"):
		return []byte(`[{"Id":"sha256:` + strings.Repeat("b", 64) + `","Config":{"Labels":{"ensemble.key":"` + strings.Repeat("a", 64) +
			`","ensemble.version":"1.2.3","ensemble.agent-uid":"1001","ensemble.agent-gid":"1002"}}}]`), nil
	case strings.HasPrefix(joined, "volume inspect"):
		return []byte(`{"ensemble.kind":"ws","ensemble.workspace":"/Users/example/src/project"}`), nil
	case strings.HasPrefix(joined, "container inspect"):
		return nil, &docker.CommandError{Exit: 1, Stderr: "No such container"}
	case strings.HasSuffix(joined, "--version"):
		if strings.Contains(joined, "codex --version") {
			return []byte("codex-cli 0.100.0\n"), nil
		}
		return []byte("1.0.0\n"), nil
	}
	return nil, nil
}

func TestTokenNeverLeaves(t *testing.T) {
	const token = "sk-ant-oat01-NEVER-PRINT-ME"
	manifest, err := os.ReadFile(filepath.Join(fixtures.Dir(), "manifest-valid.json"))
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "m.json")
	if err := os.WriteFile(path, manifest, 0o600); err != nil {
		t.Fatal(err)
	}
	const home = "/Users/example"
	for _, fail := range []string{"", "cat >", "codex --version"} {
		d := &scripted{fail: fail}
		h := launcher.Host{
			Home: home, Endpoint: "unix://" + home + "/.docker/run/docker.sock",
			IsDir:  func(string) bool { return true },
			Safe:   func(string) error { return nil },
			Docker: d,
			Creds:  broker.Credentials{Keychain: &memKeychain{token: []byte(token)}},
			GitHub: broker.GitHub{Keychain: noSecrets{}, API: github.Client{Now: time.Now}, Lock: broker.FileLock(filepath.Join(t.TempDir(), "tokens.lock"))},
		}
		a := app{start: func(ctx context.Context, _ keys, m string, _ io.Writer) (any, error) { return h.Start(ctx, m) }}
		_, out, errOut := invoke(a, "start", "--manifest", path)
		if strings.Contains(out+errOut, token) {
			t.Fatalf("token in output: %q %q", out, errOut)
		}
		injected := 0
		for i, c := range d.calls {
			if strings.Contains(strings.Join(c, " "), token) {
				t.Fatalf("token in argv %q", c)
			}
			if strings.Contains(string(d.stdin[i]), token) {
				injected++
			}
		}
		if injected != 1 && fail != "codex --version" {
			t.Fatalf("%q: token written %d times", fail, injected)
		}
	}
}

func TestBuildProgressGoesToStderr(t *testing.T) {
	result := map[string]any{"image": "ensemble-worker:k", "digest": "sha256:d", "rebuilt": true}
	build := func(fail bool) app {
		return app{build: func(_ context.Context, _ keys, _ string, _ launcher.BuildSpec, _ time.Duration, progress io.Writer) (any, error) {
			_, _ = io.WriteString(progress, "#1 [1/3] FROM base\n#2 DONE\n")
			if fail {
				return nil, protocol.New(protocol.BuildFailed, "stage failed")
			}
			return result, nil
		}}
	}
	code, out, errOut := invoke(build(false), "build", "--ensemble", "/pkg")
	want, _ := json.Marshal(result)
	if code != 0 || out != string(want)+"\n" || errOut != "#1 [1/3] FROM base\n#2 DONE\n" {
		t.Fatalf("%d %q %q", code, out, errOut)
	}
	code, out, errOut = invoke(build(true), "build", "--ensemble", "/pkg")
	progress, envelope, _ := strings.Cut(errOut, "#2 DONE\n")
	if code != 8 || progress != "#1 [1/3] FROM base\n" {
		t.Fatalf("%d %q", code, errOut)
	}
	assertError(t, envelope, out, "build", protocol.BuildFailed)
}

func TestStatus(t *testing.T) {
	a := app{status: func(context.Context, keys, bool) (any, error) {
		return launcher.StatusResult{Revocations: []protocol.Pending{{Container: "c", Account: "acme", ExpiresAt: "2026-10-17T13:00:00Z"}}}, nil
	}}
	code, out, errOut := invoke(a, "status")
	if code != 0 || errOut != "" || out != `{"revocations":[{"container":"c","account":"acme","expires_at":"2026-10-17T13:00:00Z"}]}`+"\n" {
		t.Fatalf("%d %q %q", code, out, errOut)
	}
	a = app{status: func(context.Context, keys, bool) (any, error) { return nil, protocol.New(protocol.KeychainFailed, "x") }}
	code, out, errOut = invoke(a, "status")
	if code != 11 {
		t.Fatalf("%d %q", code, errOut)
	}
	assertError(t, errOut, out, "status", protocol.KeychainFailed)
}

func TestStatusCleanupOption(t *testing.T) {
	var calls []call
	a := recording(&calls)
	var choices []bool
	a.status = func(ctx context.Context, k keys, cleanup bool) (any, error) {
		choices = append(choices, cleanup)
		return recording(&calls).status(ctx, k, cleanup)
	}
	code, out, errOut := invoke(a, "status", "--cleanup")
	if code != 0 || len(calls) != 1 || out == "" || errOut != "" {
		t.Fatalf("explicit cleanup not dispatched: %d %q %q %v", code, out, errOut, calls)
	}
	for _, args := range [][]string{{"status", "--other"}, {"status", "--cleanup", "--cleanup"}} {
		if code, _, _ := invoke(recording(&calls), args...); code != 2 || len(calls) != 1 {
			t.Fatal("invalid status options dispatched")
		}
	}
	if code, _, _ := invoke(a, "status"); code != 0 || !slices.Equal(choices, []bool{true, false}) {
		t.Fatalf("wrong status choices: %v", choices)
	}
}

// noSecrets is an empty broker-only keychain.
type noSecrets struct{}

func (noSecrets) Secret(string, string) ([]byte, error)  { return nil, broker.ErrNotFound }
func (noSecrets) PutSecret(string, string, []byte) error { return errors.New("read-only") }
func (noSecrets) DeleteSecret(string, string) error      { return nil }

// memSecrets is a broker-only keychain in memory.
type memSecrets map[string][]byte

func (m memSecrets) Secret(service, account string) ([]byte, error) {
	if b, ok := m[service+"/"+account]; ok {
		return append([]byte(nil), b...), nil
	}
	return nil, broker.ErrNotFound
}
func (m memSecrets) PutSecret(service, account string, data []byte) error {
	m[service+"/"+account] = append([]byte(nil), data...)
	return nil
}
func (m memSecrets) DeleteSecret(service, account string) error {
	delete(m, service+"/"+account)
	return nil
}

type appKey []byte

func (appKey) AppAccount(string) (string, error)       { return "1234567", nil }
func (k appKey) AppKey(string, string) ([]byte, error) { return append([]byte(nil), k...), nil } // jwt erases it

func TestAuditHoldsNoSecret(t *testing.T) {
	const claudeToken = "sk-ant-oat01-AUDIT-NEVER-LOG-ME"
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	pemKey := pem.EncodeToMemory(&pem.Block{Type: "RSA PRIVATE KEY", Bytes: x509.MarshalPKCS1PrivateKey(key)})
	issued := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/app/installations":
			_, _ = io.WriteString(w, `[{"id":7,"account":{"login":"acme"}}]`)
		case r.Method == http.MethodPost:
			issued++
			w.WriteHeader(http.StatusCreated)
			_, _ = fmt.Fprintf(w, `{"token":"ghs_AUDITTEST_%d","expires_at":"2099-01-01T00:00:00Z"}`, issued)
		case r.URL.Path == "/installation/repositories":
			_, _ = io.WriteString(w, `{"total_count":1,"repository_selection":"selected","repositories":[{"full_name":"acme/ensemble"}]}`)
		case r.Method == http.MethodDelete:
			w.WriteHeader(http.StatusNoContent)
		default:
			t.Errorf("unexpected %s %s", r.Method, r.URL)
		}
	}))
	defer srv.Close()
	manifest, err := os.ReadFile(filepath.Join(fixtures.Dir(), "manifest-github-app.json"))
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "m.json")
	if err := os.WriteFile(path, manifest, 0o600); err != nil {
		t.Fatal(err)
	}
	var m struct{ Name, Generation string }
	_ = json.Unmarshal(manifest, &m)
	c := "ensemble-" + m.Name + "-" + m.Generation
	log := filepath.Join(t.TempDir(), "Library/Logs/ensemble/broker.jsonl")
	keys := memSecrets{"ensemble-broker-github-users/ensemble-broker-bot": []byte(`{"slug":"ensemble-broker-bot","id":343764394}`)}
	lock := broker.FileLock(filepath.Join(t.TempDir(), "tokens.lock"))
	for _, command := range []string{"start", "maintain", "close"} {
		rec := &audit.Recorder{Command: command, Now: time.Now, Log: audit.Log{Path: log}}
		if command != "start" {
			rec.Container = c
		}
		const home = "/Users/example"
		h := launcher.Host{
			Home: home, Endpoint: "unix://" + home + "/.docker/run/docker.sock",
			IsDir:  func(string) bool { return true },
			Safe:   func(string) error { return nil },
			Docker: &scripted{},
			Creds:  broker.Credentials{Keychain: &memKeychain{token: []byte(claudeToken)}},
			GitHub: broker.GitHub{Keychain: keys, Apps: appKey(pemKey), Lock: lock, Audit: rec,
				API: github.Client{Base: srv.URL, HTTP: srv.Client(), Now: time.Now}},
			Audit: rec,
		}
		switch command {
		case "start":
			_, err = h.Start(context.Background(), path)
		case "maintain":
			_, err = h.Maintain(context.Background(), c, path, nil, true)
		case "close":
			_, err = h.Close(context.Background(), c)
		}
		rec.Flush(err)
		if err != nil {
			t.Fatalf("%s: %v", command, err)
		}
	}
	data, err := os.ReadFile(log)
	if err != nil {
		t.Fatal(err)
	}
	for _, secret := range []string{"ghs_AUDITTEST", claudeToken, "PRIVATE KEY"} {
		if bytes.Contains(data, []byte(secret)) {
			t.Fatalf("log holds %q:\n%s", secret, data)
		}
	}
	var results []string
	for _, line := range bytes.Split(bytes.TrimSpace(data), []byte("\n")) {
		var l struct {
			Event       string
			Credentials []struct{ Kind, Result string }
		}
		if err := json.Unmarshal(line, &l); err != nil {
			t.Fatal(err)
		}
		for _, cr := range l.Credentials {
			results = append(results, l.Event+" "+cr.Kind+" "+cr.Result)
		}
	}
	want := []string{"start claude-setup-token issued", "start github-installation-token issued",
		"refresh github-installation-token renewed", "refresh github-installation-token revoked", "close github-installation-token revoked"}
	if !slices.Equal(results, want) || !bytes.Contains(data, []byte(`"repositories":["acme/ensemble"]`)) {
		t.Fatalf("%v\n%s", results, data)
	}
}

// --key-file's content and --app-id reach login github; the file's bytes are erased after it returns.
func TestLoginGitHubKeyFileDispatch(t *testing.T) {
	const pemText = "-----BEGIN RSA PRIVATE KEY-----\nSENTINEL\n-----END RSA PRIVATE KEY-----\n"
	path := filepath.Join(t.TempDir(), "app.pem")
	if err := os.WriteFile(path, []byte(pemText), 0o600); err != nil {
		t.Fatal(err)
	}
	var seen *keyFile
	var held string
	a := app{loginGitHub: func(_ context.Context, _ keys, app string, key *keyFile, out io.Writer) error {
		seen, held = key, string(key.pem)
		_, err := io.WriteString(out, "ok\n")
		return err
	}}
	if code, out, errOut := invoke(a, "login", "github", "ensemble-broker-bot", "--key-file", path, "--app-id", "1234567"); code != 0 || out != "ok\n" || errOut != "" {
		t.Fatalf("exit %d %q %q", code, out, errOut)
	}
	if seen.id != "1234567" || held != pemText || !bytes.Equal(seen.pem, make([]byte, len(pemText))) {
		t.Fatalf("key %q %q, after %q", seen.id, held, seen.pem)
	}
	big := filepath.Join(t.TempDir(), "big.pem")
	if err := os.WriteFile(big, bytes.Repeat([]byte("k"), maxKeyFile+1), 0o600); err != nil {
		t.Fatal(err)
	}
	for file, want := range map[string]string{
		big:                       "ensemble-broker: " + big + ": more than 16384 bytes\n",
		filepath.Join(path, "no"): "ensemble-broker: reading the App private key failed: open " + filepath.Join(path, "no") + ": not a directory\n",
	} {
		called := false
		a := app{loginGitHub: func(context.Context, keys, string, *keyFile, io.Writer) error { called = true; return nil }}
		if code, out, errOut := invoke(a, "login", "github", "ensemble-broker-bot", "--key-file", file, "--app-id", "1234567"); code != 1 || out != "" || errOut != want || called {
			t.Fatalf("%s: exit %d %q %q called %v", file, code, out, errOut, called)
		}
	}
}

func TestLoginEnvDispatch(t *testing.T) {
	var got string
	a := app{loginEnv: func(_ keys, name string, out io.Writer) error {
		got = name
		_, err := io.WriteString(out, "ok\n")
		return err
	}}
	if code, out, errOut := invoke(a, "login", "env", "dummy"); code != 0 || got != "dummy" || out != "ok\n" || errOut != "" {
		t.Fatalf("exit %d name %q %q %q", code, got, out, errOut)
	}
	a = app{loginEnv: func(keys, string, io.Writer) error { return errors.New("the value must be 1 to 16384 bytes") }}
	if code, out, errOut := invoke(a, "login", "env", "dummy"); code != 1 || out != "" || errOut != "ensemble-broker: the value must be 1 to 16384 bytes\n" {
		t.Fatalf("%d %q %q", code, out, errOut)
	}
}

// keptReader reads from r and keeps every buffer it was handed, so a test can check the caller erased it.
type keptReader struct {
	r    io.Reader
	bufs [][]byte
}

func (k *keptReader) Read(p []byte) (int, error) {
	k.bufs = append(k.bufs, p)
	return k.r.Read(p)
}

// erased is whether every buffer k was handed, to its capacity, is zero.
func (k *keptReader) erased() bool {
	for _, b := range k.bufs {
		b = b[:cap(b)]
		if !bytes.Equal(b, make([]byte, len(b))) {
			return false
		}
	}
	return len(k.bufs) > 0
}

func TestReadEnvValue(t *testing.T) {
	for _, value := range []string{"", "one line", "-----BEGIN KEY-----\nabc\n-----END KEY-----\n", strings.Repeat("v", launcher.MaxSecretBytes)} {
		r := &keptReader{r: strings.NewReader(value)}
		got, err := readEnvValue(r)
		if err != nil || string(got) != value || len(r.bufs) == 0 || cap(r.bufs[0]) != launcher.MaxSecretBytes+1 {
			t.Fatalf("%.20q: %.20q %v, buffers %d", value, got, err, len(r.bufs))
		}
		end := func(b []byte) *byte { return &b[:cap(b)][cap(b)-1] }
		for _, b := range r.bufs {
			if end(b) != end(r.bufs[0]) {
				t.Fatalf("%.20q: read into a second buffer", value)
			}
		}
	}
	for _, n := range []int{launcher.MaxSecretBytes + 1, launcher.MaxSecretBytes + 2} {
		r := &keptReader{r: strings.NewReader(strings.Repeat("v", n))}
		if _, err := readEnvValue(r); err == nil || err.Error() != "the value must be 1 to 16384 bytes" || !r.erased() {
			t.Fatalf("%d bytes: %v, erased %v", n, err, r.erased())
		}
	}
	r := &keptReader{r: io.MultiReader(strings.NewReader("partial"), iotest.ErrReader(errors.New("closed")))}
	if _, err := readEnvValue(r); err == nil || err.Error() != "closed" || !r.erased() {
		t.Fatalf("read error: %v, erased %v", err, r.erased())
	}
}

type datedCall struct {
	kind           credential.Kind
	name, authFile string
	reg            credential.Registration
}

func datedApp(got *[]datedCall) app {
	return app{dated: func(_ keys, kind credential.Kind, name, authFile string, reg credential.Registration, out io.Writer) error {
		*got = append(*got, datedCall{kind, name, authFile, reg})
		_, err := io.WriteString(out, "ok\n")
		return err
	}}
}

func day(t *testing.T, s string) time.Time {
	t.Helper()
	d, err := credential.ParseDate(s)
	if err != nil {
		t.Fatal(err)
	}
	return d
}

func TestLoginClaudeFlagsDispatch(t *testing.T) {
	for _, c := range []struct {
		args []string
		want datedCall
	}{
		{[]string{"--fresh-token"}, datedCall{credential.Claude, "default", "", credential.Registration{Fresh: true}}},
		{[]string{"work", "--fresh-token"}, datedCall{credential.Claude, "work", "", credential.Registration{Fresh: true}}},
		{[]string{"--expires", "2027-01-02"}, datedCall{credential.Claude, "default", "", credential.Registration{Expires: day(t, "2027-01-02")}}},
		{[]string{"work", "--issued", "2026-01-02", "--expires", "2027-01-02"}, datedCall{credential.Claude, "work", "", credential.Registration{Issued: day(t, "2026-01-02"), Expires: day(t, "2027-01-02")}}},
		{[]string{"--fresh-token", "--expires", "2027-01-02"}, datedCall{credential.Claude, "default", "", credential.Registration{Fresh: true, Expires: day(t, "2027-01-02")}}},
	} {
		var got []datedCall
		code, out, errOut := invoke(datedApp(&got), append([]string{"login", "claude"}, c.args...)...)
		if code != 0 || out != "ok\n" || errOut != "" || len(got) != 1 || !reflect.DeepEqual(got[0], c.want) {
			t.Errorf("%q: exit %d %q %q %+v", c.args, code, out, errOut, got)
		}
	}
}

func TestLoginStaticFlagsDispatch(t *testing.T) {
	for _, c := range []struct {
		args []string
		want datedCall
	}{
		{[]string{"opencode", "strata", "--expires", "2027-01-02"}, datedCall{credential.Opencode, "strata", "", credential.Registration{Expires: day(t, "2027-01-02")}}},
		{[]string{"opencode", "strata", "--expires", "2027-01-02", "--auth-file", "/tmp/auth.json"}, datedCall{credential.Opencode, "strata", "/tmp/auth.json", credential.Registration{Expires: day(t, "2027-01-02")}}},
		{[]string{"opencode", "strata", "--auth-file", "/tmp/auth.json", "--expires", "2027-01-02"}, datedCall{credential.Opencode, "strata", "/tmp/auth.json", credential.Registration{Expires: day(t, "2027-01-02")}}},
		{[]string{"env", "db", "--expires", "2027-01-02"}, datedCall{credential.Env, "db", "", credential.Registration{Expires: day(t, "2027-01-02")}}},
	} {
		var got []datedCall
		code, out, errOut := invoke(datedApp(&got), append([]string{"login"}, c.args...)...)
		if code != 0 || out != "ok\n" || errOut != "" || len(got) != 1 || !reflect.DeepEqual(got[0], c.want) {
			t.Errorf("%q: exit %d %q %q %+v", c.args, code, out, errOut, got)
		}
	}
}

func TestLoginFlagsUsage(t *testing.T) {
	var got []datedCall
	a := datedApp(&got)
	for _, args := range [][]string{
		{"claude", "--expires"}, {"claude", "--bogus"}, {"claude", "--fresh-token", "x"}, {"claude", "Work", "--fresh-token"},
		{"claude", "--fresh-token", "work"}, {"opencode", "strata", "--issued", "2026-01-02"}, {"opencode", "strata", "--fresh-token"},
		{"env", "db", "--fresh-token"}, {"env", "db", "--issued", "2026-01-02"}, {"env", "db", "--expires"},
		{"opencode", "strata", "--expires", "2027-01-02", "--auth-file", "relative"}, {"env", "db", "--auth-file", "/tmp/a"},
	} {
		if code, _, _ := invoke(a, append([]string{"login"}, args...)...); code != 2 {
			t.Errorf("%q: exit %d", args, code)
		}
	}
	for _, c := range []struct {
		args []string
		err  string
	}{
		{[]string{"claude", "--expires", "2027-1-2"}, `"2027-1-2" is not a YYYY-MM-DD date`},
		{[]string{"claude", "--expires", "2027-01-02", "--expires", "2027-01-03"}, "--expires given twice"},
		{[]string{"claude", "--fresh-token", "--fresh-token"}, "--fresh-token given twice"},
		{[]string{"env", "db", "--expires", "2027-02-30"}, `"2027-02-30" is not a YYYY-MM-DD date`},
	} {
		code, out, errOut := invoke(a, append([]string{"login"}, c.args...)...)
		if code != 1 || out != "" || errOut != "ensemble-broker: "+c.err+"\n" {
			t.Errorf("%q: exit %d %q %q", c.args, code, out, errOut)
		}
	}
	if len(got) != 0 {
		t.Fatalf("dated called: %+v", got)
	}
}

// staticFixture is a login's static records under a temporary home: its store, audit log and clock.
func staticFixture(t *testing.T) (staticLogin, string) {
	t.Helper()
	home := t.TempDir()
	return staticLogin{store: staticStore(home), log: audit.Log{Path: audit.Path(home)},
		now: func() time.Time { return time.Date(2026, 10, 26, 12, 0, 0, 0, time.UTC) }}, home
}

func savedLogin(out io.Writer, line string) func() error {
	return func() error {
		_, err := io.WriteString(out, line+"\n")
		return err
	}
}

func TestStaticLoginRecordsExpiry(t *testing.T) {
	s, home := staticFixture(t)
	var out, errOut bytes.Buffer
	if err := s.register("ensemble-broker-claude/default", credential.Registration{}, &out, &errOut, savedLogin(&out, "saved")); err != nil {
		t.Fatal(err)
	}
	if out.String() != "saved\nExpiry unknown\n" || errOut.String() != "" {
		t.Fatalf("%q %q", out.String(), errOut.String())
	}
	out.Reset()
	if err := s.register("ensemble-broker-claude/default", credential.Registration{Fresh: true}, &out, &errOut, savedLogin(&out, "saved")); err != nil {
		t.Fatal(err)
	}
	if out.String() != "saved\nExpires 2027-10-26 (assumed)\n" {
		t.Fatalf("%q", out.String())
	}
	records, err := staticStore(home).Load()
	if err != nil || !reflect.DeepEqual(records["ensemble-broker-claude/default"], credential.Static{Version: 2, ExpiresAt: "2027-10-26T12:00:00Z", Provenance: credential.Assumed}) {
		t.Fatalf("%+v %v", records, err)
	}
	if _, err := os.Stat(audit.Path(home)); !os.IsNotExist(err) {
		t.Fatalf("an audit line without a notice: %v", err)
	}
}

func TestStaticLoginRejectsBeforeLogin(t *testing.T) {
	s, home := staticFixture(t)
	called := false
	var out, errOut bytes.Buffer
	err := s.register("ensemble-broker-claude/default", credential.Registration{Fresh: true, Expires: day(t, "2027-01-02")}, &out, &errOut, func() error {
		called = true
		return nil
	})
	if err == nil || err.Error() != "--fresh-token takes no date" || called || out.Len() != 0 {
		t.Fatalf("%v %v %q", err, called, out.String())
	}
	if _, err := os.Stat(staticStore(home).Path); !os.IsNotExist(err) {
		t.Fatalf("store written: %v", err)
	}
}

func TestStaticLoginPrintsDueNotice(t *testing.T) {
	s, home := staticFixture(t)
	var out, errOut bytes.Buffer
	if err := s.register("ensemble-broker-env/db", credential.Registration{Expires: day(t, "2026-10-31")}, &out, &errOut, savedLogin(&out, "saved")); err != nil {
		t.Fatal(err)
	}
	want := "saved\nExpires 2026-10-31 (explicit)\nensemble-broker-env/db expires 2026-10-31 (explicit), in 5 days: rotate it at its source, then ensemble-broker login env db [--expires <date>]; running sessions keep their copy until they restart\n"
	if out.String() != want || errOut.String() != "" {
		t.Fatalf("%q %q", out.String(), errOut.String())
	}
	records, _ := staticStore(home).Load()
	if got := records["ensemble-broker-env/db"].Notified; !reflect.DeepEqual(got, []string{"7d"}) {
		t.Fatalf("notified %q", got)
	}
	data, err := os.ReadFile(audit.Path(home))
	if err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(strings.TrimSpace(string(data)), "\n")
	if len(lines) != 1 || !strings.Contains(lines[0], `"command":"login","event":"credential-notice"`) ||
		!strings.Contains(lines[0], `"kind":"env-secret","result":"notified","expires_at":"2026-10-31T00:00:00Z","item":"ensemble-broker-env/db","notice":"7d"`) {
		t.Fatalf("%q", lines)
	}
}

func TestStaticLoginAuditWarning(t *testing.T) {
	s, home := staticFixture(t)
	if err := os.MkdirAll(audit.Path(home), 0o700); err != nil { // a directory where the log goes
		t.Fatal(err)
	}
	var out, errOut bytes.Buffer
	if err := s.register("ensemble-broker-env/db", credential.Registration{Expires: day(t, "2026-10-31")}, &out, &errOut, savedLogin(&out, "saved")); err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(errOut.String(), "ensemble-broker: audit_write_failed: ") || !strings.Contains(out.String(), "in 5 days") {
		t.Fatalf("%q %q", out.String(), errOut.String())
	}
}

func TestStaticSentinelAbsent(t *testing.T) {
	const sentinel = "SENTINEL-5ec2e7"
	s, home := staticFixture(t)
	var out, errOut bytes.Buffer
	login := func() error {
		value := []byte(sentinel) // what the Keychain login reads and stores; never handed to the static records
		clear(value)
		_, err := io.WriteString(&out, "saved\n")
		return err
	}
	if err := s.register("ensemble-broker-env/db", credential.Registration{Expires: day(t, "2026-10-31")}, &out, &errOut, login); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(s.store.Path, []byte(sentinel), 0o600); err != nil {
		t.Fatal(err)
	}
	err := s.register("ensemble-broker-env/db", credential.Registration{}, &out, &errOut, login)
	if err == nil || err.Error() != s.store.Path+": invalid content" {
		t.Fatalf("%v", err)
	}
	code, stdout, stderr := invoke(app{loginEnv: func(keys, string, io.Writer) error { return err }}, "login", "env", "db")
	logged, _ := os.ReadFile(audit.Path(home))
	for name, text := range map[string]string{"stdout": out.String() + stdout, "stderr": errOut.String() + stderr, "audit": string(logged)} {
		if strings.Contains(text, sentinel) {
			t.Errorf("%s: %q", name, text)
		}
	}
	if code != 1 {
		t.Fatalf("exit %d", code)
	}
}

func TestRefreshAndNoticesAskForRestart(t *testing.T) {
	const c = "ensemble-worker_1-0123456789abcdef0123456789abcdef"
	var calls []call
	for _, args := range [][]string{
		{"refresh", "--container", c, "--manifest", "/tmp/m.json"},
		{"notices", "--container", c, "--manifest", "/tmp/m.json", "--delivered", "ensemble-broker-env/db@2:7d"},
		{"refresh"},
	} {
		code, out, errOut := invoke(recording(&calls), args...)
		assertError(t, errOut, out, "unknown", protocol.Usage)
		var got struct{ Error protocol.Error }
		if code != 2 || json.Unmarshal([]byte(errOut), &got) != nil || got.Error.Message != replaced || len(calls) != 0 {
			t.Fatalf("%q: %d %q %v", args, code, errOut, calls)
		}
	}
	if !strings.Contains(replaced, "restart this session") {
		t.Fatal(replaced)
	}
}

func TestMaintainArgs(t *testing.T) {
	const c = "ensemble-worker_1-0123456789abcdef0123456789abcdef"
	type got struct {
		args  []string
		renew bool
	}
	var calls []got
	a := app{maintain: func(_ context.Context, _ keys, container, manifest string, delivered []string, renew bool) (any, error) {
		calls = append(calls, got{append([]string{container, manifest}, delivered...), renew})
		return launcher.MaintainResult{Container: container, Results: []launcher.ProviderResult{}, Notices: []launcher.Notice{}}, nil
	}}
	base := []string{"maintain", "--container", c, "--manifest", "/tmp/m.json"}
	for _, tc := range []struct {
		extra []string
		want  got
	}{
		{nil, got{[]string{c, "/tmp/m.json"}, false}},
		{[]string{"--renew"}, got{[]string{c, "/tmp/m.json"}, true}},
		{[]string{"--delivered", "ensemble-broker-env/db@2:7d", "--renew", "--delivered", "ensemble-broker-claude/work@1:rotated"},
			got{[]string{c, "/tmp/m.json", "ensemble-broker-env/db@2:7d", "ensemble-broker-claude/work@1:rotated"}, true}},
	} {
		calls = nil
		code, out, errOut := invoke(a, append(slices.Clone(base), tc.extra...)...)
		if code != 0 || errOut != "" || out != `{"container":"`+c+`","next_check":null,"deadline":null,"results":[],"notices":[]}`+"\n" ||
			len(calls) != 1 || !slices.Equal(calls[0].args, tc.want.args) || calls[0].renew != tc.want.renew {
			t.Fatalf("%q: %d %q %q %+v", tc.extra, code, out, errOut, calls)
		}
	}
	many := slices.Clone(base)
	for range maxDelivered + 1 {
		many = append(many, "--delivered", "ensemble-broker-env/db@2:7d")
	}
	calls = nil
	for _, args := range [][]string{
		append(slices.Clone(base), "--renew", "--renew"), append(slices.Clone(base), "--delivered"),
		append(slices.Clone(base), "--delivered", "ensemble-broker-bot/x@1:7d"), append(slices.Clone(base), "extra"),
		{"maintain", "--container", "bad", "--manifest", "/tmp/m.json"}, {"maintain", "--container", c, "--manifest", "relative"},
		{"maintain", "--manifest", "/tmp/m.json", "--container", c}, many,
	} {
		code, out, errOut := invoke(a, args...)
		if code != 2 || out != "" || calls != nil {
			t.Fatalf("%q: %d %+v", args, code, calls)
		}
		assertError(t, errOut, out, "maintain", protocol.Usage)
	}
}

func TestPipedSecretReaders(t *testing.T) {
	for _, c := range []struct{ in, token, value string }{
		{"tok\n", "tok", "tok\n"}, {"tok\r\n", "tok", "tok\r\n"}, {"tok", "tok", "tok"}, {"a\nb\n", "a\nb", "a\nb\n"},
	} {
		for _, token := range []bool{true, false} {
			r, w, err := os.Pipe()
			if err != nil {
				t.Fatal(err)
			}
			go func() { _, _ = io.WriteString(w, c.in); w.Close() }()
			read, _ := stdinReaders(r, "unused prompt", token)
			got, err := read()
			r.Close()
			want := c.value
			if token {
				want = c.token
			}
			if err != nil || string(got) != want {
				t.Errorf("%q token=%v: %q %v", c.in, token, got, err)
			}
		}
	}
}

func TestStaticLoginPipedToken(t *testing.T) {
	// login claude with the token piped and no terminal: --fresh-token records assumed, its absence unknown, and
	// nothing asks for confirmation.
	for _, c := range []struct {
		reg  credential.Registration
		want credential.Provenance
	}{{credential.Registration{Fresh: true}, credential.Assumed}, {credential.Registration{}, credential.Unknown}} {
		s, home := staticFixture(t)
		r, w, err := os.Pipe()
		if err != nil {
			t.Fatal(err)
		}
		go func() { _, _ = io.WriteString(w, "sk-ant-oat01-piped\n"); w.Close() }()
		read, _ := stdinReaders(r, "Claude account default setup-token (hidden): ", true)
		var out, errOut bytes.Buffer
		err = s.register("ensemble-broker-claude/default", c.reg, &out, &errOut, func() error {
			token, err := read()
			if err == nil && string(token) != "sk-ant-oat01-piped" {
				err = fmt.Errorf("token %q", token)
			}
			return err
		})
		r.Close()
		records, _ := staticStore(home).Load()
		if err != nil || records["ensemble-broker-claude/default"].Provenance != c.want || errOut.Len() != 0 || strings.Contains(out.String(), "piped") {
			t.Fatalf("%v: %v %+v %q %q", c.want, err, records, out.String(), errOut.String())
		}
	}
}

func TestRetrustCommand(t *testing.T) {
	var got []keys
	a := app{retrust: func(k keys, out io.Writer) error {
		got = append(got, k)
		_, err := fmt.Fprintln(out, "re-trusted 1 broker keychain item(s)")
		return err
	}}
	if code, out, errOut := invoke(a, "retrust"); code != 0 || out != "re-trusted 1 broker keychain item(s)\n" || errOut != "" || len(got) != 1 {
		t.Fatalf("%d %q %q %v", code, out, errOut, got)
	}
	for _, args := range [][]string{{"retrust", "x"}, {"retrust", "x", "y"}} {
		if code, _, errOut := invoke(a, args...); code != 2 || !strings.Contains(errOut, "| retrust") {
			t.Fatalf("%q: %d %q", args, code, errOut)
		}
	}
	if len(got) != 1 {
		t.Fatalf("retrust ran with arguments: %v", got)
	}
	a.retrust = func(keys, io.Writer) error { return errors.New("1 broker keychain item(s) not re-trusted; rerun x") }
	if code, out, errOut := invoke(a, "retrust"); code != 1 || out != "" || errOut != "ensemble-broker: 1 broker keychain item(s) not re-trusted; rerun x\n" {
		t.Fatalf("%d %q %q", code, out, errOut)
	}
	a.keychain = func(bool) (keys, error) { return keys{}, errors.New("cannot resolve system account home") }
	if code, _, errOut := invoke(a, "retrust"); code != 1 || errOut != "ensemble-broker: cannot resolve system account home\n" {
		t.Fatalf("%d %q", code, errOut)
	}
}

// Only a login or retrust gets an interactive keychain, whose dialogs and writes may ask; every other command's
// keychain fails rather than show a dialog.
func TestOnlyLoginsAndRetrustAreInteractive(t *testing.T) {
	var calls []call
	a := recording(&calls)
	ok := func(keys, io.Writer) error { return nil }
	a.retrust = ok
	a.login = func(keys, string, io.Writer) error { return nil }
	a.loginGitHub = func(context.Context, keys, string, *keyFile, io.Writer) error { return nil }
	a.loginCodex = func(context.Context, keys, io.Writer) error { return nil }
	a.loginOpencode = func(keys, string, string, io.Writer) error { return nil }
	a.loginEnv = func(keys, string, io.Writer) error { return nil }
	a.dated = func(keys, credential.Kind, string, string, credential.Registration, io.Writer) error { return nil }
	a.linear = func(context.Context, keys, bool) (any, error) { return map[string]bool{"ok": true}, nil }
	a.installCodex = func(context.Context, string, bool) (any, error) { return map[string]bool{"ok": true}, nil }
	container := "ensemble-worker_1-0123456789abcdef0123456789abcdef"
	for _, tc := range []struct {
		args        []string
		interactive []bool
	}{
		{[]string{"retrust"}, []bool{true}},
		{[]string{"login", "claude"}, []bool{true}},
		{[]string{"login", "claude", "work"}, []bool{true}},
		{[]string{"login", "claude", "--expires", "2027-01-01"}, []bool{true}},
		{[]string{"login", "github", broker.AppKeyService}, []bool{true}},
		{[]string{"login", "codex"}, []bool{true}},
		{[]string{"login", "opencode", "strata"}, []bool{true}},
		{[]string{"login", "env", "db"}, []bool{true}},
		{[]string{"login", "linear"}, []bool{true}},
		{[]string{"linear-maintain"}, []bool{false}},
		{[]string{"start", "--manifest", "/m.json"}, []bool{false}},
		{[]string{"maintain", "--container", container, "--manifest", "/m.json"}, []bool{false}},
		{[]string{"close", "--container", container}, []bool{false}},
		{[]string{"status", "--cleanup"}, []bool{false}},
		{[]string{"build", "--ensemble", "/e"}, []bool{false}},
		{[]string{"lock-query", "--lock", "dummy"}, []bool{false}},
		{[]string{"probe-provider", "--provider", "strata", "--base-url", "https://strata.test/v1"}, []bool{false}},
		{[]string{"workspace-rm", "--volume", "ensemble-ws-w"}, []bool{false}},
		{[]string{"check-clients"}, []bool{false}},
		{[]string{"version"}, nil},
		{[]string{"install-codex", "0.1.0"}, nil},
	} {
		var asked []bool
		a.keychain = func(interactive bool) (keys, error) {
			asked = append(asked, interactive)
			return keys{Home: "/nonexistent"}, nil
		}
		if code, _, errOut := invoke(a, tc.args...); code != 0 || !slices.Equal(asked, tc.interactive) {
			t.Errorf("%q: exit %d %q, keychain asked %v, want %v", tc.args, code, errOut, asked, tc.interactive)
		}
	}
}
