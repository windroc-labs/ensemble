package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/signal"
	"os/user"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unicode/utf8"

	"ensemble/broker/internal/audit"
	"ensemble/broker/internal/broker"
	"ensemble/broker/internal/codex"
	"ensemble/broker/internal/credential"
	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/github"
	"ensemble/broker/internal/launcher"
	"ensemble/broker/internal/protocol"
)

const usage = "usage: ensemble-broker version | install-codex VERSION | update-codex VERSION | login codex | login claude [NAME] | login github ensemble-broker-bot [--key-file PEM --app-id ID] | login opencode PROVIDER [--auth-file FILE] | login env NAME | (login claude, opencode and env also take --expires YYYY-MM-DD; claude --issued YYYY-MM-DD or --fresh-token instead) | build --ensemble DIR [--base-image IMAGE] [--plugin NAME@OWNER/REPO#COMMIT[::MARKETPLACE/REPO#COMMIT]]... [--image-plugin NAME]... [--max-age SECONDS] | start --manifest FILE | lock-query --lock NAME | maintain --container NAME --manifest FILE [--renew] [--delivered KEY]... | close --container NAME | status | probe-provider --provider NAME --base-url URL | workspace-rm --volume NAME [--force] | check-clients | login linear | linear-maintain (public identity JSON on stdin) | retrust"

// replaced answers a supervisor started before protocol 29, which still calls refresh and notices.
const replaced = "Broker protocol 29 replaced refresh and notices with maintain; restart this session (close and resume it) to renew its credentials."

