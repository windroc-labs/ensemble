package launcher

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"ensemble/broker/internal/fixtures"
)

func TestStateVolumeConstantsMatchTheProtocol(t *testing.T) {
	data, err := os.ReadFile(filepath.Join(fixtures.Dir(), "..", "state-volume.json"))
	if err != nil {
		t.Fatal(err)
	}
	var found map[string]string
	if err := json.Unmarshal(data, &found); err != nil {
		t.Fatal(err)
	}
	want := map[string]string{"client_homes": ClientHomes, "state_volume_prefix": stateVolumePrefix, "state_label": stateLabel,
		"state_owner_label": stateOwnerLabel, "reader_label": readerLabel}
	if !reflect.DeepEqual(found, want) {
		t.Fatalf("state-volume.json %v, want %v", found, want)
	}
}
