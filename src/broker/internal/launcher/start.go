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
	// Guard is the container's main process; the harness hands it its client over docker attach.
	Guard   = "/opt/ensemble/src/ensemble/session/harness/guard.py"
	checker = "/opt/ensemble/lib/workspace-check"
	cache   = ".ensemble-cache"
)

// errTaken: another start created the container first, so it is not this start's to roll back.
var errTaken = errors.New("container name taken")

func asAgent(container string, argv ...string) []string {
	return append([]string{"exec", "--user", "agent", container}, argv...)
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
	// Before Issue, so this start's own tokens land on its line.
	facts := &audit.StartFacts{Image: m.Image}
	h.Audit.Begin(c, facts)
	if err := h.checkMounts(m); err != nil {
		return StartResult{}, err
	}
	d, r, err := h.find(ctx, m.Daemon, protocol.StartFailed)
	if err != nil {
		return StartResult{}, err
	}
	if err := h.localOnly(d, m); err != nil {
		return StartResult{}, err
	}
	px, err := h.checkProxy(ctx, d, m.Gateway)
	if err != nil {
		return StartResult{}, err
	}
	route, err := h.caller(ctx, d, px, m.Gateway)
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
	env := map[string]string{}
	if m.GitHubApp != "" {
		if err := h.GitHub.Ready(m.GitHubApp); err != nil {
			return StartResult{}, err
		}
		id, err := h.GitHub.BotUserID(m.GitHubApp)
		if err != nil {
			return StartResult{}, err
		}
		bot := m.GitHubApp + "[bot]"
		email := strconv.FormatInt(id, 10) + "+" + bot + "@users.noreply.github.com"
		env = map[string]string{
			"ENSEMBLE_PR_AUTHOR": bot,
			"GIT_AUTHOR_NAME":    bot, "GIT_AUTHOR_EMAIL": email,
			"GIT_COMMITTER_NAME": bot, "GIT_COMMITTER_EMAIL": email,
		}
	}
	if len(m.Secrets) > 0 {
		if err := h.Env.Ready(secretItems(m.Secrets)); err != nil {
			return StartResult{}, err
		}
	}
	if m.Linear != nil {
		if err := h.Linear.Ready(m.Linear.App); err != nil {
			return StartResult{}, injected(err, protocol.StartFailed, "Preparing Linear credentials failed.")
		}
	}
	lap("preflight")
	info, err := h.checkImage(ctx, d, m.Image)
	if err != nil {
		return StartResult{}, err
	}
	lap("image")
	facts.ImageDigest = info.ID
	if _, err := d.Runner.Run(ctx, []string{"container", "inspect", c}, nil); !docker.Absent(err) {
		if err != nil {
			return StartResult{}, failure(err, protocol.StartFailed, "Cannot check for container "+c+".")
		}
		return StartResult{}, taken(c)
	}
	lostName := false
	created, err := h.ensureVolume(ctx, d, m, info)
	if err != nil {
		return StartResult{}, err
	}
	// A start that lost the container name to another leaves its volumes to that one.
	if created && m.Prefetch != nil {
		defer func() {
			var e *protocol.Error
			if errors.As(retErr, &e) && !lostName {
				h.removeVolume(ctx, d, m.Workspace, e)
			}
		}()
	}
	stateCreated, err := h.ensureStateVolume(ctx, d, m, info)
	if err != nil {
		return StartResult{}, err
	}
	if stateCreated {
		defer func() {
			var e *protocol.Error
			if errors.As(retErr, &e) && !lostName {
				h.removeVolume(ctx, d, stateVolume(m.Name), e)
			}
		}()
	}
	lap("checks")
	if created && m.Prefetch != nil {
		if err := h.prefetch(ctx, d, r, c, m, info, px); err != nil {
			return StartResult{}, err
		}
		lap("prefetch")
	}
	var held holding
	if m.Lock != "" {
		lock, err := h.startLock(m.Lock)
		if err != nil {
			return StartResult{}, err
		}
		defer lock.Close()
		if err := h.lockBusy(ctx, d, m.Lock, ""); err != nil {
			return StartResult{}, err
		}
	}
	result, held, err = h.create(ctx, d, c, m, info, route, facts, lap)
	if errors.Is(err, errTaken) {
		lostName = true
		return StartResult{}, taken(c)
	}
	if err != nil {
		var e *protocol.Error
		if !errors.As(err, &e) {
			e = protocol.New(protocol.StartFailed, "Start failed.")
		}
		held.revokeTokens()
		h.rollback(ctx, d, c, e)
		h.dropTokens(m)
		if e.Code == protocol.Busy && e.Cleanup == protocol.Failed {
			e.Code, e.Holder = protocol.StartFailed, nil
			e.Message = "Cannot remove yielding container " + c + "."
		}
		h.withdraw(c, m, held, e)
		return StartResult{}, e
	}
	result.Env, result.Daemon = env, d.ID
	return result, nil
}

