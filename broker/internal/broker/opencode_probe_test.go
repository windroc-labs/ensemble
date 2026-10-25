package broker

import (
	"bytes"
	"context"
	"errors"
	"net/http"
	"strings"
	"testing"
	"time"

	"ensemble/broker/internal/protocol"
)

type probeBody struct {
	t      *testing.T
	reads  int
	closes int
}

func (b *probeBody) Read([]byte) (int, error) {
	b.reads++
	b.t.Fatal("provider probe read the response body")
	return 0, errors.New("unexpected body read")
}

func (b *probeBody) Close() error {
	b.closes++
	return nil
}

func TestOpencodeProbe(t *testing.T) {
	for _, status := range []int{200, 204, 301, 401, 500} {
		k := &fakeSecrets{items: map[string][]byte{"ensemble-opencode/strata": []byte(apiKey)}}
		calls := 0
		body := &probeBody{t: t}
		transport := roundTrip(func(req *http.Request) (*http.Response, error) {
			calls++
			deadline, ok := req.Context().Deadline()
			if remaining := time.Until(deadline); !ok || remaining <= 0 || remaining > 5*time.Second {
				t.Fatalf("probe deadline: %v %v", deadline, ok)
			}
			if req.URL.String() != "https://strata.test/v1/models" || req.Header.Get("Authorization") != "Bearer "+apiKey {
				t.Fatal("wrong endpoint or authentication")
			}
			response := &http.Response{StatusCode: status, Header: make(http.Header), Body: body}
			response.Header.Set("Location", "https://elsewhere.test/steal")
			return response, nil
		})
		available, err := (OpencodeKeys{Keychain: k}).Probe(context.Background(), "strata", "https://strata.test/v1/", transport)
		if err != nil || available != (status >= 200 && status < 300) || calls != 1 {
			t.Fatalf("status=%d available=%v err=%v calls=%d", status, available, err, calls)
		}
		if body.reads != 0 || body.closes != 1 {
			t.Fatalf("status=%d body reads=%d closes=%d", status, body.reads, body.closes)
		}
		for _, key := range k.returned {
			if !bytes.Equal(key, make([]byte, len(key))) {
				t.Fatal("key not erased")
			}
		}
	}
}

func TestOpencodeProbeFailuresAreOpaque(t *testing.T) {
	k := &fakeSecrets{items: map[string][]byte{"ensemble-opencode/strata": []byte(apiKey)}}
	transport := roundTrip(func(*http.Request) (*http.Response, error) { return nil, errors.New(apiKey) })
	available, err := (OpencodeKeys{Keychain: k}).Probe(context.Background(), "strata", "http://strata.test/v1", transport)
	if available || err != nil {
		t.Fatalf("%v %v", available, err)
	}
	for _, failure := range []error{ErrNotFound, ErrNeedsDialog, errors.New(apiKey)} {
		k.readErr = failure
		available, err = (OpencodeKeys{Keychain: k}).Probe(context.Background(), "strata", "http://strata.test/v1", transport)
		if available || err == nil || strings.Contains(err.Error(), apiKey) {
			t.Fatalf("%v %v", available, err)
		}
	}
}

func TestOpencodeProbeClassifiesCredentialFailures(t *testing.T) {
	for name, tc := range map[string]struct {
		failure error
		invalid bool
		code    protocol.Code
	}{
		"missing":       {failure: ErrNotFound, code: protocol.LoginRequired},
		"invalid":       {invalid: true, code: protocol.LoginRequired},
		"locked":        {failure: ErrNeedsDialog, code: protocol.KeychainFailed},
		"access failed": {failure: errors.New(apiKey), code: protocol.KeychainFailed},
	} {
		t.Run(name, func(t *testing.T) {
			k := providerKeys("strata")
			k.readErr = tc.failure
			if tc.invalid {
				k.items["ensemble-opencode/strata"] = []byte("\n")
			}
			transport := roundTrip(func(*http.Request) (*http.Response, error) {
				t.Fatal("credential failure must not contact the provider")
				return nil, errors.New("unexpected request")
			})
			available, err := (OpencodeKeys{Keychain: k}).Probe(context.Background(), "strata", "https://strata.test/v1", transport)
			var failure *protocol.Error
			if available || !errors.As(err, &failure) || failure.Code != tc.code || strings.Contains(err.Error(), apiKey) {
				t.Fatalf("available=%v err=%v", available, err)
			}
			for _, key := range k.returned {
				if !bytes.Equal(key, make([]byte, len(key))) {
					t.Fatal("key not erased")
				}
			}
		})
	}
}

func TestOpencodeProbeRejectsUntrustedInputsBeforeReadingKeys(t *testing.T) {
	for _, base := range []string{"file:///tmp/key", "https://u:secret@host/v1", "https://host/v1?key=x", "https://host/v1#x", "http://", "http://host/\n", "http://host:bad/v1"} {
		k := &fakeSecrets{}
		available, err := (OpencodeKeys{Keychain: k}).Probe(context.Background(), "strata", base, nil)
		var failure *protocol.Error
		if available || !errors.As(err, &failure) || failure.Code != protocol.Usage || len(k.reads) != 0 {
			t.Fatalf("accepted %q", base)
		}
	}
	if ValidOpencodeURL("https://host/v1") != true {
		t.Fatal("valid URL refused")
	}
	k := &fakeSecrets{}
	available, err := (OpencodeKeys{Keychain: k}).Probe(context.Background(), "../bad", "https://host/v1", nil)
	var failure *protocol.Error
	if available || !errors.As(err, &failure) || failure.Code != protocol.Usage || len(k.reads) != 0 {
		t.Fatal("bad provider accepted")
	}
}
