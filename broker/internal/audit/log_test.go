package audit

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"syscall"
	"testing"
)

func logIn(t *testing.T) Log {
	t.Helper()
	return Log{Path: Path(t.TempDir())}
}

func mode(t *testing.T, path string) os.FileMode {
	t.Helper()
	info, err := os.Lstat(path)
	if err != nil {
		t.Fatal(err)
	}
	return info.Mode().Perm()
}

func TestPath(t *testing.T) {
	if got := Path("/Users/x"); got != "/Users/x/Library/Logs/ensemble/broker.jsonl" {
		t.Fatal(got)
	}
}

func TestAppendModes(t *testing.T) {
	l := logIn(t)
	if err := l.Append([]byte(`{"a":1}`), []byte(`{"a":2}`)); err != nil {
		t.Fatal(err)
	}
	if m := mode(t, filepath.Dir(l.Path)); m != 0o700 {
		t.Fatalf("dir mode %o", m)
	}
	if m := mode(t, l.Path); m != 0o600 {
		t.Fatalf("file mode %o", m)
	}
	data, _ := os.ReadFile(l.Path)
	if string(data) != "{\"a\":1}\n{\"a\":2}\n" {
		t.Fatalf("%q", data)
	}
}

func TestAppendNarrowsModes(t *testing.T) {
	l := logIn(t)
	dir := filepath.Dir(l.Path)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(l.Path, []byte("{}\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := l.Append([]byte("{}")); err != nil {
		t.Fatal(err)
	}
	if mode(t, dir) != 0o700 || mode(t, l.Path) != 0o600 {
		t.Fatalf("modes %o %o", mode(t, dir), mode(t, l.Path))
	}
}

func TestAppendRefusesSymlink(t *testing.T) {
	t.Run("file", func(t *testing.T) {
		l := logIn(t)
		target := filepath.Join(t.TempDir(), "elsewhere")
		if err := os.WriteFile(target, nil, 0o600); err != nil {
			t.Fatal(err)
		}
		if err := os.MkdirAll(filepath.Dir(l.Path), 0o700); err != nil {
			t.Fatal(err)
		}
		if err := os.Symlink(target, l.Path); err != nil {
			t.Fatal(err)
		}
		if l.Append([]byte("{}")) == nil {
			t.Fatal("wrote through a symlinked file")
		}
		if data, _ := os.ReadFile(target); len(data) != 0 {
			t.Fatalf("target got %q", data)
		}
	})
	t.Run("dir", func(t *testing.T) {
		l := logIn(t)
		target := t.TempDir()
		if err := os.MkdirAll(filepath.Dir(filepath.Dir(l.Path)), 0o700); err != nil {
			t.Fatal(err)
		}
		if err := os.Symlink(target, filepath.Dir(l.Path)); err != nil {
			t.Fatal(err)
		}
		if l.Append([]byte("{}")) == nil {
			t.Fatal("wrote through a symlinked directory")
		}
		if entries, _ := os.ReadDir(target); len(entries) != 0 {
			t.Fatalf("target got %v", entries)
		}
	})
}

func TestAppendRefusesNonRegular(t *testing.T) {
	l := logIn(t)
	if err := os.MkdirAll(filepath.Dir(l.Path), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := syscall.Mkfifo(l.Path, 0o600); err != nil {
		t.Fatal(err)
	}
	if l.Append([]byte("{}")) == nil {
		t.Fatal("wrote into a FIFO")
	}

	d := logIn(t)
	if err := os.MkdirAll(d.Path, 0o700); err != nil {
		t.Fatal(err)
	}
	if d.Append([]byte("{}")) == nil {
		t.Fatal("wrote into a directory")
	}
}

func TestRotation(t *testing.T) {
	l := logIn(t)
	if err := os.MkdirAll(filepath.Dir(l.Path), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(l.Path, bytes.Repeat([]byte("x"), MaxSize-10), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := l.Append([]byte(`{"n":1}`)); err != nil { // 8 bytes with LF: fits exactly at 10 MiB − 2
		t.Fatal(err)
	}
	if _, err := os.Stat(l.Path + ".1"); !os.IsNotExist(err) {
		t.Fatal("rotated before the file would pass 10 MiB")
	}
	if err := l.Append([]byte(`{"n":2}`)); err != nil {
		t.Fatal(err)
	}
	old, _ := os.ReadFile(l.Path + ".1")
	if len(old) != MaxSize-2 || !bytes.HasSuffix(old, []byte("{\"n\":1}\n")) {
		t.Fatalf("old log %d bytes", len(old))
	}
	if data, _ := os.ReadFile(l.Path); string(data) != "{\"n\":2}\n" {
		t.Fatalf("%q", data)
	}
	if m := mode(t, l.Path); m != 0o600 {
		t.Fatalf("new file mode %o", m)
	}

	if err := os.WriteFile(l.Path, bytes.Repeat([]byte("y"), MaxSize), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := l.Append([]byte(`{"n":3}`)); err != nil {
		t.Fatal(err)
	}
	entries, _ := os.ReadDir(filepath.Dir(l.Path))
	if len(entries) != 2 {
		t.Fatalf("want broker.jsonl and broker.jsonl.1, got %v", entries)
	}
	old, _ = os.ReadFile(l.Path + ".1")
	if len(old) != MaxSize || old[0] != 'y' {
		t.Fatal("the second rotation replaces the old log")
	}
	if data, _ := os.ReadFile(l.Path); string(data) != "{\"n\":3}\n" {
		t.Fatalf("%q", data)
	}
}

func TestConcurrentAppend(t *testing.T) {
	path := Path(t.TempDir())
	const writers, each = 8, 50
	var wg sync.WaitGroup
	for w := range writers {
		wg.Add(1)
		go func() {
			defer wg.Done()
			l := Log{Path: path}
			for i := range each {
				line := fmt.Sprintf(`{"w":%d,"i":%d,"pad":"%s"}`, w, i, bytes.Repeat([]byte("p"), 4000))
				if err := l.Append([]byte(line)); err != nil {
					t.Error(err)
				}
			}
		}()
	}
	wg.Wait()
	f, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	s := bufio.NewScanner(f)
	s.Buffer(nil, 1<<20)
	n := 0
	for s.Scan() {
		var v map[string]any
		if err := json.Unmarshal(s.Bytes(), &v); err != nil {
			t.Fatalf("line %d: %v", n, err)
		}
		n++
	}
	if n != writers*each {
		t.Fatalf("%d lines", n)
	}
}
