package docker

import (
	"errors"
	"strings"
)

// tomlValue is a value the setting reader keeps: a plain string, or an array of them. Every other value is skipped.
type tomlValue struct {
	str   string
	plain bool // a single-line string without escapes
	array []tomlValue
	isArr bool
}

// tomlParser tokenizes TOML well enough to skip any value and to keep plain strings and arrays of them.
type tomlParser struct {
	s       string
	i, line int
}

func (p *tomlParser) done() bool { return p.i >= len(p.s) }

func (p *tomlParser) peek() byte {
	if p.done() {
		return 0
	}
	return p.s[p.i]
}

// skipBlank skips spaces, tabs and comments, and newlines too when newlines is set.
func (p *tomlParser) skipBlank(newlines bool) {
	for !p.done() {
		switch c := p.s[p.i]; {
		case c == ' ' || c == '\t' || c == '\r' && newlines:
			p.i++
		case c == '\n' && newlines:
			p.i++
			p.line++
		case c == '#':
			for !p.done() && p.s[p.i] != '\n' {
				p.i++
			}
		default:
			return
		}
	}
}

// lineEnd consumes the rest of a statement's line: blanks, a comment, then a newline or the end.
func (p *tomlParser) lineEnd() bool {
	p.skipBlank(false)
	if p.done() {
		return true
	}
	if strings.HasPrefix(p.s[p.i:], "\r\n") {
		p.i++
	}
	if p.s[p.i] != '\n' {
		return false
	}
	p.i++
	p.line++
	return true
}

func bareKeyChar(c byte) bool {
	return c >= 'A' && c <= 'Z' || c >= 'a' && c <= 'z' || c >= '0' && c <= '9' || c == '_' || c == '-'
}

// keyPath reads a dotted key; a quoted part with an escape is refused, since it could spell any key.
func (p *tomlParser) keyPath() ([]string, error) {
	var path []string
	for {
		var key string
		switch c := p.peek(); {
		case c == '"' || c == '\'':
			v, err := p.str()
			if err != nil {
				return nil, err
			}
			if !v.plain {
				return nil, errors.New("a quoted key must be a plain single-line string")
			}
			key = v.str
		case bareKeyChar(c):
			start := p.i
			for !p.done() && bareKeyChar(p.s[p.i]) {
				p.i++
			}
			key = p.s[start:p.i]
		default:
			return nil, errors.New("expected a key")
		}
		path = append(path, key)
		p.skipBlank(false)
		if p.peek() != '.' {
			return path, nil
		}
		p.i++
		p.skipBlank(false)
	}
}

func (p *tomlParser) keyValue() ([]string, tomlValue, error) {
	path, err := p.keyPath()
	if err != nil {
		return nil, tomlValue{}, err
	}
	if p.peek() != '=' {
		return nil, tomlValue{}, errors.New("expected =")
	}
	p.i++
	p.skipBlank(false)
	v, err := p.value()
	return path, v, err
}

func (p *tomlParser) value() (tomlValue, error) {
	switch c := p.peek(); c {
	case '"', '\'':
		return p.str()
	case '[':
		return p.array()
	case '{':
		return tomlValue{}, p.inlineTable()
	default:
		// A number, boolean or date-time, kept unchecked: the strict TOML parser of the configuration's other
		// readers refuses a malformed one. A space joins a date and a time.
		start := p.i
		for !p.done() {
			c := p.s[p.i]
			if bareKeyChar(c) || c == '+' || c == '.' || c == ':' ||
				c == ' ' && p.i+1 < len(p.s) && p.s[p.i+1] >= '0' && p.s[p.i+1] <= '9' {
				p.i++
				continue
			}
			break
		}
		if p.i == start {
			return tomlValue{}, errors.New("expected a value")
		}
		return tomlValue{}, nil
	}
}

func (p *tomlParser) array() (tomlValue, error) {
	p.i++
	out := tomlValue{isArr: true}
	for {
		p.skipBlank(true)
		if p.peek() == ']' {
			p.i++
			return out, nil
		}
		v, err := p.value()
		if err != nil {
			return tomlValue{}, err
		}
		out.array = append(out.array, v)
		p.skipBlank(true)
		switch p.peek() {
		case ',':
			p.i++
		case ']':
		default:
			return tomlValue{}, errors.New("unclosed array")
		}
	}
}

func (p *tomlParser) inlineTable() error {
	p.i++
	for {
		p.skipBlank(true)
		if p.peek() == '}' {
			p.i++
			return nil
		}
		if _, _, err := p.keyValue(); err != nil {
			return err
		}
		p.skipBlank(true)
		switch p.peek() {
		case ',':
			p.i++
		case '}':
			p.i++
			return nil
		default:
			return errors.New("unclosed inline table")
		}
	}
}

// str reads any of TOML's four string forms; only a single-line one without escapes keeps its text.
func (p *tomlParser) str() (tomlValue, error) {
	quote := p.s[p.i]
	basic := quote == '"'
	triple := strings.Repeat(string(quote), 3)
	if strings.HasPrefix(p.s[p.i:], triple) {
		p.i += 3
		for !p.done() {
			switch c := p.s[p.i]; {
			case c == '\\' && basic:
				// An escape, or a line-ending backslash that trims the line break.
				p.i++
				if p.peek() == '\n' {
					p.line++
				}
				p.i++
				continue
			case c == quote && strings.HasPrefix(p.s[p.i:], triple):
				// Up to two quotes right before the closing three belong to the string.
				run := 0
				for p.i+run < len(p.s) && p.s[p.i+run] == quote {
					run++
				}
				if run > 5 {
					return tomlValue{}, errors.New("too many quotes closing a string")
				}
				p.i += run
				return tomlValue{}, nil
			case c == '\n':
				p.line++
			}
			p.i++
		}
		return tomlValue{}, errors.New("unclosed multi-line string")
	}
	p.i++
	start, escaped := p.i, false
	for !p.done() {
		switch c := p.s[p.i]; {
		case c == '\\' && basic:
			escaped = true
			p.i++
			if p.peek() == '\n' {
				return tomlValue{}, errors.New("unclosed string")
			}
			p.i++
			continue
		case c == quote:
			v := tomlValue{str: p.s[start:p.i], plain: !escaped}
			p.i++
			return v, nil
		case c == '\n':
			return tomlValue{}, errors.New("unclosed string")
		}
		p.i++
	}
	return tomlValue{}, errors.New("unclosed string")
}
