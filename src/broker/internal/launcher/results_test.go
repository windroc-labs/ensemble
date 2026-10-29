package launcher

import (
	"bytes"
	"encoding/json"
	"testing"

	"ensemble/broker/internal/fixtures"
	"ensemble/broker/internal/protocol"
)

func TestOutputFixturesRoundTrip(t *testing.T) {
	for _, c := range fixtures.Cases(t, "build_output", "start_output", "close_output", "maintain_output", "status_output") {
		if !c.Valid {
			continue
		}
		t.Run(c.Name, func(t *testing.T) {
			var v any
			switch c.Definition {
			case "build_output":
				v = &BuildResult{}
			case "start_output":
				v = &StartResult{}
			case "maintain_output":
				v = &MaintainResult{}
			case "status_output":
				v = &StatusResult{}
			default:
				v = &CloseResult{}
			}
			// The command boundary owns audit metadata, outside the launcher's operation result.
			var fields map[string]json.RawMessage
			if err := json.Unmarshal(c.Data, &fields); err != nil {
				t.Fatal(err)
			}
			warning := fields["audit_warning"]
			if warning != nil {
				var value string
				if err := json.Unmarshal(warning, &value); err != nil || value != protocol.AuditWriteFailed {
					t.Fatal("invalid warning fixture")
				}
				delete(fields, "audit_warning")
			}
			body, _ := json.Marshal(fields)
			dec := json.NewDecoder(bytes.NewReader(body))
			dec.DisallowUnknownFields()
			if err := dec.Decode(v); err != nil {
				t.Fatal(err)
			}
			out, _ := json.Marshal(v)
			if warning != nil {
				_ = json.Unmarshal(out, &fields)
				fields["audit_warning"] = warning
				out, _ = json.Marshal(fields)
			}
			if !fixtures.SameJSON(t, out, c.Data) {
				t.Fatalf("got %s want %s", out, c.Data)
			}
		})
	}
}
