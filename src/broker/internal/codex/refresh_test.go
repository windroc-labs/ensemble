package codex

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"
)

type refreshFixture struct {
	*testing.T
	runtime Runtime
	home    string
	process *fakeRefreshProcess
	starts  int
}

type fakeRefreshProcess struct {
	mu           sync.Mutex
	requests     []map[string]any
	output       bytes.Buffer
	respond      func(map[string]any) string
	inputClosed  bool
	outputClosed bool
	waitErr      error
	waitDone     chan struct{}
	once         sync.Once
	outputOnce   sync.Once
	signals      []syscall.Signal
	hang         bool
	block        chan struct{}
	readStarted  chan struct{}
}

type fakeRefreshInput struct{ p *fakeRefreshProcess }
type fakeRefreshOutput struct{ p *fakeRefreshProcess }

func (p *fakeRefreshProcess) Input() io.WriteCloser { return fakeRefreshInput{p} }
func (p *fakeRefreshProcess) Output() io.ReadCloser { return fakeRefreshOutput{p} }
func (p *fakeRefreshProcess) Wait() error           { <-p.waitDone; return p.waitErr }
func (p *fakeRefreshProcess) Signal(s syscall.Signal) error {
	p.mu.Lock()
	p.signals = append(p.signals, s)
	p.mu.Unlock()
	if s == syscall.SIGKILL {
		p.once.Do(func() { close(p.waitDone) })
	}
	return nil
}
func (w fakeRefreshInput) Write(b []byte) (int, error) {
	var request map[string]any
	if err := json.Unmarshal(bytes.TrimSpace(b), &request); err != nil {
		return 0, err
	}
	w.p.mu.Lock()
	defer w.p.mu.Unlock()
	w.p.requests = append(w.p.requests, request)
	response := w.p.respond(request)
	_, _ = w.p.output.WriteString(response)
	return len(b), nil
}
func (w fakeRefreshInput) Close() error {
	w.p.mu.Lock()
	w.p.inputClosed = true
	hang := w.p.hang
	w.p.mu.Unlock()
	if !hang {
		w.p.once.Do(func() { close(w.p.waitDone) })
	}
	return nil
}
func (o fakeRefreshOutput) Read(b []byte) (int, error) {
	if o.p.block != nil {
		select {
		case o.p.readStarted <- struct{}{}:
		default:
		}
		<-o.p.block
		return 0, io.EOF
	}
	o.p.mu.Lock()
	defer o.p.mu.Unlock()
	return o.p.output.Read(b)
}
func (o fakeRefreshOutput) Close() error {
	o.p.mu.Lock()
	o.p.outputClosed = true
	o.p.mu.Unlock()
	if o.p.block != nil {
		o.p.outputOnce.Do(func() { close(o.p.block) })
	}
	return nil
}

