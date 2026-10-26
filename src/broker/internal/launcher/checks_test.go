package launcher

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func TestPluginCheckScript(t *testing.T) {
	node, err := exec.LookPath("node")
	if err != nil {
		t.Skip("no node")
	}
	if os.Geteuid() == 0 {
		t.Skip("root reads everything")
	}
	script, err := checks.ReadFile("checks/plugin-check.js")
	if err != nil {
		t.Fatal(err)
	}
	root := t.TempDir()
	manifest := func(name, text string) {
		putFile(t, filepath.Join(root, name, ".claude-plugin/plugin.json"), text)
	}
	manifest("good", `{"name":"good","version":"1.0.0"}`)
	putFile(t, filepath.Join(root, "good/commands/hello.md"), "hello")
	manifest("misnamed", `{"name":"other"}`)
	manifest("badjson", `{`)
	manifest("array", `["array"]`)
	if err := os.MkdirAll(filepath.Join(root, "nomanifest/commands"), 0o755); err != nil {
		t.Fatal(err)
	}
	putFile(t, filepath.Join(root, "notdir"), "file")
	manifest("lockedfile", `{"name":"lockedfile"}`)
	putFile(t, filepath.Join(root, "lockedfile/commands/secret.md"), "x")
	manifest("lockeddir", `{"name":"lockeddir"}`)
	putFile(t, filepath.Join(root, "lockeddir/skills/a/SKILL.md"), "x")
	for path, mode := range map[string]os.FileMode{"lockedfile/commands/secret.md": 0, "lockeddir/skills": 0o600} {
		full := filepath.Join(root, path)
		if err := os.Chmod(full, mode); err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = os.Chmod(full, 0o755) })
	}
	// linked is a symlink to a plugin directory holding a file agent cannot read.
	target := t.TempDir()
	putFile(t, filepath.Join(target, ".claude-plugin/plugin.json"), `{"name":"linked"}`)
	putFile(t, filepath.Join(target, "secret.md"), "x")
	if err := os.Chmod(filepath.Join(target, "secret.md"), 0); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(target, filepath.Join(root, "linked")); err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command(node, "-", root, "good", "misnamed", "badjson", "array", "nomanifest", "notdir", "lockedfile", "lockeddir", "linked", "absent")
	cmd.Stdin = strings.NewReader(string(script))
	out, err := cmd.Output()
	if err != nil {
		t.Fatal(err)
	}
	want := "misnamed misnamed\nbadjson misnamed\narray misnamed\nnomanifest missing\nnotdir missing\nlockedfile unreadable\nlockeddir unreadable\nlinked unreadable\nabsent missing\n"
	if string(out) != want {
		t.Fatalf("got %q", out)
	}
}

func TestBaseCheckScript(t *testing.T) {
	sh, err := exec.LookPath("sh")
	if err != nil {
		t.Skip("no sh")
	}
	script, err := checks.ReadFile("checks/base-check")
	if err != nil {
		t.Fatal(err)
	}
	tools := []string{"apt-get", "dpkg", "node", "npm", "tmux", "git", "claude"}
	// id answers -u agent and -g agent with STUB_UID and STUB_GID, failing when STUB_UID is empty (no user agent).
	id := "#!/bin/sh\n[ -n \"$STUB_UID\" ] || exit 1\ncase $1 in -u) echo \"$STUB_UID\" ;; -g) echo \"$STUB_GID\" ;; esac\n"
	check := func(t *testing.T, mode string, present []string, uid, gid string) string {
		t.Helper()
		dir := t.TempDir()
		for _, name := range present {
			body := "#!/bin/sh\nexit 0\n"
			if name == "id" {
				body = id
			}
			if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o755); err != nil {
				t.Fatal(err)
			}
		}
		cmd := exec.Command(sh, "-s", mode, dir)
		cmd.Stdin = strings.NewReader(string(script))
		cmd.Env = []string{"STUB_UID=" + uid, "STUB_GID=" + gid}
		out, err := cmd.Output()
		if err != nil {
			t.Fatalf("%s: %v", mode, err)
		}
		return string(out)
	}
	for name, tc := range map[string]struct {
		present  []string
		uid, gid string
		want     string
	}{
		"agent":      {[]string{"id"}, "1000", "1001", "agent 1000 1001\n"},
		"no agent":   {[]string{"id"}, "", "", "missing agent\n"},
		"root agent": {[]string{"id"}, "0", "1000", "missing agent\n"},
		"root group": {[]string{"id"}, "1000", "0", "missing agent\n"},
		"no id":      {nil, "1000", "1000", "missing agent\n"},
	} {
		t.Run(name, func(t *testing.T) {
			if got := check(t, "agent", tc.present, tc.uid, tc.gid); got != tc.want {
				t.Fatalf("%q", got)
			}
		})
	}
	for name, tc := range map[string]struct {
		present []string
		want    string
	}{
		"all":       {tools, ""},
		"no tmux":   {[]string{"apt-get", "dpkg", "node", "npm", "git", "claude"}, "missing tmux\n"},
		"two short": {[]string{"apt-get", "node", "npm", "tmux", "git"}, "missing dpkg\nmissing claude\n"},
		"none":      {nil, "missing apt-get\nmissing dpkg\nmissing node\nmissing npm\nmissing tmux\nmissing git\nmissing claude\n"},
	} {
		t.Run(name, func(t *testing.T) {
			if got := check(t, "tools", tc.present, "", ""); got != tc.want {
				t.Fatalf("%q", got)
			}
		})
	}
	cmd := exec.Command(sh, "-s", "other", t.TempDir())
	cmd.Stdin = strings.NewReader(string(script))
	if err := cmd.Run(); err == nil {
		t.Fatal("unknown mode succeeded")
	}
}
