package launcher

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"time"

	"ensemble/broker/internal/audit"
	"ensemble/broker/internal/credential"
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
func (h Host) Start(ctx context.Context, path string) (result StartResult, retErr error) {
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
	defer func() {
		if retErr == nil && progress != nil {
			fmt.Fprintf(&times, "ensemble-broker: start total %.2f s\n", time.Since(began).Seconds())
			_, _ = io.WriteString(progress, times.String()+notes.String())
		}
	}()
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
	px, err := h.checkProxy(ctx, m.Gateway)
	if err != nil {
		return StartResult{}, err
	}
	route, err := h.caller(ctx, px, m.Gateway)
	if err != nil {
		return StartResult{}, err
	}
	switch m.Client {
	case "codex":
		if err := h.versionPolicy(); err != nil {
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
	if len(m.Secrets) > 0 {
		items := make([]string, len(m.Secrets))
		for i, s := range m.Secrets {
			items[i] = s.Item
		}
		if err := h.Env.Ready(items); err != nil {
			return StartResult{}, err
		}
	}
	if err := h.checkStatic(staticRefs(m)); err != nil {
		return StartResult{}, err
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
	lostName := false
	created, err := h.ensureVolume(ctx, m, info)
	if err != nil {
		return StartResult{}, err
	}
	lap("checks")
	if created && m.Prefetch != nil {
		// Registered before the lock's, so it runs after every rollback of the container. A start that lost the
		// container name to another leaves the workspace to that one.
		defer func() {
			var e *protocol.Error
			if errors.As(retErr, &e) && !lostName {
				h.removeVolume(ctx, m.Workspace, e)
			}
		}()
		if err := h.prefetch(ctx, c, m, info, px); err != nil {
			return StartResult{}, err
		}
		lap("prefetch")
	}
	var held holding
	if m.Lock != "" {
		mutex, err := h.acquireMutex(ctx, m.Lock, info.ID)
		if err != nil {
			return StartResult{}, err
		}
		defer func() {
			if cleanup := h.releaseMutex(ctx, mutex); cleanup != nil {
				if retErr == nil {
					if m.GitHubApp != "" {
						rctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 30*time.Second)
						revoked := h.GitHub.Release(rctx, c)
						cancel()
						if revoked != nil {
							cleanup.Message += " " + injected(revoked, protocol.KeychainFailed, "Revoking the GitHub tokens failed; retry their pending revocation.").Message
						}
					}
					h.rollback(ctx, c, cleanup)
					h.dropTokens(m)
					h.releaseCodex(ctx, c, held, cleanup)
					h.withdraw(c, m, held, cleanup)
					result, retErr = StartResult{}, cleanup
				} else {
					var e *protocol.Error
					if errors.As(retErr, &e) {
						e.Message += " " + cleanup.Message
						if e.Code == protocol.Busy {
							e.Code, e.Holder = protocol.StartFailed, nil
						}
					} else {
						retErr = cleanup
					}
				}
			}
		}()
		holder, err := h.lockHolder(ctx, m.Lock, "")
		if err != nil {
			return StartResult{}, err
		}
		if holder != nil {
			return StartResult{}, busy(holder)
		}
	}
	result, held, err = h.create(ctx, c, m, info, route, facts, lap)
	if errors.Is(err, errTaken) {
		lostName = true
		return StartResult{}, taken(c)
	}
	if err != nil {
		var e *protocol.Error
		if !errors.As(err, &e) {
			e = protocol.New(protocol.StartFailed, "Start failed.")
		}
		h.rollback(ctx, c, e)
		h.dropTokens(m)
		h.releaseCodex(ctx, c, held, e)
		if e.Code == protocol.Busy && e.Cleanup == protocol.Failed {
			e.Code, e.Holder = protocol.StartFailed, nil
			e.Message = "Cannot remove yielding container " + c + "."
		}
		h.withdraw(c, m, held, e)
		return StartResult{}, e
	}
	result.GitHubUserID = userID
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
		// 1 GiB: Codex's log DB alone outgrows its 64 MiB budget (counts live rows only, never WAL or free pages)
		// and no setting disables or caps it.
		tmpfs(m.CodexDir, "rw,noexec,nosuid,nodev,size=1073741824"+owner)
		tmpfs(docker.RunDir, "rw,noexec,nosuid,nodev,size=1048576"+owner)
		bind(filepath.Join(m.CodexDir, "sessions"))
	} else {
		bind(m.StateDir())
	}
	size := 65536
	if n := len(m.Secrets); n > 0 {
		size += 4096 + MaxSecretBytes*n // the env directory and each secret at its largest
	}
	tmpfs(docker.SecretsDir, "rw,noexec,nosuid,nodev,size="+strconv.Itoa(size)+owner)
	return flags, list
}

// holding is the credentials a container being started holds: its client's, the environment secrets written, and
// whether a Codex grant may be recorded.
type holding struct {
	client  bool
	codex   bool
	secrets []string
}

// releaseCodex withdraws c's Codex grant, when its start recorded one, on a context of its own; a failure is added to
// e's message.
func (h Host) releaseCodex(ctx context.Context, c string, held holding, e *protocol.Error) {
	if !held.codex {
		return
	}
	rctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 30*time.Second)
	defer cancel()
	if err := h.Codex.Release(rctx, c); err != nil {
		e.Message += " " + injected(err, protocol.KeychainFailed, "Withdrawing the Codex grant failed.").Message
	}
}