func newRefreshFixture(t *testing.T) *refreshFixture {
	t.Helper()
	root := t.TempDir()
	home := t.TempDir()
	if err := os.Chmod(home, 0700); err != nil {
		t.Fatal(err)
	}
	native := filepath.Join(root, "releases", "fixed", "codex")
	if err := os.MkdirAll(filepath.Dir(native), 0755); err != nil {
		t.Fatal(err)
	}
	content := []byte("synthetic native")
	if err := os.WriteFile(native, content, 0755); err != nil {
		t.Fatal(err)
	}
	hash := sha256.Sum256(content)
	metadata, _ := json.Marshal(Installation{Version: "0.163.0", Path: "releases/fixed/codex", SHA256: hex.EncodeToString(hash[:])})
	if err := os.WriteFile(filepath.Join(root, "active.json"), metadata, 0644); err != nil {
		t.Fatal(err)
	}
	p := &fakeRefreshProcess{waitDone: make(chan struct{})}
	p.respond = func(request map[string]any) string {
		switch request["method"] {
		case "initialize":
			return "{\"id\":1,\"result\":{}}\n"
		case "account/read":
			id := request["id"]
			response, _ := json.Marshal(map[string]any{"id": id, "result": map[string]any{"account": map[string]any{"type": "chatgpt"}, "requiresOpenaiAuth": true}})
			return string(response) + "\n"
		}
		return ""
	}
	r := New()
	r.root = root
	r.trustRoot = root
	r.owned = func(os.FileInfo) bool { return true }
	f := &refreshFixture{T: t, runtime: r, home: home, process: p}
	f.writeAuth("before")
	f.runtime.startRefresh = func(ctx context.Context, c command) (refreshProcess, error) {
		f.starts++
		if c.path != native || c.dir != home || !reflect.DeepEqual(c.env, environment(home)) || !reflect.DeepEqual(c.args, []string{"-c", `cli_auth_credentials_store="file"`, "app-server", "--listen", "stdio://"}) {
			t.Errorf("unsafe native command: %+v", c)
		}
		deadline, ok := ctx.Deadline()
		if !ok || time.Until(deadline) > 2*time.Minute {
			t.Error("native refresh deadline missing")
		}
		return p, nil
	}
	return f
}
func (f *refreshFixture) writeAuth(access string) {
	f.Helper()
	data, _ := json.Marshal(map[string]any{"tokens": map[string]any{"access_token": access}})
	if err := os.WriteFile(filepath.Join(f.home, "auth.json"), data, 0600); err != nil {
		f.Fatal(err)
	}
}
func (f *refreshFixture) refresh() error { return f.runtime.Refresh(context.Background(), f.home) }
func (f *refreshFixture) checkCleanup() {
	f.Helper()
	if f.starts != 1 || !f.process.inputClosed || !f.process.outputClosed {
		f.Fatalf("cleanup/start: %d %v %v", f.starts, f.process.inputClosed, f.process.outputClosed)
	}
}

func TestRefreshSequentialExchange(t *testing.T) {
	f := newRefreshFixture(t)
	t.Setenv("CODEX_HOME", "/personal")
	t.Setenv("OPENAI_API_KEY", "secret")
	t.Setenv("HTTPS_PROXY", "https://secret")
	if err := f.refresh(); err != nil {
		t.Fatal(err)
	}
	f.checkCleanup()
	methods := []string{}
	for _, request := range f.process.requests {
		methods = append(methods, request["method"].(string))
	}
	if !reflect.DeepEqual(methods, []string{"initialize", "initialized", "account/read", "account/read"}) {
		t.Fatalf("exchange: %v", methods)
	}
	for n, force := range []bool{false, true} {
		params := f.process.requests[n+2]["params"].(map[string]any)
		if params["refreshToken"] != force {
			t.Fatalf("refresh %d: %v", n, params)
		}
	}
}

func TestRefreshReusesStartupRotation(t *testing.T) {
	f := newRefreshFixture(t)
	respond := f.process.respond
	f.process.respond = func(request map[string]any) string {
		if request["method"] == "initialize" {
			f.writeAuth("startup-rotated")
		}
		if request["method"] == "account/read" && request["params"].(map[string]any)["refreshToken"] == true {
			t.Error("duplicate rotation")
		}
		return respond(request)
	}
	if err := f.refresh(); err != nil {
		t.Fatal(err)
	}
	f.checkCleanup()
	if len(f.process.requests) != 3 {
		t.Fatalf("requests: %d", len(f.process.requests))
	}
}

func TestRefreshRejectsProtocol(t *testing.T) {
	for name, response := range map[string]string{
		"eof": "", "invalid": "secret\n", "error": "{\"id\":1,\"error\":{\"message\":\"secret\"}}\n", "wrong_id": "{\"id\":9,\"result\":{}}\n", "string_id": "{\"id\":\"1\",\"result\":{}}\n", "null": "null\n", "result_array": "{\"id\":1,\"result\":[]}\n", "request": "{\"id\":8,\"method\":\"secret\"}\n", "trailing": "{\"id\":1,\"result\":{}}secret\n", "oversize": strings.Repeat("x", 64*1024+1) + "\n", "notifications": strings.Repeat("{\"method\":\"notice\"}\n", 1025),
	} {
		t.Run(name, func(t *testing.T) {
			f := newRefreshFixture(t)
			f.process.respond = func(map[string]any) string { return response }
			err := f.refresh()
			if err == nil || strings.Contains(err.Error(), "secret") {
				t.Fatalf("unsafe protocol result: %v", err)
			}
			f.checkCleanup()
		})
	}
}

