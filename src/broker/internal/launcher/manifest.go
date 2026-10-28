package launcher

import (
	"bytes"
	"encoding/json"
	"ensemble/broker/internal/credential"
	"errors"
	"fmt"
	"io"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"unicode/utf8"

	githubapi "ensemble/broker/internal/github"
	"ensemble/broker/internal/protocol"
)

type Manifest struct {
	Linear                                                                                      *credential.LinearRequest
	Name, Generation, Image, CWD, Workspace, Client, ClaudeDir, CodexDir, OpencodeDir, HookPath string
	HasHook                                                                                     bool
	Labels                                                                                      map[string]string
	GitHubApp                                                                                   string            // the App whose tokens the session gets; "": github null
	GitHubPermissions                                                                           map[string]string // the repository permissions each of its tokens is limited to; nil: github null
	GitHubRepo                                                                                  string            // the one repository its tokens reach (owner/name); "": every repository of every installation
	OpencodeProviders                                                                           []string          // the providers whose API keys an opencode container gets
	ClaudeAccount                                                                               string            // the account whose setup-token a claude container gets; "" for other clients
	Secrets                                                                                     []Secret          // the environment secrets the container gets; nil without the field
	Lock, LockOwner                                                                             string            // optional lock name and readable owner
	Gateway                                                                                     string            // the egress proxy's caller network key; workers without the field
	Prefetch                                                                                    *Prefetch         // the repo cloned into a new workspace; nil without the field
}

// Prefetch is the repo a start clones into the workspace volume it creates: Repo owner/name, Ref a branch or tag
// name, "" for the default branch.
type Prefetch struct{ Repo, Ref string }

// Secret is one environment secret a start writes: Item is its keychain reference (ValidEnvItem), File its name in
// the container's secrets directory.
type Secret struct{ Item, File string }

// A workspace volume's labels: its kind (always kind) and its mount point.
const (
	kindLabel      = "ensemble.kind"
	kind           = "ws"
	workspaceLabel = "ensemble.workspace"
	maxProviders   = 32
	maxSecrets     = 16
	maxSecretFile  = 255
	// MaxSecretBytes caps one environment secret's value: login env's limit and the secrets tmpfs budget.
	MaxSecretBytes = 16384
)

var (
	namePattern       = regexp.MustCompile(`^[A-Za-z0-9_-]+$`)
	generationPattern = regexp.MustCompile(`^[0-9a-f]{32}$`)
	imagePattern      = regexp.MustCompile(`^ensemble-worker:[0-9a-f]{64}$`)
	labelKeyPattern   = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]*$`)
	appPattern        = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,62}$`)
	lockPattern       = regexp.MustCompile(`^[a-z0-9][a-z0-9_.-]{0,62}$`)
	// A caller network key: one label path segment, windroc.egress.network.<key>.name.
	gatewayPattern = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,62}$`)
	refChars       = regexp.MustCompile(`^[A-Za-z0-9._/-]+$`)
	shaPattern     = regexp.MustCompile(`^[0-9a-fA-F]{7,64}$`)
	// An OpenCode provider's name: its keychain account and its key file's name, <name>.key.
	providerPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`)
	// A Claude account's name: its setup-token item's keychain account. Lowercase only, so the keychain's case
	// handling cannot merge two names.
	claudeAccountPattern = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]{0,63}$`)
	// An environment secret's name, as Claude accounts: lowercase so the keychain's case handling cannot merge two.
	envNamePattern = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]{0,63}$`)
)

// envItemPrefix starts every environment secret's keychain service; the broker package's envServicePrefix is the same.
const envItemPrefix = "ensemble-broker-env/"

// ValidEnvName is whether name can be an environment secret, item ensemble-broker-env/<name>.
func ValidEnvName(name string) bool { return envNamePattern.MatchString(name) }

// ValidEnvItem is whether item is an environment secret's reference, ensemble-broker-env/<name>; no other keychain service is.
func ValidEnvItem(item string) bool {
	name, ok := strings.CutPrefix(item, envItemPrefix)
	return ok && ValidEnvName(name)
}

