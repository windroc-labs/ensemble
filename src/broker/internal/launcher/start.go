package launcher

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"time"

	"ensemble/broker/internal/audit"
	"ensemble/broker/internal/codex"
	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/protocol"
)

const (
	Python    = "/opt/ensemble/venv/bin/python"
	Bootstrap = "/opt/ensemble/lib/bootstrap.py"
	checker   = "/opt/ensemble/lib/workspace-check"
	sharing   = "Docker Desktop file sharing must include ~/.ensemble/docker-state (Settings > Resources > File sharing)."
	cache     = ".ensemble-cache"
)

// errTaken: another start created the container first, so it is not this start's to roll back.
var errTaken = errors.New("container name taken")

func asAgent(container string, argv ...string) []string {
	return append([]string{"exec", "--user", "agent", container}, argv...)
}

// probe reads every client's version; the image installed them.
func (h Host) probe(ctx context.Context, container string) (Clients, error) {
	var versions [3]string
	var errs []error
	for i, client := range []string{"claude", "/opt/ensemble/clients/bin/codex", "/opt/ensemble/clients/bin/opencode"} {
		out, err := h.Docker.Run(ctx, asAgent(container, client, "--version"), nil)
		line, _, _ := strings.Cut(strings.TrimSpace(string(out)), "\n")
		versions[i] = strings.TrimSpace(line)
		if err == nil && (versions[i] == "" || hasControl(versions[i])) {
			err = errors.New(client + " --version gave no usable version")
		}
		errs = append(errs, err)
	}
	return Clients{Claude: versions[0], Codex: versions[1], Opencode: versions[2]}, errors.Join(errs...)
}

func taken(c string) *protocol.Error {
	return protocol.New(protocol.StartFailed, "Container "+c+" already exists; close it first.")
}

// Start writes each phase's seconds to Progress only once it succeeds, so a failure's stderr stays one error object.
func (h Host) Start(ctx context.Context, path string) (StartResult, error) {
	var times strings.Builder
	var notes strings.Builder
	progress := h.Progress
	h.Progress = &notes
	began := time.Now()
	last := began
	lap := func(phase string) {
		now := time.Now()
		fmt.Fprintf(&times, "ensemble-broker: start %s %.2f s\n", phase, now.Sub(last).Seconds())
		last = now
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return StartResult{}, protocol.New(protocol.ManifestRejected, "Cannot read manifest "+path+".")
	}
	m, err := ParseManifest(data)
	if err != nil {
		return StartResult{}, err
	}
	c := "ensemble-" + m.Name + "-" + m.Generation
	// Before the backlog retry and Issue, so this start's own tokens land on its line.
	facts := &audit.StartFacts{Image: m.Image}
	h.Audit.Begin(c, facts)
	if err := h.checkMounts(m); err != nil {
		return StartResult{}, err
	}
	gateway, err := h.checkEgress(ctx)
	if err != nil {
		return StartResult{}, err
	}
	var policy codex.Installation
	switch m.Client {
	case "codex":
		policy, err = h.versionPolicy(ctx)
		if err != nil {
			return StartResult{}, err
		}
		if err := h.Codex.Ready(ctx); err != nil {
			return StartResult{}, err
		}
	case "claude":
		if err := h.Creds.Ready(m.ClaudeAccount); err != nil {
			return StartResult{}, err
		}
	case "opencode":
		if len(m.OpencodeProviders) > 0 {
			if err := h.Opencode.Ready(m.OpencodeProviders); err != nil {
				return StartResult{}, err
			}
		}
	}
	var userID *int64
	if m.GitHubApp != "" {
		if err := h.GitHub.Ready(m.GitHubApp); err != nil {
			return StartResult{}, err
		}
		id, err := h.GitHub.BotUserID(m.GitHubApp)
		if err != nil {
			return StartResult{}, err
		}
		userID = &id
	}
	if _, err := h.GitHub.Retry(ctx); err != nil {
		times.WriteString("ensemble-broker: start revocations skipped\n")
	}
	lap("preflight")
	info, err := h.checkImage(ctx, m.Image)
	if err != nil {
		return StartResult{}, err
	}
	lap("image")
	facts.ImageDigest = info.ID
	if _, err := h.Docker.Run(ctx, []string{"container", "inspect", c}, nil); !docker.Absent(err) {
		if err != nil {
			return StartResult{}, failure(err, protocol.StartFailed, "Cannot check for container "+c+".")
		}
		return StartResult{}, taken(c)
	}
	if err := h.ensureVolume(ctx, m, info); err != nil {
		return StartResult{}, err
	}
	lap("checks")
	result, held, err := h.create(ctx, c, m, info, gateway, policy, facts, lap)
	if errors.Is(err, errTaken) {
		return StartResult{}, taken(c)
	}
	if err != nil {
		var e *protocol.Error
		if !errors.As(err, &e) {
			e = protocol.New(protocol.StartFailed, "Start failed.")
		}
		h.rollback(ctx, c, e)
		if kind := clientKind(m.Client); held && kind != "" && e.Cleanup == protocol.Removed {
			h.Audit.Add(c, audit.Credential{Kind: kind, Result: audit.Withdrawn}, "")
		}
		return StartResult{}, e
	}
	result.GitHubUserID = userID
	fmt.Fprintf(&times, "ensemble-broker: start total %.2f s\n", time.Since(began).Seconds())
	if progress != nil {
		_, _ = io.WriteString(progress, times.String()+notes.String())
	}
	return result, nil
}

