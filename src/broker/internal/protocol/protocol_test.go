package protocol

import (
	"encoding/json"
	"strings"
	"testing"
	"unicode/utf8"

	"ensemble/broker/internal/fixtures"
)

func TestText(t *testing.T) {
	for in, want := range map[string]string{
		"plain":           "plain",
		"a\nb\tc\x7fd":    `a\x0ab\x09c\x7fd`,
		"bad \xff byte":   "bad � byte",
		"  \n ":           `\x0a`,
		"":                "-",
		"\x00":            `\x00`,
		"  padded text  ": "padded text",
	} {
		if got := Text(in); got != want {
			t.Errorf("Text(%q) = %q, want %q", in, got, want)
		}
	}
	long := strings.Repeat("é", 600)
	got := Text(long)
	if len(got) > maxText || !utf8.ValidString(got) || !strings.HasPrefix(long, got) {
		t.Fatalf("bad truncation: %d bytes, valid=%v", len(got), utf8.ValidString(got))
	}
}

func TestErrorFixtures(t *testing.T) {
	for _, c := range fixtures.Cases(t, "error") {
		if !c.Valid {
			continue
		}
		t.Run(c.File, func(t *testing.T) {
			var in struct {
				ExitCode     int    `json:"exit_code"`
				AuditWarning string `json:"audit_warning"`
				Error        struct {
					Command string `json:"command"`
					Error
				} `json:"error"`
			}
			if err := json.Unmarshal(c.Data, &in); err != nil {
				t.Fatal(err)
			}
			if Exit(in.Error.Code) != in.ExitCode {
				t.Fatalf("Exit(%s) = %d, fixture %d", in.Error.Code, Exit(in.Error.Code), in.ExitCode)
			}
			in.Error.AuditWarning = in.AuditWarning
			out := Envelope(in.Error.Command, &in.Error.Error)
			if !strings.HasSuffix(string(out), "}\n") || strings.Count(string(out), "\n") != 1 {
				t.Fatalf("not one line: %q", out)
			}
			if !fixtures.SameJSON(t, out, c.Data) {
				t.Fatalf("got %s want %s", out, c.Data)
			}
		})
	}
}

func TestEnvelopeDefaultsAndRedaction(t *testing.T) {
	e := New(StartFailed, "line\none")
	e.CleanupError = "ignored unless failed"
	var got struct {
		Error map[string]any `json:"error"`
	}
	if err := json.Unmarshal(Envelope("start", e), &got); err != nil {
		t.Fatal(err)
	}
	inner := got.Error
	if inner["message"] != `line\x0aone` || inner["cleanup"] != NotNeeded || inner["cleanup_error"] != nil {
		t.Fatalf("%v", inner)
	}
	if Exit("bogus") != 70 {
		t.Fatal("unknown code must map to internal_error")
	}
}