// validSecretFile is whether file can name a secret in the container: one path component of at most maxSecretFile
// bytes, with no C0 or C1 control character, not . or ..
func validSecretFile(file string) bool {
	return file != "" && len(file) <= maxSecretFile && file != "." && file != ".." && !strings.ContainsFunc(file, func(r rune) bool {
		return r == '/' || r < 0x20 || r >= 0x7f && r <= 0x9f
	})
}

func ValidLock(name string) bool { return lockPattern.MatchString(name) }

func validGateway(key string) bool { return gatewayPattern.MatchString(key) }

// ValidRef is whether ref names a branch or tag, by ensemble.utils.git.check_ref's rule (WDRC-109 FR-52): never an
// option, a full ref, HEAD or something that looks like a commit SHA.
func ValidRef(ref string) bool {
	if len(ref) > 255 || !refChars.MatchString(ref) || strings.HasPrefix(ref, "-") || ref == "HEAD" ||
		strings.HasPrefix(ref, "refs/") || shaPattern.MatchString(ref) || strings.Contains(ref, "..") || strings.HasSuffix(ref, ".") {
		return false
	}
	for _, part := range strings.Split(ref, "/") {
		if part == "" || strings.HasPrefix(part, ".") || strings.HasSuffix(part, ".lock") {
			return false
		}
	}
	return true
}

// ValidProvider is whether name can be a listed OpenCode provider; ollama needs no key.
func ValidProvider(name string) bool { return providerPattern.MatchString(name) && name != "ollama" }

// DefaultClaudeAccount is the Claude account a start without a selection uses.
const DefaultClaudeAccount = "default"

// ValidClaudeAccount is whether name can be a Claude account; setup-token is the retired single item's account.
func ValidClaudeAccount(name string) bool {
	return claudeAccountPattern.MatchString(name) && name != "setup-token"
}

func hasControl(s string) bool {
	return strings.ContainsFunc(s, func(r rune) bool { return r < 0x20 || r == 0x7f })
}

