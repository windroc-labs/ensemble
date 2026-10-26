package main

import (
	"encoding/json"
	"errors"

	"ensemble/broker/internal/audit"
	"ensemble/broker/internal/protocol"
)

// withAudit keeps operation and audit outcomes separate, including failures before a container exists.
func withAudit(rec *audit.Recorder, f func() (any, error)) (result any, err error) {
	defer func() {
		if rec.Flush(err) == nil {
			return
		}
		if err == nil {
			result = auditWarningResult{result}
			return
		}
		var e *protocol.Error
		if !errors.As(err, &e) {
			e = protocol.New(protocol.InternalError, "Unexpected broker failure.")
		}
		copy := *e
		copy.AuditWarning = protocol.AuditWriteFailed
		err = &copy
	}()
	return f()
}

type auditWarningResult struct{ result any }

func (r auditWarningResult) MarshalJSON() ([]byte, error) {
	data, err := json.Marshal(r.result)
	if err != nil {
		return nil, err
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(data, &fields); err != nil {
		return nil, err
	}
	fields["audit_warning"], _ = json.Marshal(protocol.AuditWriteFailed)
	return json.Marshal(fields)
}
