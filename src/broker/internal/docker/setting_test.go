package docker

import (
	"errors"
	"slices"
	"strings"
	"testing"
)

// TestTargetSettings: the broker reads only [docker], whatever else the shared config file holds.
func TestTargetSettings(t *testing.T) {
	sixteen := `["ensemble-broker-env/a", "ensemble-broker-env/b", "ensemble-broker-env/c", "ensemble-broker-env/d", "ensemble-broker-env/e", "ensemble-broker-env/f", "ensemble-broker-env/g", "ensemble-broker-env/h", "ensemble-broker-env/i", "ensemble-broker-env/j", "ensemble-broker-env/k", "ensemble-broker-env/l", "ensemble-broker-env/m", "ensemble-broker-env/n", "ensemble-broker-env/o", "ensemble-broker-env/p"`
	// Each of TOML's other forms, holding text that would read as the target if a string or array ended early.
	others := "# [docker]\n" +
		"[session.hosts]\nhidden_size = \"120x40\" # [docker]\n\n" +
		"[delegate.roles.engineer]\nnote = \"\"\"\n[docker]\nhost = \"ssh://evil\"\n\"\"\"\n" +
		"quoted = \"\"\"a \\\"\"\"\n[docker]\n\"\"\"\"\"\n" +
		"literal = '''\n[docker]\nC:\\path\\'''''\n" +
		"escaped = \"\\\"[docker]\\\"\"\n" +
		"single = '[docker] \\'\n" +
		"list = [\n  \"]\", # [docker]\n  [1, 2.5e3, -inf],\n  { a = \"}\" },\n]\n" +
		"when = 1979-05-27 07:32:00Z\nday = 1979-05-27\nflag = true\n" +
		"\"a.b\".'c' = { docker = { host = \"ssh://evil\" } }\n" +
		"docker = \"not the target: a key of [delegate.roles.engineer]\"\n\n" +
		"[[delegate.environments.mini-op.credentials.env]]\nname = \"x\"\nitem = { kind = \"env\" }\n"
	for _, c := range []struct {
		content      string
		host         string
		localSecrets []string
	}{
		{others, "", nil},
		{"", "", nil},
		{dockerTable(`host = "ssh://u@pc"` + "\nlocal_secrets = [\"ensemble-broker-env/mini-op-password\", \"ensemble-broker-env/mini-op-ssh-key\"]"), "ssh://u@pc", []string{"ensemble-broker-env/mini-op-password", "ensemble-broker-env/mini-op-ssh-key"}},
		{dockerTable("local_secrets = []\nhost = 'ssh://pc:2222'"), "ssh://pc:2222", []string{}},
		{dockerTable(`host = "ssh://pc"` + "\nlocal_secrets = " + sixteen + "]"), "ssh://pc", []string{"ensemble-broker-env/a", "ensemble-broker-env/b", "ensemble-broker-env/c", "ensemble-broker-env/d", "ensemble-broker-env/e", "ensemble-broker-env/f", "ensemble-broker-env/g", "ensemble-broker-env/h", "ensemble-broker-env/i", "ensemble-broker-env/j", "ensemble-broker-env/k", "ensemble-broker-env/l", "ensemble-broker-env/m", "ensemble-broker-env/n", "ensemble-broker-env/o", "ensemble-broker-env/p"}},
		{others + dockerTable(`host = "ssh://u@pc"`+"\nlocal_secrets = [\n  \"ensemble-broker-env/a\", # kept on the Mac\n]") + "\n[after]\nhost = \"ssh://evil\"\n", "ssh://u@pc", []string{"ensemble-broker-env/a"}},
		{"[ \"docker\" ]\r\nhost = \"ssh://pc\"\r\nlocal_secrets = []\r\n", "ssh://pc", []string{}},
	} {
		home := t.TempDir()
		writeTarget(t, home, c.content)
		target, err := LoadTarget(home)
		if err != nil || target.Host != c.host || !slices.Equal(target.LocalSecrets, c.localSecrets) {
			t.Errorf("%s: %+v %v", c.content, target, err)
		}
	}
	for _, body := range []string{
		`host = "ssh://pc"`,
		"local_secrets = []",
		`host = "ssh://pc"` + "\nlocal_secrets = \"ensemble-broker-env/mini-op-password\"",
		`host = "ssh://pc"` + "\nlocal_secrets = [1]",
		`host = "ssh://pc"` + "\nlocal_secrets = [\"mini-op-password\"]",
		`host = "ssh://pc"` + "\nlocal_secrets = [\"ensemble-broker-claude/x\"]",
		`host = "ssh://pc"` + "\nlocal_secrets = [\"ensemble-broker-env/Mini\"]",
		`host = "ssh://pc"` + "\nlocal_secrets = [\"ensemble-broker-env/a\", \"ensemble-broker-env/a\"]",
		`host = "ssh://pc"` + "\nlocal_secrets = " + sixteen + `, "ensemble-broker-env/q"]`,
		`host = "ssh://pc"` + "\nlocal_secrets = []\nextra = 1",
		`host = "ssh://pc"` + "\nlocal_secrets = [\"ensemble-broker-env/a\"]\nLOCAL_SECRETS = []",
		`host = "ssh://pc"` + "\nlocal_secrets = [[\"ensemble-broker-env/a\"]]",
		`host = "ssh://pc"` + "\nlocal_secrets = [\"ensemble-broker-env/\\u0061\"]",
		`host = "ssh://pc"` + "\nlocal_secrets = []\n[docker.extra]",
	} {
		home := t.TempDir()
		writeTarget(t, home, dockerTable(body))
		_, err := LoadTarget(home)
		var u *Unreachable
		if !errors.As(err, &u) || !strings.HasPrefix(u.Reason, "invalid target setting (") {
			t.Errorf("%s: %v", body, err)
		}
	}
}