var containerPattern = regexp.MustCompile(`^ensemble-[A-Za-z0-9_-]+-[0-9a-f]{32}$`)
var secondsPattern = regexp.MustCompile(`^[0-9]{1,9}$`)
var volumePattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$`)

// step runs a command; progress is the broker's stderr, for build's Docker progress and start's phase times.
type step func(ctx context.Context, k keys, arg string, progress io.Writer) (any, error)

// keys is the login keychain every command but version and install-codex reaches, chosen by run: interactive, its
// dialogs and writes allowed to ask, only for a login or retrust; every other command fails rather than ask.
type keys = broker.SystemKeychain

type app struct {
	keychain      func(interactive bool) (keys, error)
	legacy        func() ([]string, error)                                    // old-name items the one-time rename left (legacy.go)
	linear        func(ctx context.Context, k keys, enroll bool) (any, error) // bounded identity JSON on stdin
	build         func(ctx context.Context, k keys, pkg string, spec launcher.BuildSpec, maxAge time.Duration, progress io.Writer) (any, error)
	start, close  step
	maintain      func(ctx context.Context, k keys, container, manifest string, delivered []string, renew bool) (any, error)
	status        func(ctx context.Context, k keys) (any, error)
	lockQuery     func(ctx context.Context, k keys, name string) (any, error)
	probeOpencode func(ctx context.Context, k keys, provider, base string) (any, error)
	workspaceRm   func(ctx context.Context, k keys, volume string, force bool) (any, error)
	login         func(k keys, account string, out io.Writer) error                                // login claude [NAME]
	loginGitHub   func(ctx context.Context, k keys, app string, key *keyFile, out io.Writer) error // key: nil without --key-file
	loginCodex    func(ctx context.Context, k keys, out io.Writer) error
	loginOpencode func(k keys, provider, authFile string, out io.Writer) error
	loginEnv      func(k keys, name string, out io.Writer) error // login env NAME: the value on stdin
	// a login claude, opencode or env with an expiry option; authFile: opencode's --auth-file, "" without
	dated        func(k keys, kind credential.Kind, name, authFile string, reg credential.Registration, out io.Writer) error
	retrust      func(k keys, out io.Writer) error
	installCodex func(context.Context, string, bool) (any, error)
	checkClients func(ctx context.Context, k keys, progress io.Writer) (any, error)
}

// keyFile is login github's --key-file content and --app-id.
type keyFile struct {
	id  string
	pem []byte
}

// maxKeyFile caps --key-file: a 4096-bit PEM key is about 3.3 KB.
const maxKeyFile = 16384

// readKeyFile reads the --key-file PEM, at most maxKeyFile bytes; errors name only the path.
func readKeyFile(path string) ([]byte, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, fmt.Errorf("reading the App private key failed: %w", err)
	}
	defer f.Close()
	data, err := io.ReadAll(io.LimitReader(f, maxKeyFile+1))
	switch {
	case err != nil:
		clear(data)
		return nil, fmt.Errorf("reading the App private key failed: %w", err)
	case len(data) > maxKeyFile:
		clear(data)
		return nil, fmt.Errorf("%s: more than %d bytes", path, maxKeyFile)
	}
	return data, nil
}

// readOpencodeAuth reads only provider's API entry. Errors never include JSON contents or decoder details.
func readOpencodeAuth(path, provider string) ([]byte, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, errors.New("cannot read OpenCode auth.json")
	}
	defer f.Close()
	const maxAuthFile = 1 << 20
	data, err := io.ReadAll(io.LimitReader(f, maxAuthFile+1))
	defer clear(data)
	if err != nil || len(data) > maxAuthFile {
		return nil, errors.New("cannot read OpenCode auth.json (limit 1 MiB)")
	}
	var entries map[string]json.RawMessage
	err = json.Unmarshal(data, &entries)
	defer func() {
		for _, entry := range entries {
			clear(entry)
		}
	}()
	if err != nil {
		return nil, errors.New("OpenCode auth.json is not a JSON object")
	}
	var entry struct {
		Type string `json:"type"`
		Key  string `json:"key"`
	}
	if json.Unmarshal(entries[provider], &entry) != nil || entry.Type != "api" || entry.Key == "" {
		return nil, fmt.Errorf("OpenCode auth.json has no API key for provider %s", provider)
	}
	return []byte(entry.Key), nil
}

// readEnvValue is all of r, a pipe or file carrying login env's value verbatim, at most launcher.MaxSecretBytes bytes,
// read into one buffer that is erased unless returned.
func readEnvValue(r io.Reader) ([]byte, error) {
	buf := make([]byte, launcher.MaxSecretBytes+1)
	n, err := io.ReadFull(r, buf)
	switch {
	case errors.Is(err, io.EOF), errors.Is(err, io.ErrUnexpectedEOF):
		return buf[:n], nil
	case err != nil:
		clear(buf)
		return nil, err
	}
	clear(buf)
	return nil, fmt.Errorf("the value must be 1 to %d bytes", launcher.MaxSecretBytes)
}

func main() {
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	code := run(ctx, os.Args[1:], os.Stdout, os.Stderr, system())
	cancel()
	os.Exit(code)
}

func accountHome() (string, error) {
	u, err := user.Current()
	if err != nil || !strings.HasPrefix(u.HomeDir, "/") {
		return "", errors.Join(errors.New("cannot resolve system account home"), err)
	}
	return u.HomeDir, nil
}

func isPath(s string) bool {
	return strings.HasPrefix(s, "/") && utf8.ValidString(s) && !strings.ContainsFunc(s, func(r rune) bool { return r < 0x20 || r == 0x7f })
}

func run(ctx context.Context, args []string, stdout, stderr io.Writer, a app) (code int) {
	if len(args) == 1 && args[0] == "linear-maintain" || len(args) == 2 && args[0] == "login" && args[1] == "linear" {
		if err := a.legacyError(); err != nil {
			fmt.Fprintln(stderr, "ensemble-broker:", err)
			return 1
		}
		k, err := a.keychain(args[0] == "login")
		var result any
		if err == nil {
			result, err = a.linear(ctx, k, args[0] == "login")
		}
		switch {
		case errors.Is(err, broker.ErrLinearStale):
			fmt.Fprintln(stderr, "ensemble-broker:", broker.ErrLinearStale)
			return 1
		case err != nil:
			fmt.Fprintln(stderr, "ensemble-broker: Linear command failed; check enrollment and broker installation, then rerun")
			return 1
		}
		return emit(stdout, result)
	}
	command := "unknown"
	if len(args) > 0 {
		switch args[0] {
		case "install-codex", "update-codex":
			if len(args) == 2 && codex.ValidVersion(args[1]) {
				err := a.legacyError()
				var result any
				if err == nil {
					result, err = a.installCodex(ctx, args[1], args[0] == "update-codex")
				}
				if err != nil {
					fmt.Fprintln(stderr, "ensemble-broker:", err)
					return 1
				}
				return emit(stdout, result)
			}
		case "version", "build", "start", "maintain", "close", "status", "workspace-rm", "probe-provider", "lock-query", "check-clients":
			command = args[0]
		case "login", "retrust":
			var login func(keys) error
			switch {
			case len(args) == 1 && args[0] == "retrust":
				login = func(k keys) error { return a.retrust(k, stdout) }
			case args[0] != "login":
			case len(args) == 2 && args[1] == "codex":
				login = func(k keys) error { return a.loginCodex(ctx, k, stdout) }
			case len(args) == 2 && args[1] == "claude":
				login = func(k keys) error { return a.login(k, launcher.DefaultClaudeAccount, stdout) }
			case len(args) == 3 && args[1] == "claude" && launcher.ValidClaudeAccount(args[2]):
				login = func(k keys) error { return a.login(k, args[2], stdout) }
			case len(args) == 3 && args[1] == "github" && args[2] == broker.AppKeyService:
				login = func(k keys) error { return a.loginGitHub(ctx, k, args[2], nil, stdout) }
			case len(args) == 7 && args[1] == "github" && args[2] == broker.AppKeyService && args[3] == "--key-file" && args[5] == "--app-id":
				login = func(k keys) error {
					pem, err := readKeyFile(args[4])
					if err != nil {
						return err
					}
					defer clear(pem)
					return a.loginGitHub(ctx, k, args[2], &keyFile{id: args[6], pem: pem}, stdout)
				}
			case len(args) == 3 && args[1] == "opencode" && launcher.ValidProvider(args[2]):
				login = func(k keys) error { return a.loginOpencode(k, args[2], "", stdout) }
			case len(args) == 5 && args[1] == "opencode" && launcher.ValidProvider(args[2]) && args[3] == "--auth-file" && isPath(args[4]):
				login = func(k keys) error { return a.loginOpencode(k, args[2], args[4], stdout) }
			case len(args) == 3 && args[1] == "env" && launcher.ValidEnvName(args[2]):
				login = func(k keys) error { return a.loginEnv(k, args[2], stdout) }
			default:
				login = datedLogin(args, a, stdout)
			}
			if login != nil {
				err := a.legacyError()
				var k keys
				if err == nil {
					k, err = a.keychain(true)
				}
				if err == nil {
					err = login(k)
				}
				if err != nil {
					fmt.Fprintln(stderr, "ensemble-broker:", err)
					if errors.Is(err, broker.ErrInvalidOwnerEdit) {
						return protocol.Exit(protocol.KeychainFailed)
					}
					return 1
				}
				return 0
			}
		}
	}
	defer func() {
		if r := recover(); r != nil {
			code = fail(stderr, command, protocol.New(protocol.InternalError, "Unexpected broker failure."))
		}
	}()
	var s step
	var arg string
	switch {
	case command == "version" && len(args) == 1:
		return emit(stdout, map[string]string{"protocol_version": protocol.Version})
	case command == "build" && len(args) >= 3 && args[1] == "--ensemble" && isPath(args[2]) && buildOptions(args[3:]) != nil:
		o := buildOptions(args[3:])
		s, arg = func(ctx context.Context, k keys, pkg string, progress io.Writer) (any, error) {
			return a.build(ctx, k, pkg, o.spec, o.maxAge, progress)
		}, args[2]
	case command == "start" && len(args) == 3 && args[1] == "--manifest" && isPath(args[2]):
		s, arg = a.start, args[2]
	case command == "maintain" && len(args) >= 5 && args[1] == "--container" && containerPattern.MatchString(args[2]) &&
		args[3] == "--manifest" && isPath(args[4]) && maintainOptions(args[5:]) != nil:
		manifest, o := args[4], maintainOptions(args[5:])
		s, arg = func(ctx context.Context, k keys, container string, _ io.Writer) (any, error) {
			return a.maintain(ctx, k, container, manifest, o.delivered, o.renew)
		}, args[2]
	case command == "status" && len(args) == 1:
		s = func(ctx context.Context, k keys, _ string, _ io.Writer) (any, error) {
			return a.status(ctx, k)
		}
	case command == "lock-query" && len(args) == 3 && args[1] == "--lock" && launcher.ValidLock(args[2]):
		s, arg = func(ctx context.Context, k keys, lock string, _ io.Writer) (any, error) {
			return a.lockQuery(ctx, k, lock)
		}, args[2]
	case command == "probe-provider" && len(args) == 5 && args[1] == "--provider" && launcher.ValidProvider(args[2]) && args[3] == "--base-url" && broker.ValidOpencodeURL(args[4]):
		s = func(ctx context.Context, k keys, _ string, _ io.Writer) (any, error) {
			return a.probeOpencode(ctx, k, args[2], args[4])
		}
	case command == "close" && len(args) == 3 && args[1] == "--container" && containerPattern.MatchString(args[2]):
		s, arg = a.close, args[2]
	case command == "workspace-rm" && (len(args) == 3 || len(args) == 4 && args[3] == "--force") && args[1] == "--volume" &&
		volumePattern.MatchString(args[2]):
		force := len(args) == 4
		s, arg = func(ctx context.Context, k keys, volume string, _ io.Writer) (any, error) {
			return a.workspaceRm(ctx, k, volume, force)
		}, args[2]
	case command == "check-clients" && len(args) == 1:
		s = func(ctx context.Context, k keys, _ string, progress io.Writer) (any, error) {
			return a.checkClients(ctx, k, progress)
		}
	case len(args) > 0 && (args[0] == "refresh" || args[0] == "notices"):
		return fail(stderr, command, protocol.New(protocol.Usage, replaced))
	default:
		return fail(stderr, command, protocol.New(protocol.Usage, usage))
	}
	if err := a.legacyError(); err != nil {
		return fail(stderr, command, protocol.New(protocol.KeychainFailed, err.Error()))
	}
	k, err := a.keychain(false)
	var result any
	if err == nil {
		result, err = s(ctx, k, arg, stderr)
	}
	if err != nil {
		var e *protocol.Error
		if !errors.As(err, &e) {
			e = protocol.New(protocol.InternalError, "Unexpected broker failure.")
		}
		return fail(stderr, command, e)
	}
	return emit(stdout, result)
}

type buildArgs struct {
	spec   launcher.BuildSpec
	maxAge time.Duration
}

// maxDelivered caps maintain's --delivered keys; a container holds at most a few dozen static secrets.
const maxDelivered = 64

type maintainArgs struct {
	delivered []string
	renew     bool
}

// maintainOptions parses maintain's --renew (at most once) and --delivered KEY pairs, in any order; nil for anything
// else.
func maintainOptions(args []string) *maintainArgs {
	o := &maintainArgs{delivered: []string{}}
	for len(args) > 0 {
		switch {
		case args[0] == "--renew" && !o.renew:
			o.renew, args = true, args[1:]
		case len(args) >= 2 && args[0] == "--delivered" && credential.NoticeKeyPattern.MatchString(args[1]):
			o.delivered, args = append(o.delivered, args[1]), args[2:]
		default:
			return nil
		}
	}
	if len(o.delivered) > maxDelivered {
		return nil
	}
	return o
}

// buildOptions parses build's [--base-image IMAGE] [--plugin NAME@OWNER/REPO#COMMIT[::MARKETPLACE/REPO#COMMIT]]...
// [--image-plugin NAME]... [--max-age SECONDS], else nil.
func buildOptions(args []string) *buildArgs {
	o := &buildArgs{maxAge: -1}
	if len(args) >= 2 && args[0] == "--base-image" && launcher.ValidBaseImage(args[1]) {
		o.spec.BaseImage, args = args[1], args[2:]
	}
	for len(args) >= 2 && args[0] == "--plugin" && launcher.PluginSourcePattern.MatchString(args[1]) {
		o.spec.Plugins, args = append(o.spec.Plugins, args[1]), args[2:]
	}
	for len(args) >= 2 && args[0] == "--image-plugin" && launcher.PluginNamePattern.MatchString(args[1]) {
		o.spec.ImagePlugins, args = append(o.spec.ImagePlugins, args[1]), args[2:]
	}
	if len(args) == 2 && args[0] == "--max-age" && secondsPattern.MatchString(args[1]) {
		n, _ := strconv.Atoi(args[1])
		o.maxAge, args = time.Duration(n)*time.Second, nil
	}
	if len(args) != 0 {
		return nil
	}
	return o
}

func emit(stdout io.Writer, v any) int {
	data, err := json.Marshal(v)
	if err != nil {
		panic(err)
	}
	if _, err := stdout.Write(append(data, '\n')); err != nil {
		return protocol.Exit(protocol.InternalError)
	}
	return 0
}

func fail(stderr io.Writer, command string, e *protocol.Error) int {
	_, _ = stderr.Write(protocol.Envelope(command, e))
	return protocol.Exit(e.Code)
}

// recorder is command's audit recorder, writing to the account's log; container "": not known yet.
func recorder(home, command, container string) *audit.Recorder {
	return &audit.Recorder{Command: command, Container: container, Now: time.Now, Log: audit.Log{Path: audit.Path(home)}}
}

// withHost runs f on the launcher wired to the fixed Docker Desktop CLI and the keychain k, in k's account home, then
// writes command's audit lines; docker build's stderr and the launcher's notes go to progress.
func withHost(command, container string, k keys, progress io.Writer, f func(launcher.Host) (any, error)) (result any, err error) {
	home := k.Home
	rec := recorder(home, command, container)
	return withAudit(rec, func() (any, error) {
		d, clean, err := docker.New(home)
		if err != nil {
			return nil, err
		}
		defer clean()
		d.Progress = progress
		h := launcher.NewHost(home, d.Endpoint(), d, broker.Credentials{Keychain: k}, tokens(k, rec), rec)
		h.Codex = broker.NewCodex(home, k, nil)
		h.Opencode = broker.OpencodeKeys{Keychain: k}
		h.Env = broker.EnvKeys{Keychain: k}
		h.Static = staticStore(home)
		h.Progress = progress
		h.Lock = broker.FileLock(filepath.Join(home, ".ensemble/docker/images.lock"))
		return f(h)
	})
}

// tokens is the broker's GitHub App token store in k, the account's login keychain, reporting to rec (nil: none).
func tokens(k keys, rec *audit.Recorder) broker.GitHub {
	return broker.GitHub{Keychain: k, Apps: k, API: github.New(), Lock: broker.FileLock(filepath.Join(k.Home, ".ensemble/docker/tokens.lock")),
		Audit: rec}
}

func system() app {
	return app{
		keychain: func(interactive bool) (keys, error) {
			home, err := accountHome()
			if err != nil {
				return keys{}, err
			}
			k := keys{Home: home}
			if interactive {
				k = k.Interactive()
			}
			return k, nil
		},
		legacy: systemLegacy,
		linear: linearSystem,
		lockQuery: func(ctx context.Context, k keys, lock string) (any, error) {
			return withHost("lock-query", "", k, nil, func(h launcher.Host) (any, error) { return h.LockQuery(ctx, lock) })
		},
		probeOpencode: func(ctx context.Context, k keys, provider, base string) (any, error) {
			available, err := (broker.OpencodeKeys{Keychain: k}).Probe(ctx, provider, base, nil)
			return map[string]bool{"available": available}, err
		},
		build: func(ctx context.Context, k keys, pkg string, spec launcher.BuildSpec, maxAge time.Duration, progress io.Writer) (any, error) {
			return withHost("build", "", k, progress, func(h launcher.Host) (any, error) { return h.Build(ctx, pkg, spec, maxAge) })
		},
		checkClients: func(ctx context.Context, k keys, progress io.Writer) (any, error) {
			return withHost("check-clients", "", k, progress, func(h launcher.Host) (any, error) { return h.CheckClients(ctx) })
		},
		start: func(ctx context.Context, k keys, manifest string, progress io.Writer) (any, error) {
			return withHost("start", "", k, progress, func(h launcher.Host) (any, error) { return h.Start(ctx, manifest) })
		},
		status: func(ctx context.Context, k keys) (any, error) {
			codexStatus := broker.NewCodex(k.Home, k, nil).Status(ctx)
			usable := (broker.Credentials{Keychain: k}).Usable("default")
			static, err := launcher.Host{Static: staticStore(k.Home)}.StaticStatus(map[string]bool{credential.StaticRef(credential.Claude, "default"): usable})
			if err != nil {
				return nil, err
			}
			return launcher.StatusResult{Codex: &codexStatus, Claude: map[string]bool{"default": usable}, Static: static}, nil
		},
		maintain: func(ctx context.Context, k keys, container, manifest string, delivered []string, renew bool) (any, error) {
			return withHost("maintain", container, k, nil, func(h launcher.Host) (any, error) {
				return h.Maintain(ctx, container, manifest, delivered, renew)
			})
		},
		close: func(ctx context.Context, k keys, container string, _ io.Writer) (any, error) {
			return withHost("close", container, k, nil, func(h launcher.Host) (any, error) { return h.Close(ctx, container) })
		},
		workspaceRm: func(ctx context.Context, k keys, volume string, force bool) (any, error) {
			return withHost("workspace-rm", "", k, nil, func(h launcher.Host) (any, error) { return h.WorkspaceRm(ctx, volume, force) })
		},
		login: func(k keys, account string, out io.Writer) error {
			return loginClaude(k, account, credential.Registration{}, out)
		},
		loginGitHub: func(ctx context.Context, k keys, app string, key *keyFile, out io.Writer) error {
			if err := broker.CheckInstalled(); err != nil {
				return err
			}
			if key != nil {
				if err := broker.StoreAppKey(app, key.id, key.pem, k); err != nil {
					return err
				}
			}
			if err := broker.LoginGitHub(app, k, func() ([]byte, error) {
				fmt.Fprint(os.Stderr, "Login keychain password, to make "+app+" trust only the broker (hidden): ")
				defer fmt.Fprintln(os.Stderr)
				return broker.ReadHidden()
			}, out); err != nil {
				return err
			}
			return tokens(k, nil).SaveUser(ctx, out)
		},
		loginOpencode: func(k keys, provider, authFile string, out io.Writer) error {
			return loginOpencode(k, provider, authFile, credential.Registration{}, out)
		},
		loginEnv: func(k keys, name string, out io.Writer) error {
			return loginEnv(k, name, credential.Registration{}, out)
		},
		dated: func(k keys, kind credential.Kind, name, authFile string, reg credential.Registration, out io.Writer) error {
			switch kind {
			case credential.Claude:
				return loginClaude(k, name, reg, out)
			case credential.Opencode:
				return loginOpencode(k, name, authFile, reg, out)
			}
			return loginEnv(k, name, reg, out)
		},
		retrust: func(k keys, out io.Writer) error {
			if err := broker.CheckInstalled(); err != nil {
				return err
			}
			return broker.Retrust(k, func() ([]byte, error) {
				fmt.Fprint(os.Stderr, "Login keychain password, to make the broker's keychain items trust this broker (hidden): ")
				defer fmt.Fprintln(os.Stderr)
				return broker.ReadHidden()
			}, os.Stderr, out)
		},
		installCodex: func(ctx context.Context, version string, update bool) (any, error) {
			if err := broker.CheckInstalled(); err != nil {
				return nil, err
			}
			return codex.New().Install(ctx, version, update)
		},
		loginCodex: func(ctx context.Context, k keys, out io.Writer) error {
			if err := broker.CheckInstalled(); err != nil {
				return err
			}
			return broker.NewCodex(k.Home, k, func() ([]byte, error) {
				fmt.Fprint(os.Stderr, "Login keychain password, to make Codex trust only the broker (hidden): ")
				defer fmt.Fprintln(os.Stderr)
				return broker.ReadHidden()
			}).Login(ctx, out)
		},
	}
}

// loginClaude is login claude: account's setup-token in k, registered with reg.
func loginClaude(k keys, account string, reg credential.Registration, out io.Writer) error {
	home := k.Home
	if err := broker.CheckInstalled(); err != nil {
		return err
	}
	read, hidden := stdinReaders(os.Stdin, "Claude account "+account+" setup-token (hidden): ", true)
	return newStaticLogin(home).register(credential.StaticRef(credential.Claude, account), reg, out, os.Stderr, func() error {
		return broker.Login(account, read, func() ([]byte, error) {
			fmt.Fprint(os.Stderr, "Login keychain password, to make Claude trust only the broker (hidden): ")
			defer fmt.Fprintln(os.Stderr)
			return hidden()
		}, k, os.Stderr, out)
	})
}

// loginOpencode is login opencode: provider's API key in k, from authFile ("": the terminal), registered with reg.
func loginOpencode(k keys, provider, authFile string, reg credential.Registration, out io.Writer) error {
	home := k.Home
	if err := broker.CheckInstalled(); err != nil {
		return err
	}
	return newStaticLogin(home).register(credential.StaticRef(credential.Opencode, provider), reg, out, os.Stderr, func() error {
		return broker.LoginOpencode(provider, func() ([]byte, error) {
			if authFile != "" {
				return readOpencodeAuth(authFile, provider)
			}
			fmt.Fprint(os.Stderr, "OpenCode provider "+provider+" API key (hidden): ")
			defer fmt.Fprintln(os.Stderr)
			return broker.ReadHidden()
		}, func() ([]byte, error) {
			fmt.Fprint(os.Stderr, "Login keychain password, to make the "+provider+" API key trust only the broker (hidden): ")
			defer fmt.Fprintln(os.Stderr)
			return broker.ReadHidden()
		}, k, out)
	})
}

// loginEnv is login env: name's value in k, registered with reg.
func loginEnv(k keys, name string, reg credential.Registration, out io.Writer) error {
	home := k.Home
	if err := broker.CheckInstalled(); err != nil {
		return err
	}
	read, hidden := stdinReaders(os.Stdin, "Environment secret "+name+" (hidden): ", false)
	return newStaticLogin(home).register(credential.StaticRef(credential.Env, name), reg, out, os.Stderr, func() error {
		return broker.LoginEnv(name, read, func() ([]byte, error) {
			fmt.Fprint(os.Stderr, "Login keychain password, to make ensemble-broker-env/"+name+" trust only the broker (hidden): ")
			defer fmt.Fprintln(os.Stderr)
			return hidden()
		}, k, out)
	})
}