// ParseManifest validates a manifest against the protocol's schema; every failure is manifest_rejected.
func ParseManifest(data []byte) (Manifest, error) {
	reject := func(format string, args ...any) (Manifest, error) {
		return Manifest{}, protocol.Newf(protocol.ManifestRejected, "Manifest rejected: "+format, args...)
	}
	fields, err := manifestObject(data)
	if err != nil {
		return reject("%s", err)
	}
	str := manifestString
	var m Manifest
	targets := map[string]*string{"name": &m.Name, "generation": &m.Generation, "image": &m.Image,
		"cwd": &m.CWD, "workspace": &m.Workspace, "client": &m.Client, "claude_dir": &m.ClaudeDir, "codex_dir": &m.CodexDir,
		"opencode_dir": &m.OpencodeDir, "hook_path": &m.HookPath, "lock": &m.Lock, "lock_owner": &m.LockOwner, "gateway": &m.Gateway}
	for key, raw := range fields {
		if key == "labels" || key == "github" || key == "opencode_providers" || key == "claude_account" || key == "secrets" || key == "prefetch" || key == "credentials" || key == "environment_credentials" {
			continue
		}
		target, ok := targets[key]
		if !ok {
			return reject("unknown field %q", key)
		}
		if *target, ok = str(raw); !ok {
			return reject("field %q must be a string without control characters", key)
		}
	}
	for _, key := range []string{"name", "generation", "image", "cwd", "workspace", "client", "github", "labels"} {
		if _, ok := fields[key]; !ok {
			return reject("missing field %q", key)
		}
	}
	_, m.HasHook = fields["hook_path"]
	own := false
	for _, client := range []string{"claude", "codex", "opencode"} {
		_, present := fields[client+"_dir"]
		if present != (m.Client == client) {
			return reject("client requires exactly its own state directory")
		}
		own = own || present
	}
	if !own {
		return reject("client requires exactly its own state directory")
	}
	switch {
	case !namePattern.MatchString(m.Name) || m.Name == "none":
		return reject("invalid name")
	case !generationPattern.MatchString(m.Generation):
		return reject("invalid generation")
	case !imagePattern.MatchString(m.Image):
		return reject("image must be ensemble-worker:<64 hex>")
	case m.Workspace != "ensemble-ws-"+m.Name:
		return reject("workspace must be ensemble-ws-<name>")
	case !strings.HasPrefix(m.CWD, "/") || !strings.HasPrefix(m.StateDir(), "/") || m.HasHook && !strings.HasPrefix(m.HookPath, "/"):
		return reject("paths must be absolute")
	}
	m.CWD = filepath.Clean(m.CWD) // Docker mounts at the cleaned target
	_, hasLock := fields["lock"]
	_, hasOwner := fields["lock_owner"]
	if hasLock != hasOwner || hasLock && (!ValidLock(m.Lock) || strings.TrimSpace(m.LockOwner) == "") {
		return reject("lock requires a valid lock name and nonempty lock_owner")
	}
	if _, ok := fields["gateway"]; !ok {
		m.Gateway = workersKey
	} else if !validGateway(m.Gateway) {
		return reject("invalid gateway")
	}
	if err := m.parseGitHub(fields["github"]); err != nil {
		return reject("%s", err)
	}

	if raw, ok := fields["opencode_providers"]; ok {
		if m.Client != "opencode" {
			return reject("opencode_providers requires client opencode")
		}
		var providers []string
		if len(raw) == 0 || raw[0] != '[' || json.Unmarshal(raw, &providers) != nil || len(providers) > maxProviders {
			return reject("opencode_providers must be an array of at most %d provider names", maxProviders)
		}
		for i, p := range providers {
			if !ValidProvider(p) || slices.Contains(providers[:i], p) {
				return reject("invalid or repeated provider %q", p)
			}
		}
		m.OpencodeProviders = providers
	}

	if raw, ok := fields["claude_account"]; ok {
		if m.Client != "claude" {
			return reject("claude_account requires client claude")
		}
		account, ok := str(raw)
		if !ok || !ValidClaudeAccount(account) {
			return reject("invalid claude_account")
		}
		m.ClaudeAccount = account
	} else if m.Client == "claude" {
		m.ClaudeAccount = DefaultClaudeAccount
	}

	if raw, ok := fields["secrets"]; ok {
		if err := m.parseSecrets(raw); err != nil {
			return reject("%s", err)
		}
	}
	if err := m.parseCredentials(fields); err != nil {
		return Manifest{}, err
	}

	if raw, ok := fields["prefetch"]; ok {
		const shape = "prefetch must be {\"repo\": <owner/name>, \"ref\"?: <branch or tag>}"
		var p map[string]json.RawMessage
		if len(raw) == 0 || raw[0] != '{' || json.Unmarshal(raw, &p) != nil {
			return reject(shape)
		}
		repo, ok := str(p["repo"])
		rawRef, hasRef := p["ref"]
		ref, okRef := str(rawRef)
		switch {
		case !ok || len(p) != 1 && !(len(p) == 2 && hasRef) || hasRef && !okRef:
			return reject(shape)
		case !githubapi.ValidRepo(repo):
			return reject("invalid prefetch repo %q", repo)
		case hasRef && !ValidRef(ref):
			return reject("invalid prefetch ref %q", ref)
		case m.GitHubApp != "" && m.GitHubRepo == "":
			return reject("prefetch requires github null or a github repo")
		}
		m.Prefetch = &Prefetch{Repo: repo, Ref: ref}
	}

	var labels map[string]json.RawMessage
	if raw := fields["labels"]; len(raw) == 0 || raw[0] != '{' || json.Unmarshal(raw, &labels) != nil {
		return reject("labels must be an object")
	}
	m.Labels = map[string]string{}
	for key, raw := range labels {
		value, ok := str(raw)
		if !ok || !labelKeyPattern.MatchString(key) || strings.HasPrefix(key, "ensemble.") || strings.HasPrefix(key, "windroc.") {
			return reject("invalid label %q", key)
		}
		m.Labels[key] = value
	}
	return m, nil
}

func (m Manifest) StateDir() string {
	switch m.Client {
	case "codex":
		return m.CodexDir
	case "opencode":
		return m.OpencodeDir
	}
	return m.ClaudeDir
}