// codexKept outlive the container on its state volume; the session's Codex ClientHome.persistent names the same dirs.
var codexKept = []string{"sessions", "config"}

func mounts(m Manifest, labels map[string]string) ([]string, []audit.Mount) {
	var flags []string
	var list []audit.Mount
	add := func(flag, value string, mount audit.Mount) {
		flags, list = append(flags, flag, value), append(list, mount)
	}
	tmpfs := func(target, options string) {
		add("--tmpfs", target+":"+options, audit.Mount{Type: "tmpfs", Target: target, Mode: "rw"})
	}
	state := stateVolume(m.Name)
	kept := func(subpath, target string) {
		add("--mount", stateMount(state, subpath, target), audit.Mount{Type: "volume", Source: state, Target: target, Mode: "rw"})
	}
	spec, mount := workspaceMount(m.Workspace, m.CWD, false)
	add("--mount", spec, mount)
	owner := ",mode=0700,uid=" + labels["ensemble.agent-uid"] + ",gid=" + labels["ensemble.agent-gid"]
	if m.Client == "codex" {
		// 1 GiB: Codex's log DB alone outgrows its 64 MiB budget (counts live rows only, never WAL or free pages)
		// and no setting disables or caps it.
		tmpfs(m.CodexDir, "rw,noexec,nosuid,nodev,size=1073741824"+owner)
		tmpfs(docker.RunDir, "rw,noexec,nosuid,nodev,size=1048576"+owner)
		for _, dir := range codexKept {
			kept(m.Client+"/"+dir, filepath.Join(m.CodexDir, dir))
		}
	} else {
		kept(m.Client, m.StateDir())
	}
	size := 65536
	if n := len(m.Secrets); n > 0 {
		size += 4096 + MaxSecretBytes*n // the env directory and each secret at its largest
	}
	tmpfs(docker.SecretsDir, "rw,noexec,nosuid,nodev,size="+strconv.Itoa(size)+owner)
	return flags, list
}

// holding's github revokes the GitHub tokens issued (nil: none).
type holding struct {
	client  bool
	secrets []string
	github  func()
	linear  func()
}

// revokeTokens revokes this start's issued tokens; removing the container leaves them live.
func (held holding) revokeTokens() {
	if held.github != nil {
		held.github()
	}
	if held.linear != nil {
		held.linear()
	}
}

// withdraw records each credential held as withdrawn when e's rollback removed c.
func (h Host) withdraw(c string, m Manifest, held holding, e *protocol.Error) {
	if e.Cleanup != protocol.Removed {
		return
	}
	if kind := clientKind(m.Client); held.client && kind != "" {
		h.Audit.Add(c, audit.Credential{Kind: kind, Result: audit.Withdrawn})
	}
	for _, item := range held.secrets {
		h.Audit.Add(c, audit.Credential{Kind: audit.EnvSecret, Item: item, Result: audit.Withdrawn})
	}
}

