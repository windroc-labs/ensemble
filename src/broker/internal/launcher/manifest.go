package launcher

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"maps"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"unicode/utf8"

	githubapi "ensemble/broker/internal/github"
	"ensemble/broker/internal/protocol"
)

type Manifest struct {
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

	// Credentials is the credential request; nil without the field.
	Credentials *CredentialRequest
}

// Prefetch is the repo a start clones into the workspace volume it creates: Repo owner/name, Ref a branch or tag
// name, "" for the default branch.
type Prefetch struct{ Repo, Ref string }

// Secret is one environment secret a start writes: Item is its keychain reference (ValidEnvItem), File its name in
// the container's secrets directory.
type Secret struct{ Item, File string }

// CredentialRequest is a start's credential request (protocol $defs.credentials): references only, never a value; a
// kind nil is none of it.
type CredentialRequest struct {
	GitHub []Grant  // one installation token per grant
	Linear *Linear  // the role whose Linear app token the container gets
	Env    []Secret // environment secrets, as Manifest.Secrets
}

// Grant is one GitHub installation token's scope: App's token limited to Permissions, reaching Repo (owner/name)
// alone, or every repository of every installation when Repo is "".
type Grant struct {
	App         string
	Permissions map[string]string
	Repo        string
}

// Linear names the role whose Linear app token a container gets.
type Linear struct{ Role string }

// Kinds is the kinds c names, in the schema's order.
func (c CredentialRequest) Kinds() []string {
	var kinds []string
	if c.GitHub != nil {
		kinds = append(kinds, "github")
	}
	if c.Linear != nil {
		kinds = append(kinds, "linear")
	}
	if c.Env != nil {
		kinds = append(kinds, "env")
	}
	return kinds
}

