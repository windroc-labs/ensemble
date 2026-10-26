package launcher

import (
	"bytes"
	"os"
	"path/filepath"
	"testing"
)

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
	log, err := openCheckLog(dir)
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
	log, err := openCheckLog(dir)
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
	log, err := openCheckLog(dir)
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
	if log, err := openCheckLog(dir); err == nil {
		log.Close()
		t.Fatal("opened the log past a failed rotation")
	}
	if info, err := os.Stat(path); err != nil || info.Size() != checkLogMax+1 {
		t.Fatalf("log %v %v", info, err)
	}
}
