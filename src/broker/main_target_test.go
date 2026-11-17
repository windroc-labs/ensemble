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
