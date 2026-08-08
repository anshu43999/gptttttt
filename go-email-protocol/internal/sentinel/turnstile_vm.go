package sentinel

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"math"
	"strconv"
	"strings"
	"sync"
)

// TurnstileVM ports Node TurnstileVM. JSON.parse queues are []any; drain uses coerceQueue.
type TurnstileVM struct {
	env              Env
	state            map[float64]any
	handlers         map[int]func(args ...any) (any, error)
	instructionCount int
	trace            [][]any
	settled          bool
	result           any
	resultRaw        string
	rejectErr        error
	mu               sync.Mutex
}

func NewTurnstileVM(env Env) *TurnstileVM {
	vm := &TurnstileVM{env: env, state: make(map[float64]any), handlers: make(map[int]func(args ...any) (any, error))}
	vm.install()
	return vm
}

func (vm *TurnstileVM) Run(ctx context.Context, program [][]any) (string, error) {
	vm.mu.Lock()
	defer vm.mu.Unlock()
	vm.settled, vm.instructionCount, vm.trace, vm.result, vm.rejectErr = false, 0, nil, nil, nil
	q := make([][]any, len(program))
	for i, row := range program {
		q[i] = append([]any(nil), row...)
	}
	vm.state[9] = q
	if err := vm.drain(ctx); err != nil {
		return "", err
	}
	if vm.rejectErr != nil {
		return "", vm.rejectErr
	}
	if !vm.settled {
		return "", fmt.Errorf("turnstile vm completed without return: instructions=%d recent=%v", vm.instructionCount, vm.trace)
	}
	return fmt.Sprint(vm.result), nil
}

func (vm *TurnstileVM) settleOK(v any) {
	if vm.settled {
		return
	}
	vm.settled = true
	raw := fmt.Sprint(v)
	vm.resultRaw = raw
	vm.result = base64.StdEncoding.EncodeToString([]byte(raw))
}

func (vm *TurnstileVM) settleErr(v any) {
	if vm.settled {
		return
	}
	vm.settled = true
	vm.rejectErr = fmt.Errorf("turnstile reject: %s", base64.StdEncoding.EncodeToString([]byte(fmt.Sprint(v))))
}

func (vm *TurnstileVM) drain(ctx context.Context) error {
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		default:
		}
		if vm.settled {
			return nil
		}
		q := coerceQueue(vm.state[9])
		if len(q) == 0 {
			return nil
		}
		row := q[0]
		vm.state[9] = q[1:]
		if len(row) == 0 {
			continue
		}
		opcodeRaw, args := row[0], row[1:]
		vm.trace = append(vm.trace, append([]any{opcodeRaw}, args...))
		if len(vm.trace) > 20 {
			vm.trace = vm.trace[1:]
		}
		opcodeKey := toFloat(opcodeRaw)
		opcode := int(math.Trunc(opcodeKey))
		handler := vm.lookupHandler(opcodeKey, opcode)
		if handler == nil {
			return fmt.Errorf("unsupported opcode %v raw=%v valueType=%T recent=%v", opcode, opcodeRaw, vm.state[opcodeKey], vm.trace)
		}
		if _, err := handler(args...); err != nil {
			return err
		}
		vm.instructionCount++
		if vm.instructionCount > 2000000 {
			return fmt.Errorf("turnstile instruction limit")
		}
	}
}

func (vm *TurnstileVM) lookupHandler(opcodeKey float64, opcode int) func(args ...any) (any, error) {
	if v, ok := vm.state[opcodeKey]; ok {
		if fn, ok := v.(func(args ...any) (any, error)); ok {
			return fn
		}
	}
	if h, ok := vm.handlers[opcode]; ok {
		return h
	}
	if v, ok := vm.state[float64(opcode)]; ok {
		if fn, ok := v.(func(args ...any) (any, error)); ok {
			return fn
		}
	}
	return nil
}

func (vm *TurnstileVM) readRef(ref any) any {
	if f, ok := asFloat(ref); ok {
		if v, ok := vm.state[f]; ok {
			return v
		}
	}
	return ref
}

func (vm *TurnstileVM) invokeFunction(fn any, argSlots []any) (any, error) {
	resolved := make([]any, len(argSlots))
	for i, a := range argSlots {
		resolved[i] = vm.readRef(a)
	}
	return vm.callRaw(fn, resolved)
}

