package codex

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"os"
	"testing"

	"ensemble/broker/internal/npm"
)

func TestVersionOrdering(t *testing.T) {
	for _, c := range []struct {
		a, b string
		want bool
	}{
		{"0.162.10", "0.162.9", true}, {"1.0.0", "0.999.0", true},
		{"0.163.0", "0.162.999", true}, {"0.162.1", "0.162.1", false},
		{"0.162.1-beta", "0.162.0", false}, {"01.1.1", "0.0.0", false},
		{"9999999999999999999999.0.0", "999999999999999999999.0.0", true},
	} {
		if got := Newer(c.a, c.b); got != c.want {
			t.Errorf("%s > %s: %v", c.a, c.b, got)
		}
	}
}

func TestSupportedVersion(t *testing.T) {
	for _, c := range []struct {
		version   string
		supported bool
	}{
		{"0.162.999", false}, {"0.163.0", true}, {"0.164.0", true},
		{"0.163.0-beta", false}, {"invalid", false}, {"1.0.0", true},
	} {
		if SupportedVersion(c.version) != c.supported {
			t.Fatal(c)
		}
	}
}

func TestPlatformArtifacts(t *testing.T) {
	target := struct{ goos, cpu, triple string }{"darwin", "arm64", "aarch64-apple-darwin"}
	for _, mode := range []string{
		"direct", "alias", "missing", "version", "alias_package", "alias_version", "alias_target", "main_integrity",
		"platform_integrity", "malformed_integrity", "origin", "package", "oversize", "redirect", "os", "cpu", "entry",
	} {
		t.Run(target.goos+"-"+target.cpu+"/"+mode, func(t *testing.T) {
			triple := target.triple
			if mode == "entry" {
				triple = "wrong-target"
			}
			r, data := platformFixture(t, target.goos, target.cpu, triple, mode != "direct")
			mainURL := "https://registry.npmjs.org/@openai%2Fcodex/0.163.0"
			platformURL := mainURL + "-" + target.goos + "-" + target.cpu
			if mode == "direct" {
				platformURL = npm.Package("@openai/codex-"+target.goos+"-"+target.cpu, "0.163.0")
			}
			var main, platform map[string]any
			if err := json.Unmarshal(data[mainURL], &main); err != nil {
				t.Fatal(err)
			}
			if err := json.Unmarshal(data[platformURL], &platform); err != nil {
				t.Fatal(err)
			}
			dependencies := main["optionalDependencies"].(map[string]any)
			key := "@openai/codex-" + target.goos + "-" + target.cpu
			switch mode {
			case "missing":
				delete(dependencies, key)
			case "version":
				dependencies[key] = "0.164.0"
			case "alias_package":
				dependencies[key] = "npm:@evil/codex@0.163.0-" + target.goos + "-" + target.cpu
			case "alias_version":
				dependencies[key] = "npm:@openai/codex@0.164.0-" + target.goos + "-" + target.cpu
			case "alias_target":
				dependencies[key] = "npm:@openai/codex@0.163.0-other-" + target.cpu
			case "main_integrity":
				data["https://registry.npmjs.org/main.tgz"] = []byte("corrupt")
			case "platform_integrity":
				data["https://registry.npmjs.org/native.tgz"] = []byte("corrupt")
			case "malformed_integrity":
				platform["dist"].(map[string]any)["integrity"] = "sha512-AAAA"
			case "origin":
				platform["dist"].(map[string]any)["tarball"] = "https://evil.example/package.tgz"
			case "package":
				platform["name"] = "@evil/codex"
			case "os", "cpu":
				platform[mode] = []string{"other"}
			case "redirect":
				r.http.Transport = roundTrip(func(req *http.Request) (*http.Response, error) {
					location := http.Header{"Location": []string{"https://evil.example/"}}
					return &http.Response{StatusCode: 302, Header: location, Body: http.NoBody, Request: req}, nil
				})
			}
			data[mainURL], _ = json.Marshal(main)
			data[platformURL], _ = json.Marshal(platform)
			if mode == "oversize" {
				data[platformURL] = bytes.Repeat([]byte(" "), npm.MaxMetadata+1)
			}
			var got []byte
			installed, err := r.Install(context.Background(), "0.163.0", false)
			if err == nil {
				got, err = os.ReadFile(installed.Path)
			}
			if mode == "direct" || mode == "alias" {
				if err != nil || !bytes.Equal(got, []byte("xxxxxx")) {
					t.Fatalf("%q %v", got, err)
				}
			} else if err == nil {
				t.Fatal("invalid platform artifact accepted")
			}
		})
	}
}