// mounts are create's --mount and --tmpfs flags and, for the audit, what each one mounts.
func mounts(m Manifest, labels map[string]string) ([]string, []audit.Mount) {
	var flags []string
	var list []audit.Mount
	add := func(flag, value string, mount audit.Mount) {
		flags, list = append(flags, flag, value), append(list, mount)
	}
	tmpfs := func(target, options string) {
		add("--tmpfs", target+":"+options, audit.Mount{Type: "tmpfs", Target: target, Mode: "rw"})
	}
	bind := func(path string) {
		add("--mount", bindMount(path, path), audit.Mount{Type: "bind", Source: path, Target: path, Mode: "rw"})
	}
	spec, mount := workspaceMount(m.Workspace, m.CWD, false)
	add("--mount", spec, mount)
	owner := ",mode=0700,uid=" + labels["ensemble.agent-uid"] + ",gid=" + labels["ensemble.agent-gid"]
	if m.Client == "codex" {
		tmpfs(runtimeDir, "rw,exec,nosuid,nodev,size=536870912,mode=0755,uid=0,gid=0")
		// 1 GiB: Codex's log DB alone outgrows its 64 MiB budget (counts live rows only, never WAL or free pages)
		// and no setting disables or caps it.
		tmpfs(m.CodexDir, "rw,noexec,nosuid,nodev,size=1073741824"+owner)
		tmpfs(docker.RunDir, "rw,noexec,nosuid,nodev,size=1048576"+owner)
		bind(filepath.Join(m.CodexDir, "sessions"))
	} else {
		bind(m.StateDir())
	}
	tmpfs(docker.SecretsDir, "rw,noexec,nosuid,nodev,size=65536"+owner)
	return flags, list
}