func (vm *TurnstileVM) callRaw(fn any, args []any) (any, error) {
	if fn == nil {
		// Node would still call a stub → undefined
		return nil, nil
	}
	switch f := fn.(type) {
	case func(args ...any) (any, error):
		return f(args...)
	case func(...any) any:
		return f(args...), nil
	case *AnyStub:
		return f.Call(args...)
	default:
		// Last resort: treat non-function as no-op (matches createAnyStub apply)
		return nil, nil
	}
}

func slotID(v any) float64 { return toFloat(v) }

func toFloat(v any) float64 {
	switch x := v.(type) {
	case float64:
		return x
	case float32:
		return float64(x)
	case int:
		return float64(x)
	case int64:
		return float64(x)
	case json.Number:
		f, _ := x.Float64()
		return f
	case string:
		f, _ := strconv.ParseFloat(x, 64)
		return f
	case bool:
		if x {
			return 1
		}
		return 0
	default:
		return 0
	}
}

func asFloat(v any) (float64, bool) {
	switch x := v.(type) {
	case float64:
		return x, true
	case float32:
		return float64(x), true
	case int:
		return float64(x), true
	case int64:
		return float64(x), true
	case json.Number:
		f, err := x.Float64()
		return f, err == nil
	case string:
		f, err := strconv.ParseFloat(x, 64)
		return f, err == nil
	default:
		return 0, false
	}
}

func decodeBase64Node(s string) ([]byte, error) {
	const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
	var b strings.Builder
	b.Grow(len(s))
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch {
		case c == '=':
			b.WriteByte(c)
		case c == '-':
			b.WriteByte('+')
		case c == '_':
			b.WriteByte('/')
		case strings.IndexByte(alphabet, c) >= 0:
			b.WriteByte(c)
		}
	}
	out := strings.TrimRight(b.String(), "=")
	if out == "" {
		return []byte{}, nil
	}
	switch len(out) % 4 {
	case 1:
		out = out[:len(out)-1]
	case 2:
		out += "=="
	case 3:
		out += "="
	}
	raw, err := base64.StdEncoding.DecodeString(out)
	if err != nil {
		return nil, fmt.Errorf("illegal base64 data (len=%d prefix=%q): %w", len(s), trimPrefix(s, 24), err)
	}
	return raw, nil
}

