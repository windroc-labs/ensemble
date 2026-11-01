package launcher

import (
	"encoding/json"
	"reflect"
	"testing"
)

func TestStatusUsesNamedAccountsWithoutStaticMetadata(t *testing.T) {
	var checked []string
	result := Status([]string{"work", "home", "Work", "setup-token", ""}, func(account string) bool {
		checked = append(checked, account)
		return account == "work"
	})
	if !reflect.DeepEqual(checked, []string{"work", "home"}) {
		t.Fatalf("checked accounts: %v", checked)
	}
	data, err := json.Marshal(result)
	if err != nil || string(data) != `{"claude":{"home":false,"work":true}}` {
		t.Fatalf("status: %s %v", data, err)
	}
	if result := Status(nil, nil); result.Claude == nil || len(result.Claude) != 0 {
		t.Fatalf("empty accounts: %+v", result)
	}
}
