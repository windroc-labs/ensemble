package codex

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
	"time"
)

const refreshOutputLimit = 1024 * 1024
const refreshLineLimit = 64 * 1024

var ErrRefreshVersion = errors.New("Codex refresh requires pinned " + MinVersion + " or newer")

var errRefreshExchange = errors.New("pinned Codex refresh exchange failed")

// stopRefresh waits: for a clean exit after stdin closes, then between SIGTERM and SIGKILL.
var (
	refreshExitGrace = 2 * time.Second
	refreshTermGrace = 250 * time.Millisecond
)

type refreshProcess interface {
	Input() io.WriteCloser
	Output() io.ReadCloser
	Wait() error
	Signal(syscall.Signal) error
}

type nativeRefreshProcess struct {
	cmd    *exec.Cmd
	input  io.WriteCloser
	output io.ReadCloser
}

func (p *nativeRefreshProcess) Input() io.WriteCloser { return p.input }
func (p *nativeRefreshProcess) Output() io.ReadCloser { return p.output }
func (p *nativeRefreshProcess) Wait() error           { return p.cmd.Wait() }
func (p *nativeRefreshProcess) Signal(signal syscall.Signal) error {
	return syscall.Kill(-p.cmd.Process.Pid, signal)
}

type refreshStderr struct{ size int }

func (s *refreshStderr) Write(p []byte) (int, error) {
	s.size += len(p)
	if s.size > refreshOutputLimit {
		return 0, errRefreshExchange
	}
	return len(p), nil
}

func startRefresh(ctx context.Context, c command) (refreshProcess, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	cmd := exec.Command(c.path, c.args...)
	cmd.Dir, cmd.Env = c.dir, c.env
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.WaitDelay = 2 * time.Second
	cmd.Stderr = &refreshStderr{}
	input, err := cmd.StdinPipe()
	if err != nil {
		return nil, errRefreshExchange
	}
	output, err := cmd.StdoutPipe()
	if err != nil {
		_ = input.Close()
		return nil, errRefreshExchange
	}
	if err := cmd.Start(); err != nil {
		_ = input.Close()
		_ = output.Close()
		return nil, errRefreshExchange
	}
	return &nativeRefreshProcess{cmd: cmd, input: input, output: output}, nil
}

func refreshAuthSnapshot(home string) (string, error) {
	fd, err := syscall.Open(filepath.Join(home, "auth.json"), syscall.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_CLOEXEC|syscall.O_NONBLOCK, 0)
	if err != nil {
		return "", errors.New("cannot read Codex private auth")
	}
	file := os.NewFile(uintptr(fd), "auth.json")
	defer file.Close()
	info, err := file.Stat()
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm() != 0600 {
		return "", errors.New("invalid Codex private auth")
	}
	owner, ok := info.Sys().(*syscall.Stat_t)
	if !ok || owner.Uid != uint32(os.Geteuid()) {
		return "", errors.New("invalid Codex private auth owner")
	}
	data, err := io.ReadAll(io.LimitReader(file, refreshOutputLimit+1))
	if err != nil || len(data) > refreshOutputLimit {
		return "", errors.New("invalid Codex private auth")
	}
	var auth struct {
		Tokens struct {
			Access string `json:"access_token"`
		} `json:"tokens"`
	}
	if err := json.Unmarshal(data, &auth); err != nil || auth.Tokens.Access == "" {
		return "", errors.New("invalid Codex private auth")
	}
	return auth.Tokens.Access, nil
}

func (r Runtime) Refresh(ctx context.Context, home string) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	installation, err := r.Verify()
	if err != nil {
		return err
	}
	if !SupportedVersion(installation.Version) {
		return ErrRefreshVersion
	}
	if err := privateHome(home); err != nil {
		return err
	}
	before, err := refreshAuthSnapshot(home)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(ctx, 2*time.Minute)
	defer cancel()
	process, err := r.startRefresh(ctx, command{path: installation.Path, dir: home, args: []string{"-c", `cli_auth_credentials_store="file"`, "app-server", "--listen", "stdio://"}, env: environment(home)})
	if err != nil {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		return errRefreshExchange
	}
	exchanged := make(chan error, 1)
	exchangeDone := make(chan struct{})
	go func() { defer close(exchangeDone); exchanged <- refreshExchange(process, home, before) }()
	select {
	case err = <-exchanged:
	case <-ctx.Done():
		err = ctx.Err()
	}
	finished := make(chan error, 1)
	go func() { finished <- process.Wait() }()
	cleanupErr := stopRefresh(process, finished, ctx.Err() != nil)
	select {
	case <-exchangeDone:
	case <-time.After(2 * time.Second):
		return errors.New("pinned Codex refresh cleanup failed")
	}
	if err != nil {
		return err
	}
	if ctx.Err() != nil {
		return ctx.Err()
	}
	return cleanupErr
}