func TestRefreshValidatesManagedAccount(t *testing.T) {
	for name, result := range map[string]string{
		"missing": "{}", "no_account": "{\"account\":null,\"requiresOpenaiAuth\":true}", "api_key": "{\"account\":{\"type\":\"apiKey\"},\"requiresOpenaiAuth\":true}", "provider": "{\"account\":{\"type\":\"chatgpt\"},\"requiresOpenaiAuth\":false}",
	} {
		t.Run(name, func(t *testing.T) {
			f := newRefreshFixture(t)
			respond := f.process.respond
			f.process.respond = func(request map[string]any) string {
				if request["method"] == "account/read" {
					return `{"id":2,"result":` + result + "}\n"
				}
				return respond(request)
			}
			if err := f.refresh(); err == nil {
				t.Fatal("unusable account accepted")
			}
			f.checkCleanup()
		})
	}
}

func TestRefreshNotifications(t *testing.T) {
	f := newRefreshFixture(t)
	respond := f.process.respond
	f.process.respond = func(request map[string]any) string {
		response := respond(request)
		if response != "" {
			return "{\"method\":\"notice\",\"params\":{\"secret\":true}}\n" + response
		}
		return response
	}
	if err := f.refresh(); err != nil {
		t.Fatal(err)
	}
	f.checkCleanup()
}

func TestRefreshCancellation(t *testing.T) {
	f := newRefreshFixture(t)
	f.process.block = make(chan struct{})
	f.process.readStarted = make(chan struct{}, 1)
	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		select {
		case <-f.process.readStarted:
			cancel()
		case <-time.After(time.Second):
			cancel()
		}
	}()
	defer cancel()
	if err := f.runtime.Refresh(ctx, f.home); !errors.Is(err, context.Canceled) {
		t.Fatalf("cancellation: %v", err)
	}
	f.checkCleanup()
}

func TestRefreshValidation(t *testing.T) {
	for _, name := range []string{"version", "home", "hash", "missing_auth", "symlink_auth", "large_auth"} {
		t.Run(name, func(t *testing.T) {
			f := newRefreshFixture(t)
			want := ""
			switch name {
			case "version":
				path := filepath.Join(f.runtime.root, "active.json")
				data, err := os.ReadFile(path)
				if err != nil {
					t.Fatal(err)
				}
				if err := os.WriteFile(path, bytes.ReplaceAll(data, []byte("0.163.0"), []byte("0.162.1")), 0644); err != nil {
					t.Fatal(err)
				}
				want = "requires pinned 0.163.0"
			case "home":
				if err := os.Chmod(f.home, 0755); err != nil {
					t.Fatal(err)
				}
				want = "private temporary home"
			case "hash":
				if err := os.WriteFile(filepath.Join(f.runtime.root, "releases/fixed/codex"), []byte("synthetic corrupted installation"), 0755); err != nil {
					t.Fatal(err)
				}
				want = "hash mismatch"
			case "missing_auth":
				if err := os.Remove(filepath.Join(f.home, "auth.json")); err != nil {
					t.Fatal(err)
				}
				want = "private auth"
			case "symlink_auth":
				if err := os.Rename(filepath.Join(f.home, "auth.json"), filepath.Join(f.home, "target")); err != nil {
					t.Fatal(err)
				}
				if err := os.Symlink(filepath.Join(f.home, "target"), filepath.Join(f.home, "auth.json")); err != nil {
					t.Fatal(err)
				}
				want = "private auth"
			case "large_auth":
				if err := os.WriteFile(filepath.Join(f.home, "auth.json"), bytes.Repeat([]byte("x"), 1024*1024+1), 0600); err != nil {
					t.Fatal(err)
				}
				want = "private auth"
			}
			if err := f.refresh(); err == nil || !strings.Contains(err.Error(), want) {
				t.Fatalf("%s: %v", want, err)
			}
			if f.starts != 0 {
				t.Fatal("started invalid refresh")
			}
		})
	}
}

