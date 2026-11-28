package docker

import (
	"errors"
	"fmt"
	"io"
	"io/fs"
	"maps"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strconv"
	"strings"

	"github.com/BurntSushi/toml"
)

const (
	// Without a SettingTable in SettingFile, every docker call reaches the local Docker Desktop.
	SettingFile  = ".ensemble/config.local.toml"
	SettingTable = "docker"
	// SSHDir holds only ssh, a root-owned link to this broker.
	SSHDir          = "/usr/local/libexec/ensemble/ssh"
	SSHPath         = "/usr/bin/ssh"
	TargetPattern   = `ssh://([A-Za-z0-9._-]{1,64}@)?[A-Za-z0-9][A-Za-z0-9.-]{0,252}(:[0-9]{1,5})?`
	maxSetting      = 1 << 20
	minEngine       = 26
	EnvItemPrefix   = "ensemble-broker-env/"
	maxLocalSecrets = 16
	maxHosts        = 8
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

// Target's Hosts are the remote addresses in the setting's order, none for the local Docker Desktop alone; a worker
// given any of LocalSecrets runs on the local Docker Desktop whatever Hosts are.
type Target struct {
	Hosts        []string
	LocalSecrets []string
}

func (t Target) Remote() bool { return len(t.Hosts) > 0 }

// readSetting reads one byte past maxSetting at most.
func readSetting(path string) ([]byte, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	data, err := io.ReadAll(io.LimitReader(f, maxSetting+1))
	if err := errors.Join(err, f.Close()); err != nil {
		return nil, err
	}
	return data, nil
}

func validHost(host string) bool {
	if !targetHost.MatchString(host) {
		return false
	}
	m := portSuffix.FindStringSubmatch(host)
	if m == nil {
		return true
	}
	port, err := strconv.Atoi(m[1])
	return err == nil && port >= 1 && port <= 65535
}

// LoadTarget reads SettingFile's SettingTable, admitting only the keys host, hosts and local_secrets; no file or no table is
// the local target. Keys are compared exactly: TOML keys are case-sensitive.
func LoadTarget(home string) (Target, error) {
	path := filepath.Join(home, SettingFile)
	table, err := settingTable(path)
	target := Target{}
	if err == nil && table != nil {
		target, err = parseTarget(table)
	}
	if err != nil {
		return Target{}, &Unreachable{Endpoint: path + " [" + SettingTable + "]", Reason: "invalid target setting (" + err.Error() + ")"}
	}
	return target, nil
}

// settingTable is nil when path or its SettingTable is absent.
func settingTable(path string) (map[string]any, error) {
	data, err := readSetting(path)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if len(data) > maxSetting {
		return nil, fmt.Errorf("larger than %d bytes", maxSetting)
	}
	var file map[string]any
	if err := toml.Unmarshal(data, &file); err != nil {
		return nil, err
	}
	found, ok := file[SettingTable]
	if !ok {
		return nil, nil
	}
	table, ok := found.(map[string]any)
	if !ok {
		return nil, fmt.Errorf("[%s] is not a table", SettingTable)
	}
	return table, nil
}

func parseTarget(table map[string]any) (Target, error) {
	for _, key := range slices.Sorted(maps.Keys(table)) {
		if key != "host" && key != "hosts" && key != "local_secrets" {
			return Target{}, fmt.Errorf("unknown key %q", key)
		}
	}
	hosts, err := targetHosts(table)
	if err != nil {
		return Target{}, err
	}
	secrets, err := localSecrets(table)
	if err != nil {
		return Target{}, err
	}
	return Target{Hosts: hosts, LocalSecrets: secrets}, nil
}

// targetHosts is host as a list of one, else hosts in order.
func targetHosts(table map[string]any) ([]string, error) {
	value, single := table["host"]
	list, many := table["hosts"]
	switch {
	case single && many:
		return nil, errors.New("host and hosts both set")
	case many:
		return hostList(list)
	case !single:
		return nil, errors.New("no host")
	}
	host, ok := value.(string)
	if !ok {
		return nil, errors.New("host is not a string")
	}
	if !validHost(host) {
		return nil, fmt.Errorf("host %q is not ssh://[user@]host[:port]", host)
	}
	return []string{host}, nil
}

func hostList(value any) ([]string, error) {
	items, ok := value.([]any)
	switch {
	case !ok:
		return nil, errors.New("hosts is not an array")
	case len(items) == 0:
		return nil, errors.New("hosts is empty")
	case len(items) > maxHosts:
		return nil, fmt.Errorf("more than %d hosts", maxHosts)
	}
	hosts := make([]string, 0, len(items))
	for _, value := range items {
		host, ok := value.(string)
		if !ok || !validHost(host) {
			return nil, fmt.Errorf("hosts item %q is not ssh://[user@]host[:port]", fmt.Sprint(value))
		}
		if slices.Contains(hosts, host) {
			return nil, fmt.Errorf("hosts item %q repeated", host)
		}
		hosts = append(hosts, host)
	}
	return hosts, nil
}

func localSecrets(table map[string]any) ([]string, error) {
	value, ok := table["local_secrets"]
	if !ok {
		return nil, errors.New("no local_secrets")
	}
	items, ok := value.([]any)
	if !ok {
		return nil, errors.New("local_secrets is not an array")
	}
	if len(items) > maxLocalSecrets {
		return nil, fmt.Errorf("more than %d local_secrets", maxLocalSecrets)
	}
	secrets := make([]string, 0, len(items))
	for _, value := range items {
		item, ok := value.(string)
		if !ok || !ValidEnvItem(item) {
			return nil, fmt.Errorf("local_secrets item %q is not %s<name>", fmt.Sprint(value), EnvItemPrefix)
		}
		if slices.Contains(secrets, item) {
			return nil, fmt.Errorf("local_secrets item %q repeated", item)
		}
		secrets = append(secrets, item)
	}
	return secrets, nil
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
