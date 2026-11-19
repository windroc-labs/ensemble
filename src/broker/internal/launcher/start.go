package launcher

import (
	"context"
	"errors"
	"fmt"
	"io"
	"maps"
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
				h.removeVolume(ctx, d, volumesOf(m.Name).State, e)
			}
		}()
	}
	lap("checks")
	if created && m.Prefetch != nil {
		if err := h.prefetch(ctx, launch{d, c, m, info}, r, px); err != nil {
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
	result, held, err = h.create(ctx, launch{d, c, m, info}, route, facts, lap)
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
	state := volumesOf(m.Name).State
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

// launch is one container's start on its daemon, from the absence check on.
type launch struct {
	d    Daemon
	c    string
	m    Manifest
	info imageInfo
}

func (l launch) runArgs(route egress, mountFlags []string) []string {
	m := l.m
	// Stdin open for the harness's docker attach; no log driver, so no channel data lands in docker logs.
	args := append([]string{"run", "--detach", "--interactive", "--log-driver", "none", "--name", l.c, "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", "agent"},
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
		args = append(args, "--label", secretsLabel+"="+strings.Join(secretItems(m.Secrets), ","))
	}
	if m.Lock != "" {
		args = append(args, "--restart", "no", "--label", lockLabel+"="+m.Lock, "--label", ownerLabel+"="+m.LockOwner)
	}
	for _, k := range slices.Sorted(maps.Keys(m.Labels)) {
		args = append(args, "--label", k+"="+m.Labels[k])
	}
	return append(append(args, mountFlags...), "--entrypoint", "/usr/bin/tini", l.info.ID, "--", Python, "-I", Guard, "--serve")
}

// boot creates l's container and readies it for its credentials; errTaken: another start made it first.
func (h Host) boot(ctx context.Context, l launch, args []string, lap func(phase string)) (ClientVersions, error) {
	if _, err := l.d.Runner.Run(ctx, args, nil); err != nil {
		if docker.Conflict(err) {
			return ClientVersions{}, errTaken
		}
		return ClientVersions{}, diagnosed(err, protocol.StartFailed, "Creating the container failed.")
	}
	lap("create")
	if l.m.Lock != "" {
		if err := h.lockBusy(ctx, l.d, l.m.Lock, l.c); err != nil {
			return ClientVersions{}, err
		}
	}
	if err := h.bootstrap(ctx, l.d, l.c, l.m); err != nil {
		return ClientVersions{}, err
	}
	lap("bootstrap")
	clients, err := startClients(l.info.Config.Labels)
	if err != nil {
		return ClientVersions{}, err
	}
	lap("clients")
	switch l.m.Client {
	case "codex":
		err = checkCodex(clients.Codex)
	case "opencode":
		err = checkOpencode(clients.Opencode)
	}
	return clients, err
}

// clientLogin is the client's own credential, prepared: finish (nil: nothing held) releases what preparing it holds.
type clientLogin struct {
	message  string
	commands []credential.Command
	expiry   *string // Codex's
	finish   func() error
}

func (h Host) clientLogin(ctx context.Context, m Manifest) (clientLogin, error) {
	switch m.Client {
	case "codex":
		login := clientLogin{message: "Injecting Codex credentials failed."}
		injection, err := h.Codex.Inject(ctx)
		if err != nil {
			return login, err
		}
		login.commands, login.expiry, login.finish = []credential.Command{injection.Command}, &injection.Expiry, injection.Finish
		return login, nil
	case "claude":
		command, err := h.Creds.Inject(m.ClaudeAccount)
		return clientLogin{message: "Injecting the Claude token failed.", commands: []credential.Command{command}}, err
	case "opencode":
		login := clientLogin{message: "Injecting the OpenCode provider API keys failed."}
		if len(m.OpencodeProviders) == 0 {
			return login, nil
		}
		var err error
		login.commands, err = h.Opencode.Inject(m.OpencodeProviders)
		return login, err
	}
	return clientLogin{}, nil
}

func (h Host) secretWrites(l launch, batch *startDelivery, held *holding) error {
	for _, s := range l.m.Secrets {
		message := "Writing " + s.Item + " into the container failed."
		command, err := h.Env.Inject(s)
		if err != nil {
			return injected(err, protocol.StartFailed, message)
		}
		batch.commands = append(batch.commands, command)
		batch.step(message, func() {
			held.secrets = append(held.secrets, s.Item)
			h.Audit.Add(l.c, audit.Credential{Kind: audit.EnvSecret, Item: s.Item, Result: audit.Issued})
		})
	}
	return nil
}

func (h Host) tokenWrites(ctx context.Context, l launch, batch *startDelivery, held *holding, lap func(phase string)) (github []protocol.Expiry, linear string, err error) {
	github = []protocol.Expiry{}
	if l.m.GitHubApp != "" {
		const message = "Injecting the GitHub tokens failed."
		var commands []credential.Command
		if commands, held.github, github, err = h.GitHub.Issue(ctx, l.m.GitHubApp, l.m.GitHubPermissions, l.c); err != nil {
			return nil, "", injected(err, protocol.StartFailed, message)
		}
		batch.commands = append(batch.commands, commands...)
		batch.step(message, nil)
		lap("github")
	}
	if l.m.Linear != nil {
		const message = "Injecting the Linear credentials failed."
		var writes []credential.Command
		if writes, held.linear, linear, err = h.Linear.Issue(ctx, *l.m.Linear, l.c); err != nil {
			return nil, "", injected(err, protocol.StartFailed, message)
		}
		batch.commands = append(batch.commands, writes...)
		batch.step(message, nil)
	}
	return github, linear, nil
}

// create runs every step after the absence check; any error makes Start roll back. held: the credentials in the
// container so far.
func (h Host) create(ctx context.Context, l launch, route egress, facts *audit.StartFacts, lap func(phase string)) (_ StartResult, held holding, retErr error) {
	flags, list := mounts(l.m, l.info.Config.Labels)
	facts.Mounts = list
	clients, err := h.boot(ctx, l, l.runArgs(route, flags), lap)
	if err != nil {
		return StartResult{}, held, err
	}
	var batch startDelivery
	defer batch.erase()
	login, err := h.clientLogin(ctx, l.m)
	if err != nil {
		return StartResult{}, held, injected(err, protocol.StartFailed, login.message)
	}
	if login.finish != nil {
		defer func() {
			if endErr := login.finish(); endErr != nil && retErr == nil {
				retErr = injected(endErr, protocol.StartFailed, login.message)
			}
		}()
	}
	batch.commands = append(batch.commands, login.commands...)
	batch.step(login.message, func() {
		if kind := clientKind(l.m.Client); kind != "" {
			held.client = true
			h.Audit.Add(l.c, audit.Credential{Kind: kind, Result: audit.Issued, ExpiresAt: login.expiry})
		}
	})
	if err := h.secretWrites(l, &batch, &held); err != nil {
		return StartResult{}, held, err
	}
	expiries, linearExpiry, err := h.tokenWrites(ctx, l, &batch, &held, lap)
	if err != nil {
		return StartResult{}, held, err
	}
	if err := h.deliverStart(ctx, l.d, l.c, &batch); err != nil {
		return StartResult{}, held, err
	}
	lap("credentials")
	if err := h.startTokens(l.m, login.expiry, expiries, linearExpiry); err != nil {
		return StartResult{}, held, err
	}
	delivered := TokenRecord{Expires: map[credential.Kind][]string{credential.GitHub: expiryTimes(expiries)}}
	if linearExpiry != "" {
		delivered.Expires[credential.Linear] = []string{linearExpiry}
	}
	if login.expiry != nil {
		delivered.Expires[credential.Codex] = []string{*login.expiry}
	}
	return StartResult{Container: l.c, Endpoint: l.d.Endpoint, ImageDigest: l.info.ID,
			Clients:  Clients{Claude: clients.Claude + " (Claude Code)", Codex: "codex-cli " + clients.Codex, Opencode: clients.Opencode},
			Deadline: deadlineOf(delivered, heldKinds(l.m))},
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
