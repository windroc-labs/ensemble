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
	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/github"
	"ensemble/broker/internal/launcher"
	"ensemble/broker/internal/protocol"
)

const usage = "usage: ensemble-broker version | install-codex VERSION | update-codex VERSION | login codex | login claude [NAME] | login github ensemble-bot [--key-file PEM --app-id ID] | login opencode PROVIDER [--auth-file FILE] | build --ensemble DIR [--plugin NAME@OWNER/REPO#COMMIT[::MARKETPLACE/REPO#COMMIT]]... [--image-plugin NAME]... [--max-age SECONDS] | start --manifest FILE | refresh --container NAME --manifest FILE | close --container NAME | status | probe-provider --provider NAME --base-url URL | workspace-rm --volume NAME [--force]"

var containerPattern = regexp.MustCompile(`^ensemble-[A-Za-z0-9_-]+-[0-9a-f]{32}$`)
var secondsPattern = regexp.MustCompile(`^[0-9]{1,9}$`)
var volumePattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$`)

// step runs a command; progress is the broker's stderr, for build's Docker progress and start's phase times.
type step func(ctx context.Context, arg string, progress io.Writer) (any, error)

type app struct {
	build         func(ctx context.Context, pkg string, spec launcher.BuildSpec, maxAge time.Duration, progress io.Writer) (any, error)
	start, close  step
	refresh       func(ctx context.Context, container, manifest string) (any, error)
	status        func(ctx context.Context) (any, error)
	probeOpencode func(ctx context.Context, provider, base string) (any, error)
	workspaceRm   func(ctx context.Context, volume string, force bool) (any, error)
	login         func(account string, out io.Writer) error                                // login claude [NAME]
	loginGitHub   func(ctx context.Context, app string, key *keyFile, out io.Writer) error // key: nil without --key-file
	loginCodex    func(context.Context, io.Writer) error
	loginOpencode func(provider, authFile string, out io.Writer) error
	installCodex  func(context.Context, string, bool) (any, error)
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
	command := "unknown"
	if len(args) > 0 {
		switch args[0] {
		case "install-codex", "update-codex":
			if len(args) == 2 && codex.ValidVersion(args[1]) {
				result, err := a.installCodex(ctx, args[1], args[0] == "update-codex")
				if err != nil {
					fmt.Fprintln(stderr, "ensemble-broker:", err)
					return 1
				}
				return emit(stdout, result)
			}
		case "version", "build", "start", "refresh", "close", "status", "workspace-rm", "probe-provider":
			command = args[0]
		case "login":
			var login func() error
			switch {
			case len(args) == 2 && args[1] == "codex":
				login = func() error { return a.loginCodex(ctx, stdout) }
			case len(args) == 2 && args[1] == "claude":
				login = func() error { return a.login(launcher.DefaultClaudeAccount, stdout) }
			case len(args) == 3 && args[1] == "claude" && launcher.ValidClaudeAccount(args[2]):
				login = func() error { return a.login(args[2], stdout) }
			case len(args) == 3 && args[1] == "github" && args[2] == broker.AppKeyService:
				login = func() error { return a.loginGitHub(ctx, args[2], nil, stdout) }
			case len(args) == 7 && args[1] == "github" && args[2] == broker.AppKeyService && args[3] == "--key-file" && args[5] == "--app-id":
				login = func() error {
					pem, err := readKeyFile(args[4])
					if err != nil {
						return err
					}
					defer clear(pem)
					return a.loginGitHub(ctx, args[2], &keyFile{id: args[6], pem: pem}, stdout)
				}
			case len(args) == 3 && args[1] == "opencode" && launcher.ValidProvider(args[2]):
				login = func() error { return a.loginOpencode(args[2], "", stdout) }
			case len(args) == 5 && args[1] == "opencode" && launcher.ValidProvider(args[2]) && args[3] == "--auth-file" && isPath(args[4]):
				login = func() error { return a.loginOpencode(args[2], args[4], stdout) }
			}
			if login != nil {
				if err := login(); err != nil {
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
		s, arg = func(ctx context.Context, pkg string, progress io.Writer) (any, error) {
			return a.build(ctx, pkg, o.spec, o.maxAge, progress)
		}, args[2]
	case command == "start" && len(args) == 3 && args[1] == "--manifest" && isPath(args[2]):
		s, arg = a.start, args[2]
	case command == "refresh" && len(args) == 5 && args[1] == "--container" && containerPattern.MatchString(args[2]) &&
		args[3] == "--manifest" && isPath(args[4]):
		manifest := args[4]
		s, arg = func(ctx context.Context, container string, _ io.Writer) (any, error) {
			return a.refresh(ctx, container, manifest)
		}, args[2]
	case command == "status" && len(args) == 1:
		s = func(ctx context.Context, _ string, _ io.Writer) (any, error) { return a.status(ctx) }
	case command == "probe-provider" && len(args) == 5 && args[1] == "--provider" && launcher.ValidProvider(args[2]) && args[3] == "--base-url" && broker.ValidOpencodeURL(args[4]):
		s = func(ctx context.Context, _ string, _ io.Writer) (any, error) {
			return a.probeOpencode(ctx, args[2], args[4])
		}
	case command == "close" && len(args) == 3 && args[1] == "--container" && containerPattern.MatchString(args[2]):
		s, arg = a.close, args[2]
	case command == "workspace-rm" && (len(args) == 3 || len(args) == 4 && args[3] == "--force") && args[1] == "--volume" &&
		volumePattern.MatchString(args[2]):
		force := len(args) == 4
		s, arg = func(ctx context.Context, volume string, _ io.Writer) (any, error) {
			return a.workspaceRm(ctx, volume, force)
		}, args[2]
	default:
		return fail(stderr, command, protocol.New(protocol.Usage, usage))
	}
	result, err := s(ctx, arg, stderr)
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

// buildOptions parses build's [--plugin NAME@OWNER/REPO#COMMIT[::MARKETPLACE/REPO#COMMIT]]... [--image-plugin NAME]...
// [--max-age SECONDS], else nil.
func buildOptions(args []string) *buildArgs {
	o := &buildArgs{maxAge: -1}
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

// withHost runs f on the launcher wired to the fixed Docker Desktop CLI, then writes command's audit lines; docker
// build's stderr and the launcher's notes go to progress.
func withHost(command, container string, progress io.Writer, f func(launcher.Host) (any, error)) (result any, err error) {
	home, err := accountHome()
	if err != nil {
		return nil, err
	}
	rec := recorder(home, command, container)
	defer func() { rec.Flush(err) }()
	d, clean, err := docker.New(home)
	if err != nil {
		return nil, err
	}
	defer clean()
	d.Progress = progress
	h := launcher.NewHost(home, d.Endpoint(), d, broker.Credentials{Keychain: broker.SystemKeychain{Home: home}}, tokens(home, rec), rec)
	h.Codex = broker.NewCodex(home, nil)
	h.Opencode = broker.OpencodeKeys{Keychain: broker.SystemKeychain{Home: home}}
	h.Progress = progress
	h.Lock = broker.FileLock(filepath.Join(home, ".ensemble/docker/images.lock"))
	return f(h)
}

// tokens is the broker's GitHub App token store in the account's login keychain, reporting to rec (nil: none).
func tokens(home string, rec *audit.Recorder) broker.GitHub {
	k := broker.SystemKeychain{Home: home}
	return broker.GitHub{Keychain: k, Apps: k, API: github.New(), Lock: broker.FileLock(filepath.Join(home, ".ensemble/docker/tokens.lock")),
		Audit: rec}
}

func system() app {
	return app{
		probeOpencode: func(ctx context.Context, provider, base string) (any, error) {
			home, err := accountHome()
			if err != nil {
				return nil, err
			}
			available, err := (broker.OpencodeKeys{Keychain: broker.SystemKeychain{Home: home}}).Probe(ctx, provider, base, nil)
			return map[string]bool{"available": available}, err
		},
		build: func(ctx context.Context, pkg string, spec launcher.BuildSpec, maxAge time.Duration, progress io.Writer) (any, error) {
			return withHost("build", "", progress, func(h launcher.Host) (any, error) { return h.Build(ctx, pkg, spec, maxAge) })
		},
		start: func(ctx context.Context, manifest string, progress io.Writer) (any, error) {
			return withHost("start", "", progress, func(h launcher.Host) (any, error) { return h.Start(ctx, manifest) })
		},
		refresh: func(ctx context.Context, container, manifest string) (any, error) {
			return withHost("refresh", container, nil, func(h launcher.Host) (any, error) { return h.Refresh(ctx, container, manifest) })
		},
		status: func(ctx context.Context) (_ any, err error) {
			home, err := accountHome()
			if err != nil {
				return nil, err
			}
			rec := recorder(home, "status", "")
			defer func() { rec.Flush(err) }()
			status, err := launcher.Host{GitHub: tokens(home, rec)}.Status(ctx)
			if err != nil {
				return nil, err
			}
			codexStatus := broker.NewCodex(home, nil).Status(ctx)
			status.Codex = &codexStatus
			credentials := broker.Credentials{Keychain: broker.SystemKeychain{Home: home}}
			status.Claude = map[string]bool{"default": credentials.Usable("default")}
			return status, nil
		},
		close: func(ctx context.Context, container string, _ io.Writer) (any, error) {
			return withHost("close", container, nil, func(h launcher.Host) (any, error) { return h.Close(ctx, container) })
		},
		workspaceRm: func(ctx context.Context, volume string, force bool) (any, error) {
			return withHost("workspace-rm", "", nil, func(h launcher.Host) (any, error) { return h.WorkspaceRm(ctx, volume, force) })
		},
		login: func(account string, out io.Writer) error {
			home, err := accountHome()
			if err != nil {
				return err
			}
			if err := broker.CheckInstalled(); err != nil {
				return err
			}
			return broker.Login(account, func() ([]byte, error) {
				fmt.Fprint(os.Stderr, "Claude account "+account+" setup-token (hidden): ")
				defer fmt.Fprintln(os.Stderr)
				return broker.ReadHidden()
			}, func() ([]byte, error) {
				fmt.Fprint(os.Stderr, "Login keychain password, to make Claude trust only the broker (hidden): ")
				defer fmt.Fprintln(os.Stderr)
				return broker.ReadHidden()
			}, broker.SystemKeychain{Home: home}, os.Stderr, out)
		},
		loginGitHub: func(ctx context.Context, app string, key *keyFile, out io.Writer) error {
			home, err := accountHome()
			if err != nil {
				return err
			}
			if err := broker.CheckInstalled(); err != nil {
				return err
			}
			k := broker.SystemKeychain{Home: home}
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
			return tokens(home, nil).SaveUser(ctx, out)
		},
		loginOpencode: func(provider, authFile string, out io.Writer) error {
			home, err := accountHome()
			if err != nil {
				return err
			}
			if err := broker.CheckInstalled(); err != nil {
				return err
			}
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
			}, broker.SystemKeychain{Home: home}, out)
		},
		installCodex: func(ctx context.Context, version string, update bool) (any, error) {
			if err := broker.CheckInstalled(); err != nil {
				return nil, err
			}
			return codex.New().Install(ctx, version, update)
		},
		loginCodex: func(ctx context.Context, out io.Writer) error {
			home, err := accountHome()
			if err != nil {
				return err
			}
			if err := broker.CheckInstalled(); err != nil {
				return err
			}
			return broker.NewCodex(home, func() ([]byte, error) {
				fmt.Fprint(os.Stderr, "Login keychain password, to make Codex trust only the broker (hidden): ")
				defer fmt.Fprintln(os.Stderr)
				return broker.ReadHidden()
			}).Login(ctx, out)
		},
	}
}