func (vm *TurnstileVM) install() {
	vm.handlers[0] = func(args ...any) (any, error) {
		if len(args) < 1 {
			return nil, nil
		}
		raw, err := decodeBase64Node(fmt.Sprint(args[0]))
		if err != nil {
			return nil, err
		}
		key := fmt.Sprint(vm.state[16])
		var nested [][]any
		if err := json.Unmarshal([]byte(xorCipher(string(raw), key)), &nested); err != nil {
			return nil, err
		}
		prev := coerceQueue(vm.state[9])
		vm.state[9] = nested
		err = vm.drain(context.Background())
		vm.state[9] = prev
		msg := fmt.Sprintf("%d: undefined", vm.instructionCount)
		if err != nil {
			msg = fmt.Sprintf("%d: %v", vm.instructionCount, err)
		}
		return base64.StdEncoding.EncodeToString([]byte(msg)), err
	}
	vm.handlers[1] = func(args ...any) (any, error) {
		if len(args) < 2 {
			return nil, nil
		}
		dst, src := slotID(args[0]), slotID(args[1])
		vm.state[dst] = xorCipher(fmt.Sprint(vm.state[dst]), fmt.Sprint(vm.state[src]))
		return nil, nil
	}
	vm.handlers[2] = func(args ...any) (any, error) {
		if len(args) >= 2 {
			vm.state[slotID(args[0])] = args[1]
		}
		return nil, nil
	}
	vm.handlers[3] = func(args ...any) (any, error) {
		var v any
		if len(args) > 0 {
			v = args[0]
		}
		vm.settleOK(v)
		return nil, nil
	}
	vm.handlers[4] = func(args ...any) (any, error) {
		var v any
		if len(args) > 0 {
			v = args[0]
		}
		vm.settleErr(v)
		return nil, nil
	}
	vm.handlers[5] = func(args ...any) (any, error) {
		if len(args) < 2 {
			return nil, nil
		}
		dst, src := slotID(args[0]), slotID(args[1])
		cur := vm.state[dst]
		if arr, ok := cur.([]any); ok {
			vm.state[dst] = append(arr, vm.state[src])
		} else {
			vm.state[dst] = fmt.Sprint(cur) + fmt.Sprint(vm.state[src])
		}
		return nil, nil
	}
		vm.handlers[6] = func(args ...any) (any, error) {
		if len(args) < 3 {
			return nil, nil
		}
		dst, src, idx := slotID(args[0]), slotID(args[1]), slotID(args[2])
		key := vm.state[idx]
		if key == nil {
			key = idx
		}
		vm.state[dst] = indexContainer(vm.state[src], key)
		return nil, nil
	}
	vm.handlers[7] = func(args ...any) (any, error) {
		if len(args) < 1 {
			return nil, nil
		}
		_, err := vm.invokeFunction(vm.state[slotID(args[0])], args[1:])
		return nil, err
	}
	vm.handlers[8] = func(args ...any) (any, error) {
		if len(args) >= 2 {
			vm.state[slotID(args[0])] = vm.state[slotID(args[1])]
		}
		return nil, nil
	}
	vm.state[10] = buildWindowMap(vm.env)
	vm.handlers[11] = func(args ...any) (any, error) {
		if len(args) < 2 {
			return nil, nil
		}
		needle := fmt.Sprint(vm.readRef(args[1]))
		var found any
		for _, s := range vm.env.ScriptSources {
			if needle == "" || strings.Contains(s, needle) {
				found = s
				break
			}
		}
		vm.state[slotID(args[0])] = found
		return nil, nil
	}
	vm.handlers[12] = func(args ...any) (any, error) {
		if len(args) >= 1 {
			vm.state[slotID(args[0])] = vm.state
		}
		return nil, nil
	}
	vm.handlers[13] = func(args ...any) (any, error) {
		if len(args) < 2 {
			return nil, nil
		}
		if _, err := vm.callRaw(vm.state[slotID(args[1])], args[2:]); err != nil {
			vm.state[slotID(args[0])] = err.Error()
		}
		return nil, nil
	}
	vm.handlers[14] = func(args ...any) (any, error) {
		if len(args) < 2 {
			return nil, nil
		}
		dst := slotID(args[0])
		raw := fmt.Sprint(vm.readRef(args[1]))
		var v any
		if err := json.Unmarshal([]byte(raw), &v); err != nil {
			if b, err2 := decodeBase64Node(raw); err2 == nil {
				if err3 := json.Unmarshal(b, &v); err3 == nil {
					vm.state[dst] = v
					return nil, nil
				}
			}
			return nil, fmt.Errorf("JSON.parse failed: %v", err)
		}
		vm.state[dst] = v
		return nil, nil
	}
	vm.handlers[15] = func(args ...any) (any, error) {
		if len(args) < 2 {
			return nil, nil
		}
		b, err := json.Marshal(vm.state[slotID(args[1])])
		if err != nil {
			return nil, err
		}
		vm.state[slotID(args[0])] = string(b)
		return nil, nil
	}
	vm.handlers[17] = func(args ...any) (any, error) {
		if len(args) < 2 {
			return nil, nil
		}
		dst := slotID(args[0])
		res, err := vm.invokeFunction(vm.state[slotID(args[1])], args[2:])
		if err != nil {
			vm.state[dst] = err.Error()
			return nil, nil
		}
		vm.state[dst] = res
		return nil, nil
	}
	vm.handlers[18] = func(args ...any) (any, error) {
		if len(args) < 1 {
			return nil, nil
		}
		slot := slotID(args[0])
		raw, err := decodeBase64Node(fmt.Sprint(vm.state[slot]))
		if err != nil {
			return nil, fmt.Errorf("op18 base64: %w", err)
		}
		vm.state[slot] = string(raw)
		return nil, nil
	}
	vm.handlers[19] = func(args ...any) (any, error) {
		if len(args) < 1 {
			return nil, nil
		}
		slot := slotID(args[0])
		vm.state[slot] = base64.StdEncoding.EncodeToString([]byte(fmt.Sprint(vm.state[slot])))
		return nil, nil
	}
	vm.handlers[20] = func(args ...any) (any, error) {
		if len(args) < 3 {
			return nil, nil
		}
		if vm.state[slotID(args[0])] == vm.state[slotID(args[1])] {
			_, err := vm.callRaw(vm.state[slotID(args[2])], args[3:])
			return nil, err
		}
		return nil, nil
	}
	vm.handlers[21] = func(args ...any) (any, error) {
		if len(args) < 4 {
			return nil, nil
		}
		if math.Abs(toFloat(vm.state[slotID(args[0])])-toFloat(vm.state[slotID(args[1])])) > toFloat(vm.state[slotID(args[2])]) {
			_, err := vm.callRaw(vm.state[slotID(args[3])], args[4:])
			return nil, err
		}
		return nil, nil
	}
	vm.handlers[22] = func(args ...any) (any, error) {
		if len(args) < 2 {
			return nil, nil
		}
		dst := slotID(args[0])
		prev := coerceQueue(vm.state[9])
		vm.state[9] = coerceQueue(args[1])
		err := vm.drain(context.Background())
		vm.state[9] = prev
		if err != nil {
			vm.state[dst] = err.Error()
		}
		return nil, nil
	}
	vm.handlers[23] = func(args ...any) (any, error) {
		if len(args) < 2 {
			return nil, nil
		}
		if _, ok := vm.state[slotID(args[0])]; ok {
			_, err := vm.callRaw(vm.state[slotID(args[1])], args[2:])
			return nil, err
		}
		return nil, nil
	}
		vm.handlers[24] = func(args ...any) (any, error) {
		if len(args) < 3 {
			return nil, nil
		}
		dst := slotID(args[0])
		obj := vm.readRef(args[1])
		method := fmt.Sprint(vm.readRef(args[2]))
		vm.state[dst] = bindMethod(obj, method)
		return nil, nil
	}
	vm.handlers[25] = func(args ...any) (any, error) { return nil, nil }
	vm.handlers[26] = func(args ...any) (any, error) { return nil, nil }
	vm.handlers[27] = func(args ...any) (any, error) {
		if len(args) < 2 {
			return nil, nil
		}
		dst, src := slotID(args[0]), slotID(args[1])
		cur := vm.state[dst]
		if arr, ok := cur.([]any); ok {
			target := vm.state[src]
			for i, item := range arr {
				if item == target {
					vm.state[dst] = append(arr[:i], arr[i+1:]...)
					break
				}
			}
		} else {
			vm.state[dst] = toFloat(cur) - toFloat(vm.state[src])
		}
		return nil, nil
	}
	vm.handlers[28] = func(args ...any) (any, error) { return nil, nil }
	vm.handlers[29] = func(args ...any) (any, error) {
		if len(args) >= 3 {
			vm.state[slotID(args[0])] = toFloat(vm.state[slotID(args[1])]) < toFloat(vm.state[slotID(args[2])])
		}
		return nil, nil
	}
	vm.handlers[30] = func(args ...any) (any, error) {
		if len(args) < 3 {
			return nil, nil
		}
		dst, resultSlot := slotID(args[0]), slotID(args[1])
		var argSlots []float64
		var queue [][]any
		if len(args) >= 4 {
			if arr, ok := args[2].([]any); ok {
				for _, a := range arr {
					argSlots = append(argSlots, slotID(a))
				}
			}
			queue = coerceQueue(args[3])
		} else {
			queue = coerceQueue(args[2])
		}
		vm.state[dst] = func(cbArgs ...any) (any, error) {
			prev := coerceQueue(vm.state[9])
			for i, slot := range argSlots {
				if i < len(cbArgs) {
					vm.state[slot] = cbArgs[i]
				}
			}
			vm.state[9] = append([][]any(nil), queue...)
			err := vm.drain(context.Background())
			vm.state[9] = prev
			if err != nil {
				return fmt.Sprint(err), nil
			}
			return vm.state[resultSlot], nil
		}
		return nil, nil
	}
	vm.handlers[33] = func(args ...any) (any, error) {
		if len(args) >= 3 {
			vm.state[slotID(args[0])] = toFloat(vm.state[slotID(args[1])]) * toFloat(vm.state[slotID(args[2])])
		}
		return nil, nil
	}
	vm.handlers[34] = func(args ...any) (any, error) {
		if len(args) >= 2 {
			vm.state[slotID(args[0])] = vm.state[slotID(args[1])]
		}
		return nil, nil
	}
	vm.handlers[35] = func(args ...any) (any, error) {
		if len(args) < 3 {
			return nil, nil
		}
		div := toFloat(vm.state[slotID(args[2])])
		if div == 0 {
			vm.state[slotID(args[0])] = 0.0
		} else {
			vm.state[slotID(args[0])] = toFloat(vm.state[slotID(args[1])]) / div
		}
		return nil, nil
	}
	for op, h := range vm.handlers {
		vm.state[float64(op)] = h
	}
}