func stopRefresh(process refreshProcess, finished <-chan error, cancelled bool) error {
	_ = process.Input().Close()
	defer process.Output().Close()
	if cancelled {
		_ = process.Output().Close()
	}
	grace := refreshExitGrace
	if cancelled {
		grace = 0
	}
	timer := time.NewTimer(grace)
	defer timer.Stop()
	var waitErr error
	exited := false
	select {
	case waitErr = <-finished:
		exited = true
	case <-timer.C:
	}
	cleanExit := exited
	signalErr := process.Signal(syscall.SIGTERM)
	if signalErr != nil && !errors.Is(signalErr, syscall.ESRCH) {
		return errors.New("pinned Codex refresh cleanup failed")
	}
	if !(exited && errors.Is(signalErr, syscall.ESRCH)) {
		timer.Reset(refreshTermGrace)
		if exited {
			<-timer.C
		} else {
			select {
			case waitErr = <-finished:
				exited = true
				<-timer.C
			case <-timer.C:
			}
		}
		if err := process.Signal(syscall.SIGKILL); err != nil && !errors.Is(err, syscall.ESRCH) {
			return errors.New("pinned Codex refresh cleanup failed")
		}
		if !exited {
			timer.Reset(2 * time.Second)
			select {
			case waitErr = <-finished:
			case <-timer.C:
				return errors.New("pinned Codex refresh cleanup failed")
			}
		}
	}
	if cleanExit && waitErr != nil {
		return errRefreshExchange
	}
	return nil
}

type refreshExchangeReader struct {
	scanner       *bufio.Scanner
	reader        *io.LimitedReader
	notifications int
}

func (e *refreshExchangeReader) response(id int) (json.RawMessage, error) {
	for e.scanner.Scan() {
		var message map[string]json.RawMessage
		if json.Unmarshal(e.scanner.Bytes(), &message) != nil || message == nil {
			return nil, errRefreshExchange
		}
		if method, ok := message["method"]; ok {
			var name string
			_, hasID := message["id"]
			if hasID || json.Unmarshal(method, &name) != nil || name == "" {
				return nil, errRefreshExchange
			}
			e.notifications++
			if e.notifications > 1024 {
				return nil, errRefreshExchange
			}
			continue
		}
		var actual int
		if _, ok := message["error"]; ok {
			return nil, errRefreshExchange
		}
		if json.Unmarshal(message["id"], &actual) != nil || actual != id {
			return nil, errRefreshExchange
		}
		if e.reader.N <= 0 {
			return nil, errRefreshExchange
		}
		result := message["result"]
		if len(result) == 0 || bytes.TrimSpace(result)[0] != '{' {
			return nil, errRefreshExchange
		}
		return result, nil
	}
	return nil, errRefreshExchange
}

func refreshExchange(process refreshProcess, home, before string) error {
	reader := &io.LimitedReader{R: process.Output(), N: refreshOutputLimit + 1}
	scanner := bufio.NewScanner(reader)
	scanner.Buffer(make([]byte, 4096), refreshLineLimit)
	scanner.Split(func(data []byte, atEOF bool) (int, []byte, error) {
		if end := bytes.IndexByte(data, '\n'); end >= 0 {
			return end + 1, data[:end], nil
		}
		if atEOF && len(data) > 0 {
			return 0, nil, errRefreshExchange
		}
		return 0, nil, nil
	})
	exchange := refreshExchangeReader{scanner: scanner, reader: reader}
	send := func(message string) error {
		_, err := io.WriteString(process.Input(), message+"\n")
		if err != nil {
			return errRefreshExchange
		}
		return nil
	}
	if err := send(`{"id":1,"method":"initialize","params":{"clientInfo":{"name":"ensemble-broker","title":null,"version":"` + MinVersion + `"},"capabilities":{"experimentalApi":false,"requestAttestation":false,"explicitGatewayOauth":true}}}`); err != nil {
		return err
	}
	if _, err := exchange.response(1); err != nil {
		return err
	}
	if err := send(`{"method":"initialized"}`); err != nil {
		return err
	}
	if err := send(`{"id":2,"method":"account/read","params":{"refreshToken":false}}`); err != nil {
		return err
	}
	result, err := exchange.response(2)
	if err != nil {
		return err
	}
	if err := managedRefreshAccount(result); err != nil {
		return err
	}
	after, err := refreshAuthSnapshot(home)
	if err != nil {
		return err
	}
	if after != before {
		return nil
	}
	if err := send(`{"id":3,"method":"account/read","params":{"refreshToken":true}}`); err != nil {
		return err
	}
	result, err = exchange.response(3)
	if err != nil {
		return err
	}
	return managedRefreshAccount(result)
}

func managedRefreshAccount(result json.RawMessage) error {
	var account struct {
		Account struct {
			Type string `json:"type"`
		} `json:"account"`
		RequiresOpenAIAuth bool `json:"requiresOpenaiAuth"`
	}
	if json.Unmarshal(result, &account) != nil || account.Account.Type != "chatgpt" || !account.RequiresOpenAIAuth {
		return errors.New("pinned Codex refresh requires a usable ChatGPT account")
	}
	return nil
}
