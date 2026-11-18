package docker

import (
	"errors"
	"fmt"
	"strings"
)

// settingTable is SettingTable's keys in a TOML document. The whole document is tokenized, so a header or key inside
// a string or an array is never taken for one. Any other form of a top-level SettingTable key (a dotted or inline
// table, an array of tables, a sub-table) is refused rather than read. found is false when the document has none.
func settingTable(doc string) (table map[string]tomlValue, found bool, err error) {
	p := &tomlParser{s: doc, line: 1}
	var r settingReader
	for p.skipBlank(true); !p.done(); p.skipBlank(true) {
		if p.peek() == '[' {
			err = r.header(p)
		} else {
			err = r.keyValue(p)
		}
		if err == nil && !p.lineEnd() {
			err = errors.New("unexpected text after a value")
		}
		if err != nil {
			return nil, false, fmt.Errorf("line %d: %w", p.line, err)
		}
	}
	return r.table, r.table != nil, nil
}

var errSettingForm = fmt.Errorf("write the target as a plain [%s] table", SettingTable)

// settingReader follows which table each statement belongs to; table is nil until SettingTable's header.
type settingReader struct {
	table             map[string]tomlValue
	inTable, inHeader bool
}

func (r *settingReader) header(p *tomlParser) error {
	closing := "]"
	if p.i++; p.peek() == '[' {
		p.i++
		closing = "]]"
	}
	p.skipBlank(false)
	path, err := p.keyPath()
	if err != nil {
		return err
	}
	if !strings.HasPrefix(p.s[p.i:], closing) {
		return errors.New("unclosed table header")
	}
	p.i += len(closing)
	r.inHeader, r.inTable = true, path[0] == SettingTable
	switch {
	case !r.inTable:
		return nil
	case closing == "]]" || len(path) > 1:
		return errSettingForm
	case r.table != nil:
		return fmt.Errorf("[%s] repeated", SettingTable)
	}
	r.table = map[string]tomlValue{}
	return nil
}

func (r *settingReader) keyValue(p *tomlParser) error {
	path, v, err := p.keyValue()
	switch {
	case err != nil:
		return err
	case !r.inHeader && path[0] == SettingTable:
		return errSettingForm
	case !r.inTable:
		return nil
	case len(path) > 1:
		return fmt.Errorf("unknown key %q", strings.Join(path, "."))
	}
	if _, ok := r.table[path[0]]; ok {
		return fmt.Errorf("%s repeated", path[0])
	}
	r.table[path[0]] = v
	return nil
}
