package sentinel

import (
	"fmt"
	"sync"
)

// AnyStub mirrors Node createAnyStub: callable no-op that lazily nests missing props.
// Used for window/document holes so Turnstile opcode programs do not see nil.
type AnyStub struct {
	mu    sync.Mutex
	props map[string]any
}

func newAnyStub(seed map[string]any) *AnyStub {
	s := &AnyStub{props: map[string]any{}}
	for k, v := range seed {
		s.props[k] = v
	}
	return s
}

// Call implements a no-op function (Node apply → undefined).
func (s *AnyStub) Call(args ...any) (any, error) {
	return nil, nil
}

// Get returns prop or lazily creates nested AnyStub (except then/length specials).
func (s *AnyStub) Get(key string) any {
	if s == nil {
		return newAnyStub(nil)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if key == "then" {
		// avoid accidental thenable
		return nil
	}
	if key == "length" {
		if v, ok := s.props["length"]; ok {
			return v
		}
		return 0
	}
	if v, ok := s.props[key]; ok {
		return v
	}
	nested := newAnyStub(nil)
	s.props[key] = nested
	return nested
}

// Set stores a property.
func (s *AnyStub) Set(key string, v any) {
	if s == nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.props == nil {
		s.props = map[string]any{}
	}
	s.props[key] = v
}

// AsMap shallow-copies known props for debug.
func (s *AnyStub) AsMap() map[string]any {
	if s == nil {
		return nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make(map[string]any, len(s.props))
	for k, v := range s.props {
		out[k] = v
	}
	return out
}

func (s *AnyStub) String() string { return "[AnyStub]" }

// indexContainer implements op6 property/index access with AnyStub auto-create.
func indexContainer(container any, key any) any {
	ks := fmt.Sprint(key)
	switch c := container.(type) {
	case *AnyStub:
		return c.Get(ks)
	case map[string]any:
		if v, ok := c[ks]; ok {
			return v
		}
		// Auto-stub missing object props like Node Proxy (for window holes).
		stub := newAnyStub(nil)
		c[ks] = stub
		return stub
	case []any:
		i := int(toFloat(key))
		if i >= 0 && i < len(c) {
			return c[i]
		}
		return nil
	case string:
		i := int(toFloat(key))
		if i >= 0 && i < len(c) {
			return string(c[i])
		}
		return nil
	default:
		return nil
	}
}

// bindMethod implements op24: obj[method].bind(obj) with AnyStub support.
func bindMethod(obj any, method string) any {
	switch o := obj.(type) {
	case *AnyStub:
		v := o.Get(method)
		return asCallable(v)
	case map[string]any:
		v, ok := o[method]
		if !ok {
			// Node would create nested stub then bind → callable stub
			stub := newAnyStub(nil)
			o[method] = stub
			return asCallable(stub)
		}
		return asCallable(v)
	default:
		return asCallable(nil)
	}
}

func asCallable(v any) any {
	switch f := v.(type) {
	case func(args ...any) (any, error):
		return f
	case func(...any) any:
		return func(args ...any) (any, error) { return f(args...), nil }
	case *AnyStub:
		return f.Call
	case nil:
		// callable no-op
		return func(args ...any) (any, error) { return nil, nil }
	default:
		captured := v
		return func(args ...any) (any, error) { return captured, nil }
	}
}
