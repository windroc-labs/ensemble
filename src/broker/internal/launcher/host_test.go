package launcher

import (
	"bytes"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"ensemble/broker/internal/docker"
)

const localEndpoint = "unix://" + home + "/.docker/run/docker.sock"

// one is the single configured daemon, the local Docker Desktop, run by r.
func one(r docker.Runner) []Daemon {
	return []Daemon{{Name: DaemonLocal, Endpoint: localEndpoint, Runner: r}}
}

// withRunner is h with its default daemon run by r, h's own list untouched.
func withRunner(h Host, r docker.Runner) Host {
	if len(h.Daemons) == 0 {
		h.Daemons = one(r)
		return h
	}
	h.Daemons = slices.Clone(h.Daemons)
	h.Daemons[0].Runner = r
	return h
}

// atEndpoint is h with its default daemon at endpoint, remote when it is an ssh:// one.
func atEndpoint(h Host, endpoint string) Host {
	h.Daemons = slices.Clone(h.Daemons)
	h.Daemons[0].Endpoint, h.Daemons[0].Remote = endpoint, strings.HasPrefix(endpoint, "ssh://")
	return h
}

// onTwo is h with a remote target run by remote, its default, beside the local Docker Desktop run by local, placing
// a worker given miniPassword on local.
func onTwo(h Host, remote, local docker.Runner) Host {
	h.Daemons = []Daemon{
		{Name: DaemonRemote, Endpoint: remoteEndpoint, Remote: true, Runner: remote},
		{Name: DaemonLocal, Endpoint: localEndpoint, Runner: local},
	}
	h.Place = Placement(h.Daemons, []string{miniPassword})
	return h
}

func writeLog(t *testing.T, path string, size int) {
	t.Helper()
	if err := os.WriteFile(path, bytes.Repeat([]byte("a"), size), 0o600); err != nil {
		t.Fatal(err)
	}
}

func readLog(t *testing.T, path string) string {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func TestOpenCheckLogRotatesPastTheLimit(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "clients-check.log")
	writeLog(t, path, checkLogMax+1)
	running, err := os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer running.Close()
	if _, err := running.WriteString("before"); err != nil {
		t.Fatal(err)
	}
	log, err := openCheckLog(dir, os.Rename)
	if err != nil {
		t.Fatal(err)
	}
	defer log.Close()
	if _, err := running.WriteString(" after"); err != nil {
		t.Fatal(err)
	}
	if got := readLog(t, path+".1"); len(got) != checkLogMax+1+len("before after") || got[checkLogMax+1:] != "before after" {
		t.Fatalf(".1 holds %d bytes ending %q", len(got), got[len(got)-12:])
	}
	if got := readLog(t, path); got != "" {
		t.Fatalf("new log %q", got)
	}
	if _, err := log.WriteString("next"); err != nil || readLog(t, path) != "next" {
		t.Fatalf("write %v; log %q", err, readLog(t, path))
	}
}

func TestOpenCheckLogKeepsASmallLog(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "clients-check.log")
	writeLog(t, path, checkLogMax)
	writeLog(t, path+".1", 3)
	log, err := openCheckLog(dir, os.Rename)
	if err != nil {
		t.Fatal(err)
	}
	defer log.Close()
	if _, err := log.WriteString("x"); err != nil {
		t.Fatal(err)
	}
	if got := readLog(t, path); len(got) != checkLogMax+1 || got[checkLogMax:] != "x" {
		t.Fatalf("log holds %d bytes", len(got))
	}
	if got := readLog(t, path+".1"); got != "aaa" {
		t.Fatalf(".1 %q", got)
	}
}

func TestOpenCheckLogCreatesAMissingLog(t *testing.T) {
	dir := t.TempDir()
	log, err := openCheckLog(dir, os.Rename)
	if err != nil {
		t.Fatal(err)
	}
	defer log.Close()
	info, err := os.Stat(filepath.Join(dir, "clients-check.log"))
	if err != nil || info.Mode().Perm() != 0o600 || info.Size() != 0 {
		t.Fatalf("%v %v", info, err)
	}
	if _, err := os.Stat(filepath.Join(dir, "clients-check.log.1")); !os.IsNotExist(err) {
		t.Fatalf(".1: %v", err)
	}
}

func TestOpenCheckLogReportsARenameFailure(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "clients-check.log")
	writeLog(t, path, checkLogMax+1)
	if err := os.MkdirAll(filepath.Join(path+".1", "kept"), 0o700); err != nil {
		t.Fatal(err)
	}
	if log, err := openCheckLog(dir, os.Rename); err == nil {
		log.Close()
		t.Fatal("opened the log past a failed rotation")
	}
	if info, err := os.Stat(path); err != nil || info.Size() != checkLogMax+1 {
		t.Fatalf("log %v %v", info, err)
	}
}

func TestOpenCheckLogToleratesAConcurrentRotation(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "clients-check.log")
	writeLog(t, path, checkLogMax+1)
	// Another spawn moves the log between this one's Stat and Rename.
	log, err := openCheckLog(dir, func(from, to string) error {
		if err := os.Rename(from, to); err != nil {
			t.Fatal(err)
		}
		return os.Rename(from, to)
	})
	if err != nil {
		t.Fatal(err)
	}
	defer log.Close()
	if got := readLog(t, path+".1"); len(got) != checkLogMax+1 {
		t.Fatalf(".1 holds %d bytes", len(got))
	}
	if info, err := os.Stat(path); err != nil || info.Size() != 0 || info.Mode().Perm() != 0o600 {
		t.Fatalf("log %v %v", info, err)
	}
}
