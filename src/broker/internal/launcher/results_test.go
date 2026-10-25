package launcher

import (
	"bytes"
	"encoding/json"
	"testing"

	"ensemble/broker/internal/fixtures"
)

func TestOutputFixturesRoundTrip(t *testing.T) {
	for _, c := range fixtures.Cases(t, "build_output", "start_output", "close_output", "refresh_output", "status_output", "lock_query_output") {
		if !c.Valid {
			continue
		}
		t.Run(c.File, func(t *testing.T) {
			var v any
			switch c.Definition {
			case "build_output":
				v = &BuildResult{}
			case "start_output":
				v = &StartResult{}
			case "refresh_output":
				v = &RefreshResult{}
			case "status_output":
				v = &StatusResult{}
			case "lock_query_output":
				v = &LockResult{}
			default:
				v = &CloseResult{}
			}
			dec := json.NewDecoder(bytes.NewReader(c.Data))
			dec.DisallowUnknownFields()
			if err := dec.Decode(v); err != nil {
				t.Fatal(err)
			}
			out, _ := json.Marshal(v)
			if !fixtures.SameJSON(t, out, c.Data) {
				t.Fatalf("got %s want %s", out, c.Data)
			}
		})
	}
}
