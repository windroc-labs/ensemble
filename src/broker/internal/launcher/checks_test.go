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
	cmd := exec.Command(node, "-", root, "good", "misnamed", "badjson", "array", "nomanifest", "notdir", "lockedfile", "lockeddir", "absent")
	cmd.Stdin = strings.NewReader(string(script))
	out, err := cmd.Output()
	if err != nil {
		t.Fatal(err)
	}
	want := "misnamed misnamed\nbadjson misnamed\narray misnamed\nnomanifest missing\nnotdir missing\nlockedfile unreadable\nlockeddir unreadable\nabsent missing\n"
	if string(out) != want {
		t.Fatalf("got %q", out)
	}
}