// create runs every step after the absence check; any error makes Start roll back. held: the client credential is in
// the container.
func (h Host) create(ctx context.Context, c string, m Manifest, info imageInfo, gateway egress, policy codex.Installation, facts *audit.StartFacts,
	lap func(phase string)) (_ StartResult, held bool, _ error) {
	labels := info.Config.Labels
	args := append([]string{"run", "--detach", "--name", c, "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", "agent"},
		gateway.args()...)
	args = append(args, "--workdir", m.CWD)
	switch m.Client {
	case "codex":
		args = append(args, "--env", "CODEX_HOME="+m.CodexDir)
	case "opencode":
		// Self-update and LSP-server downloads would fetch unverified executables.
		args = append(args, "--env", "XDG_DATA_HOME="+m.OpencodeDir, "--env", "OPENCODE_DISABLE_AUTOUPDATE=1", "--env", "OPENCODE_DISABLE_LSP_DOWNLOAD=1")
	default:
		args = append(args, "--env", "CLAUDE_CONFIG_DIR="+m.ClaudeDir)
	}
	for _, key := range []string{"npm_config_cache", "UV_CACHE_DIR", "PIP_CACHE_DIR"} {
		args = append(args, "--env", key+"="+filepath.Join(m.CWD, cache))
	}
	args = append(args,
		"--label", "ensemble.session="+m.Name, "--label", "ensemble.generation="+m.Generation, "--label", clientLabel+"="+m.Client)
	keys := make([]string, 0, len(m.Labels))
	for k := range m.Labels {
		keys = append(keys, k)
	}
	slices.Sort(keys)
	for _, k := range keys {
		args = append(args, "--label", k+"="+m.Labels[k])
	}
	flags, list := mounts(m, labels)
	facts.Mounts = list
	args = append(append(args, flags...), "--entrypoint", "/usr/bin/tini", info.ID, "--", "sleep", "infinity")
	if _, err := h.Docker.Run(ctx, args, nil); err != nil {
		if docker.Conflict(err) {
			return StartResult{}, false, errTaken
		}
		e := diagnosed(err, protocol.StartFailed, "Creating the container failed.")
		if text := err.Error(); strings.Contains(text, "mounts denied") || strings.Contains(text, "not shared from the host") {
			e.Message = sharing
		}
		return StartResult{}, false, e
	}
	lap("create")
	// OpenCode has no state to seed.
	if m.Client != "opencode" {
		if _, err := h.Docker.Run(ctx, asAgent(c, Python, "-I", Bootstrap, m.Client, m.StateDir(), m.CWD), nil); err != nil {
			return StartResult{}, false, failure(err, protocol.StartFailed, "Writing client bootstrap state failed.")
		}
	}
	if m.HasHook {
		if _, err := h.Docker.Run(ctx, []string{"exec", "--user", "root", c, Python, "-I", Bootstrap, "hook", m.HookPath}, nil); err != nil {
			return StartResult{}, false, failure(err, protocol.StartFailed, "Installing the hook stub failed.")
		}
	}
	lap("bootstrap")
	clients, err := h.probe(ctx, c)
	if err != nil {
		return StartResult{}, false, failure(err, protocol.StartFailed, "Cannot run the image's clients.")
	}
	lap("clients")
	if m.Client == "opencode" {
		if err := checkOpencode(clients.Opencode); err != nil {
			return StartResult{}, false, err
		}
	}
	var update *ClientUpdate
	if m.Client == "codex" {
		var prepared ClientUpdate
		clients, prepared, err = h.updateCodex(ctx, c, info.Architecture, clients, policy)
		if err != nil {
			return StartResult{}, false, err
		}
		update = &prepared
		lap("client_update")
	}
	exec := h.agentExec(ctx, c)
	var codexExpiry *string
	switch m.Client {
	case "codex":
		expiry, err := h.Codex.Inject(ctx, exec)
		if err != nil {
			return StartResult{}, false, injected(err, protocol.StartFailed, "Injecting Codex credentials failed.")
		}
		codexExpiry = &expiry
	case "claude":
		if err := h.Creds.Inject(m.ClaudeAccount, exec); err != nil {
			return StartResult{}, false, injected(err, protocol.StartFailed, "Injecting the Claude token failed.")
		}
	case "opencode":
		if len(m.OpencodeProviders) > 0 {
			if err := h.Opencode.Inject(m.OpencodeProviders, exec); err != nil {
				return StartResult{}, false, injected(err, protocol.StartFailed, "Injecting the OpenCode provider API keys failed.")
			}
		}
	}
	if kind := clientKind(m.Client); kind != "" {
		held = true
		h.Audit.Add(c, audit.Credential{Kind: kind, Result: audit.Issued, ExpiresAt: codexExpiry}, "")
	}
	lap("credentials")
	expiries := []protocol.Expiry{}
	if m.GitHubApp != "" {
		if expiries, err = h.GitHub.Issue(ctx, m.GitHubApp, m.GitHubPermissions, c, exec); err != nil {
			return StartResult{}, held, injected(err, protocol.StartFailed, "Injecting the GitHub tokens failed.")
		}
		lap("github")
	}
	return StartResult{Container: c, Endpoint: h.Endpoint, ImageDigest: info.ID, Clients: clients, GitHub: expiries, CredentialExpiresAt: codexExpiry, ClientUpdate: update}, held, nil
}

// injected classifies a Credentials or Tokens error: its protocol error, an unreachable daemon, else code.
func injected(err error, code protocol.Code, message string) *protocol.Error {
	var e *protocol.Error
	if errors.As(err, &e) {
		return e
	}
	if errors.Is(err, docker.ErrUnreachable) {
		return failure(err, code, "")
	}
	return protocol.New(code, message)
}

// agentExec runs argv as agent in container c with stdin, for secret injection.
func (h Host) agentExec(ctx context.Context, c string) func(argv []string, stdin []byte) error {
	return func(argv []string, stdin []byte) error {
		_, err := h.Docker.Run(ctx, append([]string{"exec", "--interactive", "--user", "agent", c}, argv...), stdin)
		return err
	}
}

// rollback deletes c on a fresh context so a signal or cancelled caller still cleans up.
func (h Host) rollback(ctx context.Context, c string, e *protocol.Error) {
	rctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 30*time.Second)
	defer cancel()
	removed, err := h.remove(rctx, c)
	switch {
	case err != nil:
		e.Cleanup, e.CleanupError, e.Container = protocol.Failed, docker.Diagnostic(err), c
	case removed:
		e.Cleanup = protocol.Removed
	default:
		e.Cleanup = protocol.NotNeeded
	}
}
