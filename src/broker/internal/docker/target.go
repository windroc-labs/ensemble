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
	"slices"
	"strconv"
	"strings"
)

const (
	// Without TargetFile, every docker call reaches the local Docker Desktop.
	TargetFile = ".ensemble/docker/target.json"
	// SSHDir holds only ssh, a root-owned link to this broker.
	SSHDir          = "/usr/local/libexec/ensemble/ssh"
	SSHPath         = "/usr/bin/ssh"
	TargetPattern   = `ssh://([A-Za-z0-9._-]{1,64}@)?[A-Za-z0-9][A-Za-z0-9.-]{0,252}(:[0-9]{1,5})?`
	maxTarget       = 64 << 10
	minEngine       = 26
	EnvItemPrefix   = "ensemble-broker-env/"
	maxLocalSecrets = 16
)

var (
	targetHost  = regexp.MustCompile(`^` + TargetPattern + `$`)
	portSuffix  = regexp.MustCompile(`:([0-9]+)$`)
	engineMajor = regexp.MustCompile(`^([0-9]+)\.`)
	// shown is a version safe to repeat in a message.
	shown = regexp.MustCompile(`^[0-9A-Za-z.+~_-]{1,32}$`)
	// Lowercase, so the keychain's case handling cannot merge two names.
	envNamePattern = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]{0,63}$`)
)

func ValidEnvName(name string) bool { return envNamePattern.MatchString(name) }

func ValidEnvItem(item string) bool {
	name, ok := strings.CutPrefix(item, EnvItemPrefix)
	return ok && ValidEnvName(name)
}

// sshOptions come before docker's own ssh arguments: ssh keeps an option's first value.
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

// Target's Host is "" for the local Docker Desktop; a worker given any of LocalSecrets runs there whatever Host is.
type Target struct {
	Host         string
	LocalSecrets []string
}

func (t Target) Remote() bool { return t.Host != "" }

// LoadTarget admits only the keys host and local_secrets; no file is the local target.
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
		Host         *string   `json:"host"`
		LocalSecrets *[]string `json:"local_secrets"`
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
	// The decoder matches keys case-insensitively, so a case variant could override a key.
	var keys map[string]json.RawMessage
	_ = json.Unmarshal(data, &keys)
	for key := range keys {
		if key != "host" && key != "local_secrets" {
			return invalid("unknown key %q", key)
		}
	}
	host := *v.Host
	if !targetHost.MatchString(host) {
		return invalid("host %q is not ssh://[user@]host[:port]", host)
	}
	if m := portSuffix.FindStringSubmatch(host); m != nil {
		if port, err := strconv.Atoi(m[1]); err != nil || port < 1 || port > 65535 {
			return invalid("host %q is not ssh://[user@]host[:port]", host)
		}
	}
	if v.LocalSecrets == nil {
		return invalid("no local_secrets")
	}
	secrets := *v.LocalSecrets
	if len(secrets) > maxLocalSecrets {
		return invalid("more than %d local_secrets", maxLocalSecrets)
	}
	for i, item := range secrets {
		if !ValidEnvItem(item) {
			return invalid("local_secrets item %q is not %s<name>", item, EnvItemPrefix)
		}
		if slices.Contains(secrets[:i], item) {
			return invalid("local_secrets item %q repeated", item)
		}
	}
	return Target{Host: host, LocalSecrets: secrets}, nil
}

func SSHArgs(args []string) []string {
	return append(append([]string(nil), sshOptions...), args...)
}

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

// connectDetail is ssh's last line, which the docker CLI quotes after stderr=, else its own.
func connectDetail(stderr string) string {
	if i := strings.LastIndex(stderr, "stderr="); i >= 0 {
		stderr = stderr[i+len("stderr="):]
	}
	return Diagnostic(&CommandError{Stderr: stderr})
}

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