// create runs every step after the absence check; any error makes Start roll back. held: the credentials in the
// container so far.
func (h Host) create(ctx context.Context, d Daemon, c string, m Manifest, info imageInfo, route egress, facts *audit.StartFacts,
	lap func(phase string)) (_ StartResult, held holding, retErr error) {
	labels := info.Config.Labels
	// Stdin open for the harness's docker attach; no log driver, so no channel data lands in docker logs.
	args := append([]string{"run", "--detach", "--interactive", "--log-driver", "none", "--name", c, "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", "agent"},
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
	args = append(append(args, flags...), "--entrypoint", "/usr/bin/tini", info.ID, "--", Python, "-I", Guard, "--serve")
	if _, err := d.Runner.Run(ctx, args, nil); err != nil {
		if docker.Conflict(err) {
			return StartResult{}, held, errTaken
		}
		return StartResult{}, held, diagnosed(err, protocol.StartFailed, "Creating the container failed.")
	}
	lap("create")
	if m.Lock != "" {
		if err := h.lockBusy(ctx, d, m.Lock, c); err != nil {
			return StartResult{}, held, err
		}
	}
	if err := h.bootstrap(ctx, d, c, m); err != nil {
		return StartResult{}, held, err
	}
	lap("bootstrap")
	clients, err := startClients(labels)
	if err != nil {
		return StartResult{}, held, err
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
	var batch startDelivery
	defer batch.erase()
	var codexExpiry *string
	var commands []credential.Command
	var message string
	switch m.Client {
	case "codex":
		message = "Injecting Codex credentials failed."
		var login CodexInjection
		login, err = h.Codex.Inject(ctx)
		if err == nil {
			commands, codexExpiry = []credential.Command{login.Command}, &login.Expiry
			defer func() {
				if endErr := login.Finish(); endErr != nil && retErr == nil {
					retErr = injected(endErr, protocol.StartFailed, message)
				}
			}()
		}
	case "claude":
		message = "Injecting the Claude token failed."
		var command credential.Command
		command, err = h.Creds.Inject(m.ClaudeAccount)
		commands = []credential.Command{command}
	case "opencode":
		message = "Injecting the OpenCode provider API keys failed."
		if len(m.OpencodeProviders) > 0 {
			commands, err = h.Opencode.Inject(m.OpencodeProviders)
		}
	}
	if err != nil {
		return StartResult{}, held, injected(err, protocol.StartFailed, message)
	}
	batch.commands = append(batch.commands, commands...)
	batch.step(message, func() {
		if kind := clientKind(m.Client); kind != "" {
			held.client = true
			h.Audit.Add(c, audit.Credential{Kind: kind, Result: audit.Issued, ExpiresAt: codexExpiry})
		}
	})
	for _, s := range m.Secrets {
		message := "Writing " + s.Item + " into the container failed."
		command, err := h.Env.Inject(s)
		if err != nil {
			return StartResult{}, held, injected(err, protocol.StartFailed, message)
		}
		batch.commands = append(batch.commands, command)
		batch.step(message, func() {
			held.secrets = append(held.secrets, s.Item)
			h.Audit.Add(c, audit.Credential{Kind: audit.EnvSecret, Item: s.Item, Result: audit.Issued})
		})
	}
	expiries := []protocol.Expiry{}
	if m.GitHubApp != "" {
		message := "Injecting the GitHub tokens failed."
		if commands, held.github, expiries, err = h.GitHub.Issue(ctx, m.GitHubApp, m.GitHubPermissions, c); err != nil {
			return StartResult{}, held, injected(err, protocol.StartFailed, message)
		}
		batch.commands = append(batch.commands, commands...)
		batch.step(message, nil)
		lap("github")
	}
	var linearExpiry string
	if m.Linear != nil {
		var writes []credential.Command
		writes, held.linear, linearExpiry, err = h.Linear.Issue(ctx, *m.Linear, c)
		if err != nil {
			return StartResult{}, held, injected(err, protocol.StartFailed, "Injecting the Linear credentials failed.")
		}
		batch.commands = append(batch.commands, writes...)
		batch.step("Injecting the Linear credentials failed.", nil)
	}
	if err := h.deliverStart(ctx, d, c, &batch); err != nil {
		return StartResult{}, held, err
	}
	lap("credentials")
	if err := h.startTokens(m, codexExpiry, expiries, linearExpiry); err != nil {
		return StartResult{}, held, err
	}
	delivered := TokenRecord{Expires: map[credential.Kind][]string{credential.GitHub: expiryTimes(expiries)}}
	if linearExpiry != "" {
		delivered.Expires[credential.Linear] = []string{linearExpiry}
	}
	if codexExpiry != nil {
		delivered.Expires[credential.Codex] = []string{*codexExpiry}
	}
	return StartResult{Container: c, Endpoint: d.Endpoint, ImageDigest: info.ID,
			Clients:  Clients{Claude: clients.Claude + " (Claude Code)", Codex: "codex-cli " + clients.Codex, Opencode: clients.Opencode},
			Deadline: deadlineOf(delivered, heldKinds(m))},
		held, nil
}

func (h Host) startTokens(m Manifest, codexExpiry *string, github []protocol.Expiry, linearExpiry string) error {
	var err error
	if codexExpiry != nil {
		err = h.recordTokens(m, credential.Codex, []string{*codexExpiry}, nil)
	}
	if err == nil && m.GitHubApp != "" {
		err = h.recordTokens(m, credential.GitHub, expiryTimes(github), nil)
	}
	if err == nil && linearExpiry != "" {
		err = h.recordTokens(m, credential.Linear, []string{linearExpiry}, nil)
	}
	if err == nil {
		return nil
	}
	return protocol.New(protocol.StartFailed, tokensFailed(m).Message)
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

func (h Host) agentExec(ctx context.Context, d Daemon, c string) func(argv []string, stdin []byte) error {
	return func(argv []string, stdin []byte) error {
		_, err := d.Runner.Run(ctx, append([]string{"exec", "--interactive", "--user", "agent", c}, argv...), stdin)
		return err
	}
}

// agentRead runs argv as agent in container c on the context it is given, returning its stdout: close's read of the
// token files.
func (h Host) agentRead(d Daemon, c string) func(ctx context.Context, argv []string) ([]byte, error) {
	return func(ctx context.Context, argv []string) ([]byte, error) {
		return d.Runner.Run(ctx, asAgent(c, argv...), nil)
	}
}

// rollback deletes c on a fresh context so a signal or cancelled caller still cleans up.
func (h Host) rollback(ctx context.Context, d Daemon, c string, e *protocol.Error) {
	rctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 30*time.Second)
	defer cancel()
	removed, err := h.remove(rctx, d, c)
	switch {
	case err != nil:
		e.Cleanup, e.CleanupError, e.Container = protocol.Failed, docker.Diagnostic(err), c
	case removed:
		e.Cleanup = protocol.Removed
	default:
		e.Cleanup = protocol.NotNeeded
	}
}