func manifestObject(data []byte) (map[string]json.RawMessage, error) {
	if !utf8.Valid(data) {
		return nil, fmt.Errorf("not UTF-8")
	}
	// Reject duplicate keys and anything after the first value; Token rejects NaN and other non-JSON.
	type frame struct {
		object, wantKey bool
		keys            map[string]bool
	}
	var stack []*frame
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.UseNumber()
	values := 0
	for {
		tok, err := dec.Token()
		if err == io.EOF {
			break
		}
		if err != nil {
			return nil, fmt.Errorf("%v", err)
		}
		if len(stack) == 0 {
			if values++; values > 1 {
				return nil, fmt.Errorf("trailing data after the manifest")
			}
		}
		if n := len(stack); n > 0 && stack[n-1].object && stack[n-1].wantKey {
			if key, ok := tok.(string); ok {
				if stack[n-1].keys[key] {
					return nil, fmt.Errorf("duplicate key %q", key)
				}
				stack[n-1].keys[key], stack[n-1].wantKey = true, false
				continue
			}
		}
		switch tok {
		case json.Delim('{'):
			stack = append(stack, &frame{object: true, wantKey: true, keys: map[string]bool{}})
			continue
		case json.Delim('['):
			stack = append(stack, &frame{})
			continue
		case json.Delim('}'), json.Delim(']'):
			stack = stack[:len(stack)-1]
		}
		if n := len(stack); n > 0 && stack[n-1].object {
			stack[n-1].wantKey = true
		}
	}
	if values == 0 {
		return nil, fmt.Errorf("empty manifest")
	}

	var fields map[string]json.RawMessage
	if err := json.Unmarshal(data, &fields); err != nil || fields == nil {
		return nil, fmt.Errorf("not a JSON object")
	}
	return fields, nil
}

func manifestString(raw json.RawMessage) (string, bool) {
	var s string
	if len(raw) == 0 || raw[0] != '"' || json.Unmarshal(raw, &s) != nil || hasControl(s) {
		return "", false
	}
	return s, true
}

func (m *Manifest) parseGitHub(raw json.RawMessage) error {
	str := manifestString
	if string(raw) != "null" {
		const shape = "github must be null or {\"app\": <name>, \"permissions\": {<permission>: <level>}, \"repo\"?: <owner/name>}"
		var github map[string]json.RawMessage
		if len(raw) == 0 || raw[0] != '{' || json.Unmarshal(raw, &github) != nil {
			return errors.New(shape)
		}
		app, ok := str(github["app"])
		rawRepo, hasRepo := github["repo"]
		repo, okRepo := str(rawRepo)
		var permissions map[string]string
		if raw := github["permissions"]; !ok || len(github) != 2 && !(len(github) == 3 && hasRepo) || hasRepo && !okRepo ||
			!appPattern.MatchString(app) || len(raw) == 0 || raw[0] != '{' || json.Unmarshal(raw, &permissions) != nil {
			return errors.New(shape)
		}
		if hasRepo && !githubapi.ValidRepo(repo) {
			return fmt.Errorf("invalid github repo %q", repo)
		}
		if err := githubapi.CheckPermissions(permissions); err != nil {
			return fmt.Errorf("%s", err)
		}
		m.GitHubApp, m.GitHubPermissions, m.GitHubRepo = app, permissions, repo
	}

	return nil
}

func (m *Manifest) parseSecrets(raw json.RawMessage) error {
	str := manifestString
	{
		const shape = "secrets must be an array of at most %d {\"item\", \"file\"} objects"
		var entries []map[string]json.RawMessage
		if len(raw) == 0 || raw[0] != '[' || json.Unmarshal(raw, &entries) != nil || len(entries) > maxSecrets {
			return fmt.Errorf(shape, maxSecrets)
		}
		m.Secrets = []Secret{}
		for _, entry := range entries {
			item, okItem := str(entry["item"])
			file, okFile := str(entry["file"])
			switch {
			case len(entry) != 2 || !okItem || !okFile:
				return fmt.Errorf(shape, maxSecrets)
			case !ValidEnvItem(item):
				return fmt.Errorf("invalid secret item %q", item)
			case !validSecretFile(file):
				return fmt.Errorf("invalid secret file %q", file)
			case slices.ContainsFunc(m.Secrets, func(s Secret) bool { return s.File == file }):
				return fmt.Errorf("repeated secret file %q", file)
			}
			m.Secrets = append(m.Secrets, Secret{Item: item, File: file})
		}
	}

	return nil
}