// A workspace volume's labels: its kind (always kind) and its mount point.
const (
	kindLabel      = "ensemble.kind"
	kind           = "ws"
	workspaceLabel = "ensemble.workspace"
	maxProviders   = 32
	maxSecrets     = 16
	maxSecretFile  = 255
	maxGrants      = 16
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
	// A team role's name: the credential request's linear role.
	rolePattern = regexp.MustCompile(`^[a-z][a-z0-9-]{0,62}$`)
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

// str is raw's string when it is a JSON string without control characters.
func str(raw json.RawMessage) (string, bool) {
	var s string
	if len(raw) == 0 || raw[0] != '"' || json.Unmarshal(raw, &s) != nil || hasControl(s) {
		return "", false
	}
	return s, true
}

// ParseManifest validates a manifest against the protocol's schema; every failure is manifest_rejected.
func ParseManifest(data []byte) (Manifest, error) {
	reject := func(format string, args ...any) (Manifest, error) {
		return Manifest{}, protocol.Newf(protocol.ManifestRejected, "Manifest rejected: "+format, args...)
	}
	if !utf8.Valid(data) {
		return reject("not UTF-8")
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
			return reject("%v", err)
		}
		if len(stack) == 0 {
			if values++; values > 1 {
				return reject("trailing data after the manifest")
			}
		}
		if n := len(stack); n > 0 && stack[n-1].object && stack[n-1].wantKey {
			if key, ok := tok.(string); ok {
				if stack[n-1].keys[key] {
					return reject("duplicate key %q", key)
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
		return reject("empty manifest")
	}

	var fields map[string]json.RawMessage
	if err := json.Unmarshal(data, &fields); err != nil || fields == nil {
		return reject("not a JSON object")
	}
	var m Manifest
	targets := map[string]*string{"name": &m.Name, "generation": &m.Generation, "image": &m.Image,
		"cwd": &m.CWD, "workspace": &m.Workspace, "client": &m.Client, "claude_dir": &m.ClaudeDir, "codex_dir": &m.CodexDir,
		"opencode_dir": &m.OpencodeDir, "hook_path": &m.HookPath, "lock": &m.Lock, "lock_owner": &m.LockOwner, "gateway": &m.Gateway}
	for key, raw := range fields {
		if key == "labels" || key == "github" || key == "opencode_providers" || key == "claude_account" || key == "secrets" || key == "prefetch" || key == "credentials" {
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
	if raw := fields["github"]; string(raw) != "null" {
		const shape = "github must be null or {\"app\": <name>, \"permissions\": {<permission>: <level>}, \"repo\"?: <owner/name>}"
		var github map[string]json.RawMessage
		if len(raw) == 0 || raw[0] != '{' || json.Unmarshal(raw, &github) != nil {
			return reject(shape)
		}
		app, ok := str(github["app"])
		rawRepo, hasRepo := github["repo"]
		repo, okRepo := str(rawRepo)
		var permissions map[string]string
		if raw := github["permissions"]; !ok || len(github) != 2 && !(len(github) == 3 && hasRepo) || hasRepo && !okRepo ||
			!appPattern.MatchString(app) || len(raw) == 0 || raw[0] != '{' || json.Unmarshal(raw, &permissions) != nil {
			return reject(shape)
		}
		if hasRepo && !githubapi.ValidRepo(repo) {
			return reject("invalid github repo %q", repo)
		}
		if err := githubapi.CheckPermissions(permissions); err != nil {
			return reject("%s", err)
		}
		m.GitHubApp, m.GitHubPermissions, m.GitHubRepo = app, permissions, repo
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
		const shape = "secrets must be an array of at most %d {\"item\", \"file\"} objects"
		var entries []map[string]json.RawMessage
		if len(raw) == 0 || raw[0] != '[' || json.Unmarshal(raw, &entries) != nil || len(entries) > maxSecrets {
			return reject(shape, maxSecrets)
		}
		m.Secrets = []Secret{}
		for _, entry := range entries {
			item, okItem := str(entry["item"])
			file, okFile := str(entry["file"])
			switch {
			case len(entry) != 2 || !okItem || !okFile:
				return reject(shape, maxSecrets)
			case !ValidEnvItem(item):
				return reject("invalid secret item %q", item)
			case !validSecretFile(file):
				return reject("invalid secret file %q", file)
			case slices.ContainsFunc(m.Secrets, func(s Secret) bool { return s.File == file }):
				return reject("repeated secret file %q", file)
			}
			m.Secrets = append(m.Secrets, Secret{Item: item, File: file})
		}
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

	if raw, ok := fields["credentials"]; ok {
		credentials, problem := parseCredentials(raw, m, fields["secrets"] != nil)
		if problem != "" {
			return reject("%s", problem)
		}
		m.Credentials = credentials
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

// object is raw's members when it is a JSON object.
func object(raw json.RawMessage) (map[string]json.RawMessage, bool) {
	var fields map[string]json.RawMessage
	if len(raw) == 0 || raw[0] != '{' || json.Unmarshal(raw, &fields) != nil {
		return nil, false
	}
	return fields, true
}

// list is raw's elements when it is a JSON array of 1 to most.
func list(raw json.RawMessage, most int) ([]json.RawMessage, bool) {
	var items []json.RawMessage
	if len(raw) == 0 || raw[0] != '[' || json.Unmarshal(raw, &items) != nil || len(items) == 0 || len(items) > most {
		return nil, false
	}
	return items, true
}

// onlyKeys is whether every key of fields is one of keys.
func onlyKeys(fields map[string]json.RawMessage, keys ...string) bool {
	for key := range fields {
		if !slices.Contains(keys, key) {
			return false
		}
	}
	return true
}

// parseCredentials checks a manifest's credentials, m its other fields parsed, by the schema's rules plus a repo or
// env file repeated. Its problem, "" for none, names the kind, index and key at fault, a key only when it is a known
// name, never a value: a rejected key or value may be a pasted secret.
func parseCredentials(raw json.RawMessage, m Manifest, hasSecrets bool) (*CredentialRequest, string) {
	fields, ok := object(raw)
	if !ok {
		return nil, "credentials: want an object"
	}
	if !onlyKeys(fields, "github", "linear", "env") {
		return nil, "credentials: an unknown kind"
	}
	c := &CredentialRequest{}
	if raw, ok := fields["github"]; ok {
		if m.GitHubApp != "" {
			return nil, "credentials.github: needs github null"
		}
		items, ok := list(raw, maxGrants)
		if !ok {
			return nil, fmt.Sprintf("credentials.github: want 1 to %d grants", maxGrants)
		}
		c.GitHub = []Grant{}
		for i, raw := range items {
			g, problem := parseGrant(raw)
			if problem == "" {
				switch {
				case g.Repo == "" && len(items) > 1:
					problem = "repo missing beside another grant"
				case g.Repo == "" && m.Prefetch != nil:
					problem = "repo missing beside prefetch"
				case slices.ContainsFunc(c.GitHub, func(o Grant) bool { return strings.EqualFold(o.Repo, g.Repo) }):
					problem = "repo repeated"
				}
			}
			if problem != "" {
				return nil, fmt.Sprintf("credentials.github[%d]: %s", i, problem)
			}
			c.GitHub = append(c.GitHub, g)
		}
	}
	if raw, ok := fields["linear"]; ok {
		linear, ok := object(raw)
		if !ok {
			return nil, "credentials.linear: want an object"
		}
		rawRole, hasRole := linear["role"]
		role, ok := str(rawRole)
		switch {
		case !onlyKeys(linear, "role"):
			return nil, "credentials.linear: an unknown key"
		case !hasRole:
			return nil, "credentials.linear: role missing"
		case !ok || !rolePattern.MatchString(role):
			return nil, "credentials.linear: role not valid"
		}
		c.Linear = &Linear{Role: role}
	}
	if raw, ok := fields["env"]; ok {
		if hasSecrets {
			return nil, "credentials.env: needs no secrets"
		}
		items, ok := list(raw, maxSecrets)
		if !ok {
			return nil, fmt.Sprintf("credentials.env: want 1 to %d secrets", maxSecrets)
		}
		c.Env = []Secret{}
		for i, raw := range items {
			s, problem := parseEnvSecret(raw)
			if problem == "" && slices.ContainsFunc(c.Env, func(o Secret) bool { return o.File == s.File }) {
				problem = "file repeated"
			}
			if problem != "" {
				return nil, fmt.Sprintf("credentials.env[%d]: %s", i, problem)
			}
			c.Env = append(c.Env, s)
		}
	}
	return c, ""
}

// parseGrant checks one manifest-form grant {"app", "permissions", "repo"?} as the legacy github object; problem as
// parseCredentials'. githubapi.CheckPermissions's message quotes a level, so only its verdict is used.
func parseGrant(raw json.RawMessage) (Grant, string) {
	fields, ok := object(raw)
	if !ok {
		return Grant{}, "want an object"
	}
	if !onlyKeys(fields, "app", "permissions", "repo") {
		return Grant{}, "an unknown key"
	}
	rawApp, hasApp := fields["app"]
	app, okApp := str(rawApp)
	rawPermissions, hasPermissions := fields["permissions"]
	var permissions map[string]string
	okPermissions := len(rawPermissions) > 0 && rawPermissions[0] == '{' && json.Unmarshal(rawPermissions, &permissions) == nil && len(permissions) > 0
	rawRepo, hasRepo := fields["repo"]
	repo, okRepo := str(rawRepo)
	switch {
	case !hasApp:
		return Grant{}, "app missing"
	case !okApp || !appPattern.MatchString(app):
		return Grant{}, "app not valid"
	case !hasPermissions:
		return Grant{}, "permissions missing"
	case !okPermissions:
		return Grant{}, "permissions not valid"
	case githubapi.CheckPermissions(permissions) != nil:
		return Grant{}, permissionProblem(permissions)
	case hasRepo && (!okRepo || !githubapi.ValidRepo(repo)):
		return Grant{}, "repo not valid"
	}
	return Grant{App: app, Permissions: permissions, Repo: repo}, ""
}

// permissionProblem names the first entry of permissions githubapi.CheckPermissions refuses: "<name> not valid" for a
// permission GitHub knows, else "an unknown key".
func permissionProblem(permissions map[string]string) string {
	for _, name := range slices.Sorted(maps.Keys(permissions)) {
		if githubapi.CheckPermissions(map[string]string{name: permissions[name]}) == nil {
			continue
		}
		if githubapi.CheckPermissions(map[string]string{name: "read"}) == nil || githubapi.CheckPermissions(map[string]string{name: "write"}) == nil {
			return name + " not valid"
		}
		return "an unknown key"
	}
	return "permissions not valid"
}

// parseEnvSecret checks one env entry {"item", "file"} by the legacy secrets' rules; problem as parseCredentials'.
func parseEnvSecret(raw json.RawMessage) (Secret, string) {
	fields, ok := object(raw)
	if !ok {
		return Secret{}, "want an object"
	}
	rawItem, hasItem := fields["item"]
	item, okItem := str(rawItem)
	rawFile, hasFile := fields["file"]
	file, okFile := str(rawFile)
	switch {
	case !onlyKeys(fields, "item", "file"):
		return Secret{}, "an unknown key"
	case !hasItem:
		return Secret{}, "item missing"
	case !hasFile:
		return Secret{}, "file missing"
	case !okItem || !ValidEnvItem(item):
		return Secret{}, "item not valid"
	case !okFile || !validSecretFile(file):
		return Secret{}, "file not valid"
	}
	return Secret{Item: item, File: file}, ""
}