func coerceQueue(v any) [][]any {
	switch q := v.(type) {
	case [][]any:
		return append([][]any(nil), q...)
	case []any:
		out := make([][]any, 0, len(q))
		for _, row := range q {
			if r, ok := row.([]any); ok {
				out = append(out, r)
			}
		}
		return out
	default:
		return nil
	}
}

func buildWindowMap(env Env) map[string]any {
	scripts := make([]any, 0, len(env.ScriptSources))
	for _, s := range env.ScriptSources {
		scripts = append(scripts, map[string]any{"src": s})
	}
	mkStore := func() map[string]any {
		data := map[string]string{}
		return map[string]any{
			"getItem": func(args ...any) (any, error) {
				if len(args) < 1 {
					return nil, nil
				}
				v, ok := data[fmt.Sprint(args[0])]
				if !ok {
					return nil, nil
				}
				return v, nil
			},
			"setItem": func(args ...any) (any, error) {
				if len(args) >= 2 {
					data[fmt.Sprint(args[0])] = fmt.Sprint(args[1])
				}
				return nil, nil
			},
			"removeItem": func(args ...any) (any, error) {
				if len(args) >= 1 {
					delete(data, fmt.Sprint(args[0]))
				}
				return nil, nil
			},
			"clear": func(args ...any) (any, error) {
				for k := range data {
					delete(data, k)
				}
				return nil, nil
			},
			"key": func(args ...any) (any, error) { return nil, nil },
			"length": 0,
		}
	}
	nav := map[string]any{
		"userAgent": env.UserAgent, "language": env.Language, "languages": env.Languages,
		"hardwareConcurrency": env.HardwareConcurrency, "platform": "Win32",
		"vendor": "", "webdriver": false, "maxTouchPoints": 0,
		// HAR payload referenced requestMIDIAccess style navigator props
		"requestMIDIAccess": func(args ...any) (any, error) { return newAnyStub(nil), nil },
	}
	// Wrap window as map but missing props auto-stub via indexContainer.
	return map[string]any{
		"navigator": nav,
		"screen": map[string]any{"width": env.ScreenWidth, "height": env.ScreenHeight, "colorDepth": 24, "pixelDepth": 24, "availWidth": env.ScreenWidth, "availHeight": env.ScreenHeight},
		"innerWidth": env.ScreenWidth, "innerHeight": env.ScreenHeight,
		"outerWidth": env.ScreenWidth, "outerHeight": env.ScreenHeight,
		"devicePixelRatio": 1.0,
		"location": map[string]any{
			"href": fmt.Sprintf("https://sentinel.openai.com/backend-api/sentinel/frame.html?sv=%s", env.BuildHash),
			"pathname": "/backend-api/sentinel/frame.html",
			"search": fmt.Sprintf("?sv=%s", env.BuildHash),
			"origin": "https://sentinel.openai.com",
			"host": "sentinel.openai.com", "hostname": "sentinel.openai.com", "protocol": "https:",
		},
		"document": map[string]any{
			"scripts": scripts, "cookie": "",
			"documentElement": map[string]any{
				"getAttribute": func(args ...any) (any, error) {
					if len(args) > 0 && fmt.Sprint(args[0]) == "data-build" {
						return env.BuildHash, nil
					}
					return nil, nil
				},
			},
			"createElement": func(args ...any) (any, error) {
				return newAnyStub(map[string]any{"style": map[string]any{}}), nil
			},
			"body": newAnyStub(nil),
			"head": newAnyStub(nil),
		},
		"localStorage": mkStore(), "sessionStorage": mkStore(),
		"performance": map[string]any{
			"now": func(args ...any) (any, error) { return performanceNow(), nil },
			"timeOrigin": env.TimeOrigin,
			"memory": map[string]any{"jsHeapSizeLimit": env.JSHeapSizeLimit},
		},
		"chrome": newAnyStub(nil),
	}
}