// withdraw records each credential held as withdrawn when e's rollback removed c.
func (h Host) withdraw(c string, m Manifest, held holding, e *protocol.Error) {
	if e.Cleanup != protocol.Removed {
		return
	}
	if kind := clientKind(m.Client); held.client && kind != "" {
		h.Audit.Add(c, audit.Credential{Kind: kind, Result: audit.Withdrawn}, "")
	}
	for _, item := range held.secrets {
		h.Audit.Add(c, audit.Credential{Kind: audit.EnvSecret, Item: item, Result: audit.Withdrawn}, "")
	}
}

// create runs every step after the absence check; any error makes Start roll back. held: the credentials in the
// container so far.
func (h Host) create(ctx context.Context, c string, m Manifest, info imageInfo, route egress, facts *audit.StartFacts,
	lap func(phase string)) (_ StartResult, held holding, _ error) {
	labels := info.Config.Labels
	args := append([]string{"run", "--detach", "--name", c, "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", "agent"},
		route.args()...)
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
	if len(m.Secrets) > 0 {
		items := make([]string, len(m.Secrets))
		for i, s := range m.Secrets {
			items[i] = s.Item
		}
		args = append(args, "--label", secretsLabel+"="+strings.Join(items, ","))
	}
	if m.Lock != "" {
		args = append(args, "--restart", "no", "--label", lockLabel+"="+m.Lock, "--label", ownerLabel+"="+m.LockOwner)
	}
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
			return StartResult{}, held, errTaken
		}
		e := diagnosed(err, protocol.StartFailed, "Creating the container failed.")
		if text := err.Error(); strings.Contains(text, "mounts denied") || strings.Contains(text, "not shared from the host") {
			e.Message = sharing
		}
		return StartResult{}, held, e
	}
	lap("create")
	if m.Lock != "" {
		holder, err := h.lockHolder(ctx, m.Lock, c)
		if err != nil {
			return StartResult{}, held, err
		}
		if holder != nil {
			return StartResult{}, held, busy(holder)
		}
	}
	contentCheck, err := checks.ReadFile("checks/image-content-check")
	if err != nil {
		return StartResult{}, held, protocol.New(protocol.StartFailed, "Embedded image checks unreadable.")
	}
	if _, err := h.Docker.Run(ctx, []string{"exec", "--interactive", "--user", "agent", c, "/bin/sh", "-s"}, contentCheck); err != nil {
		return StartResult{}, held, failure(err, protocol.StartFailed, "Image content under /opt/ensemble is unreadable by agent; rebuild with ensemble-broker build.")
	}
	// OpenCode has no state to seed.
	if m.Client != "opencode" {
		if _, err := h.Docker.Run(ctx, asAgent(c, Python, "-I", Bootstrap, m.Client, m.StateDir(), m.CWD), nil); err != nil {
			return StartResult{}, held, failure(err, protocol.StartFailed, "Writing client bootstrap state failed.")
		}
	}
	if m.HasHook {
		if _, err := h.Docker.Run(ctx, []string{"exec", "--user", "root", c, Python, "-I", Bootstrap, "hook", m.HookPath}, nil); err != nil {
			return StartResult{}, held, failure(err, protocol.StartFailed, "Installing the hook stub failed.")
		}
	}
	lap("bootstrap")
	clients, err := h.probe(ctx, c)
	if err != nil {
		return StartResult{}, held, failure(err, protocol.StartFailed, "Cannot run the image's clients.")
	}
	lap("clients")
	switch m.Client {
	case "codex":
		err = checkCodex(clients.Codex)
	case "opencode":
		err = checkOpencode(clients.Opencode)
	}
	if err != nil {
		return StartResult{}, held, err
	}
	exec := h.agentExec(ctx, c)
	// Read again just before the Keychain reads, the closest to the versions injected.
	var record *injectedRecord
	if refs := staticRefs(m); h.Static != nil && len(refs) > 0 {
		records, err := h.Static.Load()
		if err != nil {
			return StartResult{}, held, storeFailed(err, protocol.StartFailed)
		}
		r := injectedRecords(refs, records)
		record = &r
	}
	var codexExpiry *string
	switch m.Client {
	case "codex":
		held.codex = true // a failed Inject may leave its grant prepared
		expiry, err := h.Codex.Inject(ctx, c, exec)
		if err != nil {
			return StartResult{}, held, injected(err, protocol.StartFailed, "Injecting Codex credentials failed.")
		}
		codexExpiry = &expiry
	case "claude":
		if err := h.Creds.Inject(m.ClaudeAccount, exec); err != nil {
			return StartResult{}, held, injected(err, protocol.StartFailed, "Injecting the Claude token failed.")
		}
	case "opencode":
		if len(m.OpencodeProviders) > 0 {
			if err := h.Opencode.Inject(m.OpencodeProviders, exec); err != nil {
				return StartResult{}, held, injected(err, protocol.StartFailed, "Injecting the OpenCode provider API keys failed.")
			}
		}
	}
	if kind := clientKind(m.Client); kind != "" {
		held.client = true
		h.Audit.Add(c, audit.Credential{Kind: kind, Result: audit.Issued, ExpiresAt: codexExpiry}, "")
	}
	for _, s := range m.Secrets {
		if err := h.Env.Inject(s, exec); err != nil {
			return StartResult{}, held, injected(err, protocol.StartFailed, "Writing "+s.Item+" into the container failed.")
		}
		held.secrets = append(held.secrets, s.Item)
		h.Audit.Add(c, audit.Credential{Kind: audit.EnvSecret, Item: s.Item, Result: audit.Issued}, "")
	}
	if record != nil {
		if err := h.writeInjected(m, *record, protocol.StartFailed); err != nil {
			return StartResult{}, held, err
		}
	}
	lap("credentials")
	expiries := []protocol.Expiry{}
	if m.GitHubApp != "" {
		if expiries, err = h.GitHub.Issue(ctx, m.GitHubApp, m.GitHubPermissions, c, exec); err != nil {
			return StartResult{}, held, injected(err, protocol.StartFailed, "Injecting the GitHub tokens failed.")
		}
		lap("github")
	}
	if err := h.startTokens(ctx, c, m, codexExpiry, expiries); err != nil {
		return StartResult{}, held, err
	}
	delivered := TokenRecord{Expires: map[credential.Kind][]string{credential.GitHub: expiryTimes(expiries)}}
	if codexExpiry != nil {
		delivered.Expires[credential.Codex] = []string{*codexExpiry}
	}
	return StartResult{Container: c, Endpoint: h.Endpoint, ImageDigest: info.ID, Clients: clients, Deadline: deadlineOf(delivered, heldKinds(m))},
		held, nil
}

// startTokens records the expiries a start delivered; on failure it retires the GitHub grant, which the container
// rollback alone would leave live.
func (h Host) startTokens(ctx context.Context, c string, m Manifest, codexExpiry *string, github []protocol.Expiry) error {
	var err error
	if codexExpiry != nil {
		err = h.recordTokens(m, credential.Codex, []string{*codexExpiry}, nil)
	}
	if err == nil && m.GitHubApp != "" {
		err = h.recordTokens(m, credential.GitHub, expiryTimes(github), nil)
	}
	if err == nil {
		return nil
	}
	e := protocol.New(protocol.StartFailed, tokensFailed(m).Message)
	if m.GitHubApp != "" {
		rctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 30*time.Second)
		defer cancel()
		if revoked := h.GitHub.Release(rctx, c); revoked != nil {
			e.Message += " " + injected(revoked, protocol.KeychainFailed, "Revoking the GitHub tokens failed; retry their pending revocation.").Message
		}
	}
	return e
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
