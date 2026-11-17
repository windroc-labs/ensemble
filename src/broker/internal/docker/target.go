package docker

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
)

const (
	// TargetFile, under the account home, names the one Docker target every docker call reaches; without it, the
	// local Docker Desktop.
	TargetFile = ".ensemble/docker/target.json"
	// SSHDir holds only ssh, a root-owned link to this broker: first on docker's PATH for a remote target, so the
	// docker CLI's ssh gets SSHArgs' options.
	SSHDir  = "/usr/local/libexec/ensemble/ssh"
	SSHPath = "/usr/bin/ssh"
	// TargetPattern is a remote target, as the protocol schema's endpoint admits it.
	TargetPattern = `ssh://([A-Za-z0-9._-]{1,64}@)?[A-Za-z0-9][A-Za-z0-9.-]{0,252}(:[0-9]{1,5})?`
	maxTarget     = 64 << 10
	minEngine     = 26
)

var (
	targetHost = regexp.MustCompile(`^` + TargetPattern + `$`)
	portSuffix = regexp.MustCompile(`:([0-9]+)$`)
	// engineMajor reads a server version's major number: 26.0.0-rc1 is 26.
	engineMajor = regexp.MustCompile(`^([0-9]+)\.`)
	// shown is a version safe to repeat in a message.
	shown = regexp.MustCompile(`^[0-9A-Za-z.+~_-]{1,32}$`)
)

// sshOptions come before docker's own ssh arguments: ssh keeps an option's first value. Without the user's ssh
// config, a connection uses keys only, never forwards, and needs a known, matching host key.
var sshOptions = []string{
	"-F", "/dev/null",
	"-o", "BatchMode=yes",
	"-o", "StrictHostKeyChecking=yes",
	"-o", "UpdateHostKeys=no",
	"-o", "ForwardAgent=no",
	"-o", "ForwardX11=no",
	"-o", "ForwardX11Trusted=no",
	"-o", "ClearAllForwardings=yes",
	"-o", "PasswordAuthentication=no",
	"-o", "KbdInteractiveAuthentication=no",
	"-o", "PubkeyAuthentication=yes",
	"-o", "PreferredAuthentications=publickey",
	"-o", "GSSAPIAuthentication=no",
	"-o", "HostbasedAuthentication=no",
	"-o", "ControlMaster=no",
	"-o", "ControlPath=none",
}

// Target is the Docker target: Host is a remote ssh:// endpoint, "" the local Docker Desktop.
type Target struct {
	Host string
}

func (t Target) Remote() bool { return t.Host != "" }

// RemoteEndpoint is whether endpoint, as CLI.Endpoint gives it, is a remote target rather than the local Docker
// Desktop.
func RemoteEndpoint(endpoint string) bool { return targetHost.MatchString(endpoint) }

// LoadTarget reads home's TargetFile: {"host": "ssh://[user@]host[:port]"}, nothing else; no file is the local target.
func LoadTarget(home string) (Target, error) {
	path := filepath.Join(home, TargetFile)
	invalid := func(format string, args ...any) (Target, error) {
		return Target{}, &Unreachable{Endpoint: path, Reason: "invalid target setting (" + fmt.Sprintf(format, args...) + ")"}
	}
	f, err := os.Open(path)
	if errors.Is(err, fs.ErrNotExist) {
		return Target{}, nil
	}
	if err != nil {
		return invalid("%v", err)
	}
	defer f.Close()
	data, err := io.ReadAll(io.LimitReader(f, maxTarget+1))
	if err != nil {
		return invalid("%v", err)
	}
	if len(data) > maxTarget {
		return invalid("larger than %d bytes", maxTarget)
	}
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	var v struct {
		Host *string `json:"host"`
	}
	if err := dec.Decode(&v); err != nil {
		return invalid("%v", err)
	}
	if _, err := dec.Token(); err != io.EOF {
		return invalid("trailing data")
	}
	if v.Host == nil {
		return invalid("no host")
	}
	host := *v.Host
	if !targetHost.MatchString(host) {
		return invalid("host %q is not ssh://[user@]host[:port]", host)
	}
	if m := portSuffix.FindStringSubmatch(host); m != nil {
		if port, _ := strconv.Atoi(m[1]); port < 1 || port > 65535 {
			return invalid("host %q is not ssh://[user@]host[:port]", host)
		}
	}
	return Target{Host: host}, nil
}

// SSHArgs is the system ssh's argument list for args, the docker CLI's.
func SSHArgs(args []string) []string {
	return append(append([]string(nil), sshOptions...), args...)
}

// remoteReason names why the docker CLI could not reach a remote target from its stderr: ssh's and the remote
// shell's words, else a failed connection.
func remoteReason(stderr string) string {
	switch {
	case strings.Contains(stderr, "REMOTE HOST IDENTIFICATION HAS CHANGED"):
		return "host key mismatch: differs from ~/.ssh/known_hosts"
	case strings.Contains(stderr, "host key is known") || strings.Contains(stderr, "Host key verification failed"):
		return "host key unknown: not in ~/.ssh/known_hosts"
	case strings.Contains(stderr, "Permission denied"):
		return "authentication failed"
	case strings.Contains(stderr, "command not found") || strings.Contains(stderr, "docker: not found") ||
		strings.Contains(stderr, "is not recognized as"):
		return "no docker CLI on the remote login shell's PATH"
	case strings.Contains(stderr, "Cannot connect to the Docker daemon"):
		return "Docker daemon not running"
	}
	return "connection failed"
}

// connectDetail is the last line of what ssh printed, as the docker CLI quotes it after stderr=, else its own.
func connectDetail(stderr string) string {
	if i := strings.LastIndex(stderr, "stderr="); i >= 0 {
		stderr = stderr[i+len("stderr="):]
	}
	return Diagnostic(&CommandError{Stderr: stderr})
}

// engine checks a probe's Server version against the floor.
func engine(out []byte) string {
	version := strings.TrimSpace(string(out))
	m := engineMajor.FindStringSubmatch(version)
	if m == nil {
		if shown.MatchString(version) {
			return "unrecognized Docker Engine version " + version
		}
		return "unrecognized Docker Engine version"
	}
	if major, err := strconv.Atoi(m[1]); err != nil || major < minEngine {
		if !shown.MatchString(version) {
			version = m[1]
		}
		return "Docker Engine " + version + " is older than " + strconv.Itoa(minEngine)
	}
	return ""
}
