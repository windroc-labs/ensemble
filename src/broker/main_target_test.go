package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/protocol"
)

func TestTargetCommandRefusesAnInvalidSetting(t *testing.T) {
	home := t.TempDir()
	path := filepath.Join(home, docker.TargetFile)
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(`{"host": "ssh://u@pc", "local_secrets": [], "extra": 1}`), 0o600); err != nil {
		t.Fatal(err)
	}
	a := system()
	a.keychain = func(bool) (keys, error) { return keys{Home: home}, nil }
	code, out, errOut := invoke(a, "target")
	assertError(t, errOut, out, "target", protocol.DockerUnreachable)
	if code != 7 || !strings.Contains(errOut, path) || !strings.Contains(errOut, "invalid target setting") {
		t.Fatalf("%d %s", code, errOut)
	}
}

// A home without Docker Desktop: every daemon is unreachable, so commands that name one fail before any change.
func TestDaemonCommandsWithoutDockerDesktop(t *testing.T) {
	home := t.TempDir()
	a := system()
	a.keychain = func(bool) (keys, error) { return keys{Home: home}, nil }
	c := "ensemble-w-0123456789abcdef0123456789abcdef"
	for _, args := range [][]string{
		{"close", "--container", c, "--daemon", "D1"},
		{"workspace-rm", "--volume", "ensemble-ws-w", "--force", "--daemon", "D1"},
	} {
		code, out, errOut := invoke(a, args...)
		assertError(t, errOut, out, args[0], protocol.DockerUnreachable)
		if code != 7 || !strings.Contains(errOut, "Docker Desktop executable unavailable") {
			t.Fatalf("%q: %d %s", args, code, errOut)
		}
	}
	code, out, errOut := invoke(a, "target")
	if code != 0 || !strings.Contains(out, `"default":"local"`) || !strings.Contains(out, `"unreachable":"Docker Desktop executable unavailable`) {
		t.Fatalf("%d %s %s", code, out, errOut)
	}
}

func TestTargetCommandWithAnUnwritableHome(t *testing.T) {
	home := filepath.Join(t.TempDir(), "file")
	if err := os.WriteFile(home, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	a := system()
	a.keychain = func(bool) (keys, error) { return keys{Home: home}, nil }
	code, out, errOut := invoke(a, "target")
	assertError(t, errOut, out, "target", protocol.InternalError)
	if code != protocol.Exit(protocol.InternalError) {
		t.Fatalf("%d %s", code, errOut)
	}
}
