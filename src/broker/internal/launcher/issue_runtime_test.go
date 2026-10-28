package launcher

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func TestStagedWorkerIssueRuntime(t *testing.T) {
	pkg, err := filepath.Abs("../../../..")
	if err != nil {
		t.Fatal(err)
	}
	e := newBuildEnv(t)
	stage := t.TempDir()
	if _, _, err := e.host().stage(pkg, BuildSpec{}, stage); err != nil {
		t.Fatal(err)
	}
	root := filepath.Join(stage, "package")
	dockerfile, err := os.ReadFile(filepath.Join(root, "Dockerfile"))
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{
		"COPY src/ensemble/orchestrator /opt/ensemble/src/ensemble/orchestrator",
		"COPY ensemble-orchestrator /opt/ensemble/bin/ensemble-orchestrator",
		"chmod 755 /opt/ensemble/bin/ensemble-orchestrator",
	} {
		if !strings.Contains(string(dockerfile), want) {
			t.Errorf("worker Dockerfile lacks %q", want)
		}
	}
	launcher, err := os.ReadFile(filepath.Join(root, "ensemble-orchestrator"))
	if err != nil || !strings.Contains(string(launcher), "-m ensemble.orchestrator.worker_cli") {
		t.Fatalf("issue launcher missing: %s %v", launcher, err)
	}
	for _, path := range []string{"cli.py", "engine/config.py", "engine/router.py", "engine/promote.py", "config.toml", "trackers/local"} {
		if _, err := os.Stat(filepath.Join(root, "src/ensemble/orchestrator", path)); !os.IsNotExist(err) {
			t.Errorf("host-only runtime shipped: %s (%v)", path, err)
		}
	}
	for _, args := range [][]string{{"--help"}, {"issue", "--help"}, {"issue", "read", "--help"}} {
		cmd := exec.Command("python3", append([]string{"-P", "-m", "ensemble.orchestrator.worker_cli"}, args...)...)
		cmd.Dir = t.TempDir()
		cmd.Env = append(os.Environ(), "PYTHONPATH="+filepath.Join(root, "src"), "PYTHONDONTWRITEBYTECODE=1", "HOME="+t.TempDir())
		if out, err := cmd.CombinedOutput(); err != nil || !strings.Contains(string(out), "ensemble-orchestrator") {
			t.Fatalf("staged issue help %q: %v\n%s", args, err, out)
		}
	}
}