func TestRefreshSanitizesStartAndExitErrors(t *testing.T) {
	for _, stage := range []string{"start", "exit"} {
		t.Run(stage, func(t *testing.T) {
			f := newRefreshFixture(t)
			if stage == "start" {
				f.runtime.startRefresh = func(context.Context, command) (refreshProcess, error) { return nil, errors.New("secret") }
			} else {
				f.process.waitErr = errors.New("secret")
			}
			if err := f.refresh(); err == nil || strings.Contains(err.Error(), "secret") {
				t.Fatalf("unsafe error: %v", err)
			}
			if stage == "exit" {
				f.checkCleanup()
			}
		})
	}
}

func TestRefreshEnforcesTotalOutput(t *testing.T) {
	f := newRefreshFixture(t)
	respond := f.process.respond
	f.process.respond = func(request map[string]any) string {
		if request["method"] == "initialize" {
			notice := `{"method":"notice","params":"` + strings.Repeat("x", 60000) + `"}` + "\n"
			return strings.Repeat(notice, 18) + respond(request)
		}
		return respond(request)
	}
	if err := f.refresh(); err == nil {
		t.Fatal("unbounded output accepted")
	}
	f.checkCleanup()
}

func TestRefreshCleansProcessGroup(t *testing.T) {
	f := newRefreshFixture(t)
	f.process.hang = true
	if err := f.refresh(); err != nil {
		t.Fatal(err)
	}
	f.checkCleanup()
	if !reflect.DeepEqual(f.process.signals, []syscall.Signal{syscall.SIGTERM, syscall.SIGKILL}) {
		t.Fatalf("group cleanup: %v", f.process.signals)
	}
}

func TestRefreshBoundsStderr(t *testing.T) {
	sink := &refreshStderr{}
	if n, err := sink.Write(bytes.Repeat([]byte("s"), refreshOutputLimit)); n != refreshOutputLimit || err != nil {
		t.Fatalf("bounded stderr: %d %v", n, err)
	}
	if _, err := sink.Write([]byte("secret")); err == nil || strings.Contains(err.Error(), "secret") {
		t.Fatalf("stderr limit: %v", err)
	}
}

func TestRefreshRejectsBoundaryOverflow(t *testing.T) {
	f := newRefreshFixture(t)
	respond := f.process.respond
	f.process.respond = func(request map[string]any) string {
		response := respond(request)
		if request["method"] == "account/read" {
			f.writeAuth("startup-rotated")
			notice := `{"method":"notice","params":"` + strings.Repeat("x", 60000) + `"}` + "\n"
			data := strings.Repeat(notice, 17)
			remaining := refreshOutputLimit + 1 - len(data) - len(response) - len(respond(map[string]any{"method": "initialize"}))
			notice = `{"method":"notice","params":"` + strings.Repeat("x", remaining-len(`{"method":"notice","params":""}`)-1) + `"}` + "\n"
			return data + notice + response
		}
		return response
	}
	if err := f.refresh(); err == nil {
		t.Fatal("one-byte overflow accepted")
	}
	f.checkCleanup()
}

func TestRefreshRejectsUnterminatedResponse(t *testing.T) {
	f := newRefreshFixture(t)
	respond := f.process.respond
	f.process.respond = func(request map[string]any) string {
		response := respond(request)
		if request["method"] == "account/read" {
			f.writeAuth("startup-rotated")
			return strings.TrimSuffix(response, "\n")
		}
		return response
	}
	if err := f.refresh(); err == nil {
		t.Fatal("unterminated response accepted")
	}
	f.checkCleanup()
}

func TestRefreshMinimumVersion(t *testing.T) {
	for _, version := range []string{"0.162.999", "0.163.0", "0.164.0"} {
		t.Run(version, func(t *testing.T) {
			f := newRefreshFixture(t)
			active := filepath.Join(f.runtime.root, "active.json")
			data, err := os.ReadFile(active)
			if err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(active, bytes.ReplaceAll(data, []byte("0.163.0"), []byte(version)), 0644); err != nil {
				t.Fatal(err)
			}
			err = f.refresh()
			if version == "0.162.999" {
				if !errors.Is(err, ErrRefreshVersion) || f.starts != 0 {
					t.Fatalf("unsupported refresh: starts=%d error=%v", f.starts, err)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			f.checkCleanup()
		})
	}
}
