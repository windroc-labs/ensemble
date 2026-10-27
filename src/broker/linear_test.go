package main

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"

	"ensemble/broker/internal/broker"
)

func TestLinearHostCommands(t *testing.T) {
	var enrollments []bool
	a := app{linear: func(_ context.Context, _ keys, enroll bool) (any, error) {
		enrollments = append(enrollments, enroll)
		if enroll {
			return map[string]any{"version": 1, "enrolled": true}, nil
		}
		return broker.LinearResult{Version: 1, Renewed: true, ExpiresAt: "2026-11-26T12:00:00Z"}, nil
	}}
	for _, args := range [][]string{{"login", "linear"}, {"linear-maintain"}} {
		code, out, errOut := invoke(a, args...)
		if code != 0 || errOut != "" || !strings.Contains(out, `"version":1`) || strings.Contains(out, "access_token") {
			t.Fatalf("%d %q %q", code, out, errOut)
		}
	}
	if len(enrollments) != 2 || !enrollments[0] || enrollments[1] {
		t.Fatalf("dispatch %v", enrollments)
	}
	for _, args := range [][]string{{"login", "linear", "secret"}, {"linear-maintain", "--renew"}} {
		if code, _, _ := invoke(a, args...); code != 2 {
			t.Fatalf("invalid args accepted: %d", code)
		}
	}
	a.linear = func(context.Context, keys, bool) (any, error) { return nil, errors.New("ROOT-SENTINEL") }
	for _, args := range [][]string{{"login", "linear"}, {"linear-maintain"}} {
		code, out, errOut := invoke(a, args...)
		if code != 1 || out != "" || strings.Contains(errOut, "SENTINEL") || !strings.Contains(errOut, "Linear") {
			t.Fatalf("%d %q %q", code, out, errOut)
		}
	}
}

// A broker Linear item an earlier broker build wrote names its repair, retrust.
func TestLinearStaleItemNamesRetrust(t *testing.T) {
	a := app{linear: func(context.Context, keys, bool) (any, error) {
		return nil, fmt.Errorf("maintenance: %w", broker.ErrLinearStale)
	}}
	code, out, errOut := invoke(a, "linear-maintain")
	if code != 1 || out != "" || errOut != "ensemble-broker: "+broker.ErrLinearStale.Error()+"\n" || !strings.Contains(errOut, broker.RetrustCommand) {
		t.Fatalf("%d %q %q", code, out, errOut)
	}
}
