package sentinel

import (
	"context"
	"encoding/base64"
	"crypto/rand"
	"fmt"
	"strings"
	"sync"
	"time"

	"github.com/dop251/goja"
	"github.com/google/uuid"
)

// computeTurnstileDxViaSDK is the default entry used by ComputeTurnstileDx.
// key is the requirements REQUEST p (gAAAAAC…), matching Node computeTurnstileDx second arg.
func computeTurnstileDxViaSDK(ctx context.Context, env Env, dxB64, key string) (string, error) {
	return computeTurnstileDxViaSDKWithRequirements(ctx, env, dxB64, key, key, nil)
}

// computeTurnstileDxViaSDKWithRequirements mirrors Node:
//
//	D(requirements, requestP); return _n(requirements, dx)
//
// where requirements.token should be the RESPONSE token (c) when known.
func computeTurnstileDxViaSDKWithRequirements(ctx context.Context, env Env, dxB64, requestP, responseToken string, pow map[string]any) (string, error) {
	select {
	case <-ctx.Done():
		return "", ctx.Err()
	default:
	}
	src, _, err := LoadPinnedSDK()
	if err != nil {
		return "", err
	}
	patched, err := PatchSDK(src)
	if err != nil {
		return "", err
	}

	rt := goja.New()
	loop := newTimerLoop(rt)
	if err := installSDKSandbox(rt, env, loop); err != nil {
		return "", err
	}

	script := patched + "\n;if (typeof SentinelSDK !== 'undefined') { globalThis.__codexSentinelSdk = SentinelSDK; }\n"
	if _, err := rt.RunString(script); err != nil {
		return "", fmt.Errorf("sdk eval: %w", err)
	}
	loop.Pump(ctx, 200*time.Millisecond)

	fn, err := rt.RunString(`
(function(){
  var sdk = globalThis.__codexSentinelSdk || (typeof SentinelSDK !== 'undefined' ? SentinelSDK : null);
  if (!sdk) return null;
  if (typeof sdk.__codexTurnstileDx === 'function') return sdk.__codexTurnstileDx.bind(sdk);
  return null;
})()
`)
	if err != nil {
		return "", fmt.Errorf("sdk locate hook: %w", err)
	}
	if goja.IsUndefined(fn) || goja.IsNull(fn) {
		return "", fmt.Errorf("sdk: __codexTurnstileDx not available in goja")
	}
	callable, ok := goja.AssertFunction(fn)
	if !ok {
		return "", fmt.Errorf("sdk: hook not callable")
	}

	val, err := invokeCodexTurnstile(rt, callable, dxB64, requestP, responseToken, pow)
	if err != nil {
		return "", fmt.Errorf("sdk call: %w", err)
	}
	val, err = awaitGojaValue(ctx, rt, loop, val)
	if err != nil {
		return "", err
	}
	out := val.String()
	if out == "" {
		return "", fmt.Errorf("sdk returned empty string")
	}
	if dec := tryDecodeBase64UTF8(out); looksLikeEncodedError(dec) {
		return "", fmt.Errorf("sdk returned encoded error: %s", dec)
	}
	return out, nil
}

func invokeCodexTurnstile(rt *goja.Runtime, callable goja.Callable, dxB64, requestP, responseToken string, pow map[string]any) (goja.Value, error) {
	reqObj := rt.NewObject()
	if strings.TrimSpace(responseToken) != "" {
		_ = reqObj.Set("token", responseToken)
	}
	ts := rt.NewObject()
	_ = ts.Set("dx", dxB64)
	_ = reqObj.Set("turnstile", ts)
	powObj := rt.NewObject()
	if pow != nil {
		for k, v := range pow {
			_ = powObj.Set(k, v)
		}
	} else {
		_ = powObj.Set("required", true)
	}
	_ = reqObj.Set("proofofwork", powObj)
	// Node: D(requirements, key) with key=requestP; _n(requirements, dx)
	return callable(goja.Undefined(), reqObj, rt.ToValue(requestP), rt.ToValue(dxB64))
}

type timerLoop struct {
	mu     sync.Mutex
	rt     *goja.Runtime
	nextID int64
	timers map[int64]*scheduledTimer
}

type scheduledTimer struct {
	id      int64
	when    time.Time
	fn      goja.Callable
	args    []goja.Value
	cleared bool
}

func newTimerLoop(rt *goja.Runtime) *timerLoop {
	return &timerLoop{rt: rt, nextID: 1, timers: map[int64]*scheduledTimer{}}
}

func (l *timerLoop) SetTimeout(call goja.FunctionCall) goja.Value {
	if len(call.Arguments) < 1 {
		return l.rt.ToValue(0)
	}
	fn, ok := goja.AssertFunction(call.Arguments[0])
	if !ok {
		return l.rt.ToValue(0)
	}
	delay := 0
	if len(call.Arguments) > 1 {
		delay = int(call.Arguments[1].ToInteger())
	}
	if delay < 0 {
		delay = 0
	}
	args := append([]goja.Value(nil), call.Arguments[2:]...)
	l.mu.Lock()
	id := l.nextID
	l.nextID++
	l.timers[id] = &scheduledTimer{id: id, when: time.Now().Add(time.Duration(delay) * time.Millisecond), fn: fn, args: args}
	l.mu.Unlock()
	return l.rt.ToValue(id)
}

func (l *timerLoop) ClearTimeout(call goja.FunctionCall) goja.Value {
	if len(call.Arguments) < 1 {
		return goja.Undefined()
	}
	id := call.Arguments[0].ToInteger()
	l.mu.Lock()
	if t, ok := l.timers[id]; ok {
		t.cleared = true
		delete(l.timers, id)
	}
	l.mu.Unlock()
	return goja.Undefined()
}

func (l *timerLoop) Pump(ctx context.Context, maxWait time.Duration) {
	deadline := time.Now().Add(maxWait)
	for time.Now().Before(deadline) {
		select {
		case <-ctx.Done():
			return
		default:
		}
		now := time.Now()
		var due []*scheduledTimer
		l.mu.Lock()
		for id, t := range l.timers {
			if !t.cleared && !t.when.After(now) {
				due = append(due, t)
				delete(l.timers, id)
			}
		}
		hasFuture := len(l.timers) > 0
		l.mu.Unlock()
		if len(due) == 0 {
			if !hasFuture {
				return
			}
			time.Sleep(2 * time.Millisecond)
			continue
		}
		for _, t := range due {
			if t.cleared {
				continue
			}
			_, _ = t.fn(goja.Undefined(), t.args...)
		}
	}
}

func awaitGojaValue(ctx context.Context, rt *goja.Runtime, loop *timerLoop, val goja.Value) (goja.Value, error) {
	if val == nil || goja.IsUndefined(val) || goja.IsNull(val) {
		return val, nil
	}
	obj := val.ToObject(rt)
	if obj == nil {
		return val, nil
	}
	then := obj.Get("then")
	if then == nil || goja.IsUndefined(then) || goja.IsNull(then) {
		return val, nil
	}
	thenFn, ok := goja.AssertFunction(then)
	if !ok {
		return val, nil
	}
	type box struct {
		v    goja.Value
		err  error
		done bool
	}
	var b box
	resolve := func(call goja.FunctionCall) goja.Value {
		if !b.done {
			b.done = true
			if len(call.Arguments) > 0 {
				b.v = call.Arguments[0]
			} else {
				b.v = goja.Undefined()
			}
		}
		return goja.Undefined()
	}
	reject := func(call goja.FunctionCall) goja.Value {
		if !b.done {
			b.done = true
			msg := "promise rejected"
			if len(call.Arguments) > 0 {
				msg = call.Arguments[0].String()
			}
			b.err = fmt.Errorf("%s", msg)
		}
		return goja.Undefined()
	}
	if _, err := thenFn(val, rt.ToValue(resolve), rt.ToValue(reject)); err != nil {
		return nil, fmt.Errorf("promise then: %w", err)
	}
	deadline := time.Now().Add(8 * time.Second)
	for !b.done && time.Now().Before(deadline) {
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		default:
		}
		if loop != nil {
			loop.Pump(ctx, 25*time.Millisecond)
		} else {
			time.Sleep(5 * time.Millisecond)
		}
	}
	if !b.done {
		return nil, fmt.Errorf("sdk promise not settled within timeout")
	}
	if b.err != nil {
		return nil, b.err
	}
	return b.v, nil
}

func installSDKSandbox(rt *goja.Runtime, env Env, loop *timerLoop) error {
	// Node Buffer latin1 atob/btoa — length-correct.
	if err := rt.Set("atob", func(call goja.FunctionCall) goja.Value {
		s := ""
		if len(call.Arguments) > 0 {
			s = call.Arguments[0].String()
		}
		raw, err := base64.StdEncoding.DecodeString(s)
		if err != nil {
			pad := (4 - len(s)%4) % 4
			raw, err = base64.StdEncoding.DecodeString(s + strings.Repeat("=", pad))
			if err != nil {
				panic(rt.NewGoError(fmt.Errorf("atob: %w", err)))
			}
		}
		return rt.ToValue(string(raw))
	}); err != nil {
		return err
	}
	if err := rt.Set("btoa", func(call goja.FunctionCall) goja.Value {
		s := ""
		if len(call.Arguments) > 0 {
			s = call.Arguments[0].String()
		}
		runes := []rune(s)
		raw := make([]byte, len(runes))
		for i, r := range runes {
			raw[i] = byte(r)
		}
		return rt.ToValue(base64.StdEncoding.EncodeToString(raw))
	}); err != nil {
		return err
	}

	if _, err := rt.RunString(`
		function TextEncoder(){}
		TextEncoder.prototype.encode = function(s){
			s = String(s||"");
			var arr = new Uint8Array(s.length);
			for (var i=0;i<s.length;i++) arr[i]=s.charCodeAt(i)&0xff;
			return arr;
		};
		function TextDecoder(){}
		TextDecoder.prototype.decode = function(buf){
			if (!buf) return "";
			var a = buf.length !== undefined ? buf : (buf.buffer ? new Uint8Array(buf.buffer) : []);
			var s=""; for (var i=0;i<a.length;i++) s+=String.fromCharCode(a[i]&0xff); return s;
		};
		function URL(u, base){ this.href=String(u||""); this.pathname="/"; this.search=""; this.origin=""; this.host=""; this.hostname=""; this.protocol="https:"; this.toString=function(){return this.href}; }
		function URLSearchParams(init){ this._m={}; this.get=function(k){return this._m[k]||null}; this.set=function(k,v){this._m[k]=String(v)}; this.toString=function(){return "";}; }
		(function(){
			var origSet = Reflect.set;
			Reflect.set = function(target, propertyKey, value, receiver){
				if ((typeof target !== "object" && typeof target !== "function") || target == null) return true;
				var actualReceiver = (typeof receiver === "object" || typeof receiver === "function") && receiver != null ? receiver : target;
				try { return origSet.call(Reflect, target, propertyKey, value, actualReceiver); }
				catch (e) { return true; }
			};
		})();
		globalThis.Reflect = Reflect;
		var Buffer = {
			from: function(v, enc){
				if (enc === "base64") {
					var bin = atob(String(v||""));
					var arr = new Uint8Array(bin.length);
					for (var i=0;i<bin.length;i++) arr[i]=bin.charCodeAt(i)&0xff;
					arr.toString = function(e){
						if (e==="base64"){ var x=""; for (var i=0;i<this.length;i++) x+=String.fromCharCode(this[i]); return btoa(x); }
						var s=""; for (var j=0;j<this.length;j++) s+=String.fromCharCode(this[j]); return s;
					};
					return arr;
				}
				var s=String(v||"");
				var arr = new Uint8Array(s.length);
				for (var i=0;i<s.length;i++) arr[i]=s.charCodeAt(i)&0xff;
				arr.toString = function(e){
					if (e==="base64"){ var x=""; for (var i=0;i<this.length;i++) x+=String.fromCharCode(this[i]); return btoa(x); }
					var y=""; for (var j=0;j<this.length;j++) y+=String.fromCharCode(this[j]); return y;
				};
				return arr;
			}
		};
		// Node createAnyStub — only for DOM-like holes, not the whole window.
		function createAnyStub(seed) {
			seed = seed || {};
			var fn = function stubFn() { return undefined; };
			Object.assign(fn, seed);
			return new Proxy(fn, {
				get: function(target, prop, receiver) {
					if (Reflect.has(target, prop)) return Reflect.get(target, prop, receiver);
					if (prop === Symbol.toPrimitive) return function(){ return ""; };
					if (prop === Symbol.toStringTag) return "Function";
					if (prop === "length") return 0;
					if (prop === "name") return "stubFn";
					if (prop === "then") return undefined;
					if (prop === "toJSON") return function(){ return {}; };
					if (typeof prop === "symbol") return undefined;
					var nested = createAnyStub();
					Reflect.set(target, prop, nested, target);
					return nested;
				},
				apply: function() { return undefined; },
				construct: function() { return createAnyStub(); },
				set: function(target, prop, value) {
					Reflect.set(target, prop, value, target);
					return true;
				}
			});
		}
		function createDomStub(overrides) {
			overrides = overrides || {};
			var target = Object.assign({
				style: {},
				children: [],
				childNodes: [],
				appendChild: function(child){ this.children.push(child); this.childNodes.push(child); return child; },
				removeChild: function(child){ return child; },
				setAttribute: function(){},
				getAttribute: function(){ return null; },
				addEventListener: function(){},
				removeEventListener: function(){},
				postMessage: function(){},
				focus: function(){},
				blur: function(){},
				click: function(){},
				contentWindow: { postMessage: function(){} }
			}, overrides);
			return createAnyStub(target);
		}
		function createStorageStub() {
			var entries = new Map();
			return {
				get length(){ return entries.size; },
				clear: function(){ entries.clear(); },
				getItem: function(key){ return entries.has(String(key)) ? entries.get(String(key)) : null; },
				key: function(index){ return Array.from(entries.keys())[Number(index)] || null; },
				removeItem: function(key){ entries.delete(String(key)); },
				setItem: function(key, value){ entries.set(String(key), String(value)); }
			};
		}
	`); err != nil {
		return fmt.Errorf("sdk sandbox bootstrap: %w", err)
	}

	cryptoObj := rt.NewObject()
	_ = cryptoObj.Set("getRandomValues", func(call goja.FunctionCall) goja.Value {
		if len(call.Arguments) < 1 {
			return goja.Undefined()
		}
		arg := call.Arguments[0]
		obj := arg.ToObject(rt)
		if obj == nil {
			return arg
		}
		lv := obj.Get("length")
		if lv == nil || goja.IsUndefined(lv) {
			return arg
		}
		n := int(lv.ToInteger())
		if n < 0 {
			n = 0
		}
		if n > 65536 {
			n = 65536
		}
		buf := make([]byte, n)
		_, _ = rand.Read(buf)
		if setFn, ok := goja.AssertFunction(obj.Get("set")); ok {
			arr := make([]interface{}, n)
			for i := 0; i < n; i++ {
				arr[i] = int(buf[i])
			}
			_, _ = setFn(arg, rt.ToValue(arr))
		}
		for i := 0; i < n; i++ {
			_ = obj.Set(fmt.Sprintf("%d", i), rt.ToValue(int(buf[i])))
		}
		return arg
	})
	_ = cryptoObj.Set("randomUUID", func(call goja.FunctionCall) goja.Value {
		return rt.ToValue(uuid.NewString())
	})
	_ = cryptoObj.Set("subtle", rt.NewObject())
	_ = rt.Set("crypto", cryptoObj)

	console := rt.NewObject()
	_ = console.Set("log", func(call goja.FunctionCall) goja.Value { return goja.Undefined() })
	_ = console.Set("warn", func(call goja.FunctionCall) goja.Value { return goja.Undefined() })
	_ = console.Set("error", func(call goja.FunctionCall) goja.Value { return goja.Undefined() })
	_ = rt.Set("console", console)

	_ = rt.Set("setTimeout", loop.SetTimeout)
	_ = rt.Set("clearTimeout", loop.ClearTimeout)
	_ = rt.Set("setInterval", loop.SetTimeout)
	_ = rt.Set("clearInterval", loop.ClearTimeout)
	_ = rt.Set("requestIdleCallback", func(call goja.FunctionCall) goja.Value {
		if len(call.Arguments) >= 1 {
			if fn, ok := goja.AssertFunction(call.Arguments[0]); ok {
				deadline := rt.NewObject()
				_ = deadline.Set("timeRemaining", func(goja.FunctionCall) goja.Value { return rt.ToValue(1) })
				_ = deadline.Set("didTimeout", false)
				wrapped := rt.ToValue(func(call goja.FunctionCall) goja.Value {
					_, _ = fn(goja.Undefined(), deadline)
					return goja.Undefined()
				})
				return loop.SetTimeout(goja.FunctionCall{Arguments: []goja.Value{wrapped, rt.ToValue(0)}})
			}
		}
		return rt.ToValue(0)
	})
	_ = rt.Set("queueMicrotask", func(call goja.FunctionCall) goja.Value {
		if len(call.Arguments) >= 1 {
			if fn, ok := goja.AssertFunction(call.Arguments[0]); ok {
				_, _ = fn(goja.Undefined())
			}
		}
		return goja.Undefined()
	})
	_ = rt.Set("fetch", func(call goja.FunctionCall) goja.Value {
		// async-like thenable matching Node: fetch: async () => ({ok:false, json: async ()=>({})})
		obj := rt.NewObject()
		_ = obj.Set("ok", false)
		_ = obj.Set("status", 404)
		_ = obj.Set("json", func(goja.FunctionCall) goja.Value {
			th := rt.NewObject()
			_ = th.Set("then", func(call goja.FunctionCall) goja.Value {
				if len(call.Arguments) > 0 {
					if fn, ok := goja.AssertFunction(call.Arguments[0]); ok {
						_, _ = fn(goja.Undefined(), rt.ToValue(map[string]any{}))
					}
				}
				return call.This
			})
			return th
		})
		_ = obj.Set("text", func(goja.FunctionCall) goja.Value {
			th := rt.NewObject()
			_ = th.Set("then", func(call goja.FunctionCall) goja.Value {
				if len(call.Arguments) > 0 {
					if fn, ok := goja.AssertFunction(call.Arguments[0]); ok {
						_, _ = fn(goja.Undefined(), rt.ToValue(""))
					}
				}
				return call.This
			})
			return th
		})
		thenable := rt.NewObject()
		_ = thenable.Set("then", func(call goja.FunctionCall) goja.Value {
			if len(call.Arguments) > 0 {
				if fn, ok := goja.AssertFunction(call.Arguments[0]); ok {
					_, _ = fn(goja.Undefined(), obj)
				}
			}
			return call.This
		})
		return thenable
	})

	vendor := "Google Inc."
	if strings.Contains(env.UserAgent, "Firefox/") {
		vendor = ""
	}
	sw, sh := env.ScreenWidth, env.ScreenHeight
	if sw == 0 {
		sw = 1280
	}
	if sh == 0 {
		sh = 720
	}
	// Match Node oracle: innerHeight slightly smaller than screen.
	innerH := sh - 15
	if innerH < 1 {
		innerH = sh
	}

	_ = rt.Set("__sdkEnv", map[string]any{
		"userAgent": env.UserAgent, "language": env.Language, "languages": env.Languages,
		"hardwareConcurrency": env.HardwareConcurrency, "platform": "Win32", "vendor": vendor,
		"screenWidth": sw, "screenHeight": sh, "innerWidth": sw, "innerHeight": innerH,
		"outerWidth": sw, "outerHeight": sh, "devicePixelRatio": 1.0,
		"buildHash": env.BuildHash, "scriptSources": env.ScriptSources,
		"timeOrigin": env.TimeOrigin, "jsHeapSizeLimit": env.JSHeapSizeLimit,
		"deviceMemory": 8, "colorDepth": 24, "pixelDepth": 24,
	})
	_ = rt.Set("__sdkCrypto", cryptoObj)
	_ = rt.Set("__sdkSetTimeout", loop.SetTimeout)
	_ = rt.Set("__sdkClearTimeout", loop.ClearTimeout)
	_ = rt.Set("__sdkPerformanceNow", func(call goja.FunctionCall) goja.Value {
		return rt.ToValue(performanceNow())
	})

	// Mirror Node loadSdkTurnstileRunner context shape closely.
	// window is a PLAIN object (not full Proxy); only DOM nodes use createAnyStub.
	// window.top = {} (empty), matching Node.
	if _, err := rt.RunString(`
		(function(){
			var env = __sdkEnv;
			var scripts = (env.scriptSources || []).map(function(src){ return {src: src}; });
			var script0 = scripts.length ? scripts[0].src : "";
			var localStorage = createStorageStub();
			var sessionStorage = createStorageStub();
			var plugins = [{ name: "PDF Viewer", filename: "internal-pdf-viewer", description: "Portable Document Format" }];
			var mimeTypes = [{ type: "application/pdf", suffixes: "pdf", description: "Portable Document Format" }];
			var navigator = {
				userAgent: env.userAgent,
				language: env.language,
				languages: env.languages || [],
				hardwareConcurrency: env.hardwareConcurrency || 8,
				deviceMemory: env.deviceMemory || 8,
				connection: { effectiveType: "4g", rtt: 50, downlink: 10, saveData: false },
				cookieEnabled: true,
				webdriver: false,
				plugins: plugins,
				mimeTypes: mimeTypes,
				pdfViewerEnabled: true,
				platform: env.platform || "Win32",
				vendor: env.vendor || "",
				appCodeName: "Mozilla",
				appName: "Netscape",
				appVersion: env.userAgent,
				product: "Gecko",
				productSub: "20030107",
				maxTouchPoints: 0,
				onLine: true,
				requestMIDIAccess: function(){ return Promise.resolve({}); },
				permissions: {
					query: function(desc){
						return Promise.resolve({
							name: (desc && desc.name) || "",
							state: (desc && desc.name) === "notifications" ? "default" : "granted",
							onchange: null
						});
					}
				},
				mediaCapabilities: {
					decodingInfo: function(){ return Promise.resolve({supported:true, smooth:true, powerEfficient:true}); },
					encodingInfo: function(){ return Promise.resolve({supported:true, smooth:true, powerEfficient:true}); }
				},
				userAgentData: undefined // Firefox has no userAgentData
			};
			// Event bus so sessionObserver collector can attach keydown/pointer/etc.
			var __sdkListeners = Object.create(null);
			function __sdkAddEventListener(type, fn, opts) {
				if (!type || typeof fn !== "function") return;
				var k = String(type);
				if (!__sdkListeners[k]) __sdkListeners[k] = [];
				__sdkListeners[k].push(fn);
			}
			function __sdkRemoveEventListener(type, fn) {
				var k = String(type);
				var arr = __sdkListeners[k];
				if (!arr) return;
				__sdkListeners[k] = arr.filter(function(f){ return f !== fn; });
			}
			function __sdkDispatchEvent(type, eventObj) {
				var k = String(type);
				var arr = (__sdkListeners[k] || []).slice();
				var ev = eventObj || {};
				if (ev.type == null) ev.type = k;
				for (var i = 0; i < arr.length; i++) {
					try { arr[i].call((typeof windowRef!=='undefined'&&windowRef)||globalThis, ev); } catch (e) {}
				}
				return true;
			}
			var document = {
				scripts: scripts,
				currentScript: { src: script0 },
				head: createDomStub(),
				body: createDomStub(),
				documentElement: createDomStub({
					getAttribute: function(name){ return name === "data-build" ? env.buildHash : null; }
				}),
				createElement: function(){ return createDomStub(); },
				createElementNS: function(){ return createDomStub(); },
				getElementById: function(){ return null; },
				querySelector: function(){ return null; },
				querySelectorAll: function(){ return []; },
				cookie: "",
				addEventListener: __sdkAddEventListener,
				removeEventListener: __sdkRemoveEventListener,
				dispatchEvent: function(ev){ return __sdkDispatchEvent(ev && ev.type, ev); },
				readyState: "complete"
			};
			var location = {
				href: "https://sentinel.openai.com/backend-api/sentinel/frame.html?sv=" + env.buildHash,
				pathname: "/backend-api/sentinel/frame.html",
				search: "?sv=" + env.buildHash,
				origin: "https://sentinel.openai.com",
				host: "sentinel.openai.com",
				hostname: "sentinel.openai.com",
				protocol: "https:",
				hash: "",
				toString: function(){ return this.href; }
			};
			var performance = {
				now: function(){ return __sdkPerformanceNow(); },
				timeOrigin: env.timeOrigin || Date.now(),
				memory: { jsHeapSizeLimit: env.jsHeapSizeLimit || 0 }
			};
			var screen = {
				width: env.screenWidth, height: env.screenHeight,
				availWidth: env.screenWidth, availHeight: env.screenHeight,
				colorDepth: env.colorDepth || 24, pixelDepth: env.pixelDepth || 24
			};
			var chromeObject = {
				app: {
					isInstalled: false,
					InstallState: { DISABLED: "disabled", INSTALLED: "installed", NOT_INSTALLED: "not_installed" },
					RunningState: { CANNOT_RUN: "cannot_run", READY_TO_RUN: "ready_to_run", RUNNING: "running" }
				},
				runtime: {},
				loadTimes: function(){ return {}; },
				csi: function(){ return { startE: Date.now(), onloadT: Date.now(), pageT: 1, tran: 15 }; }
			};
			// PLAIN window object like Node windowRef (not full Proxy)
			var windowRef = {
				location: location,
				document: document,
				navigator: navigator,
				screen: screen,
				performance: performance,
				crypto: __sdkCrypto,
				requestIdleCallback: requestIdleCallback,
				localStorage: localStorage,
				sessionStorage: sessionStorage,
				innerWidth: env.innerWidth,
				innerHeight: env.innerHeight,
				outerWidth: env.outerWidth,
				outerHeight: env.outerHeight,
				devicePixelRatio: env.devicePixelRatio || 1,
				origin: "https://sentinel.openai.com",
				screenX: 0, screenY: 0, screenLeft: 0, screenTop: 0,
				scrollX: 0, scrollY: 0, pageXOffset: 0, pageYOffset: 0,
				name: "",
				navigation: {},
				history: { length: 1, state: null, back: function(){}, forward: function(){}, go: function(){}, pushState: function(){}, replaceState: function(){} },
				locationbar: {}, menubar: {}, personalbar: {}, scrollbars: {}, statusbar: {}, toolbar: {},
				status: "", closed: false, length: 0, opener: null, frameElement: null, external: {},
				visualViewport: { width: env.innerWidth, height: env.innerHeight, scale: env.devicePixelRatio || 1 },
				chrome: chromeObject,
				permissions: navigator.permissions,
				mediaCapabilities: navigator.mediaCapabilities,
				clientInformation: {
					userAgent: env.userAgent,
					language: env.language,
					languages: env.languages || [],
					hardwareConcurrency: env.hardwareConcurrency || 8,
					deviceMemory: env.deviceMemory || 8
				},
				ontouchstart: undefined,
				styleMedia: {},
				isSecureContext: true,
				Date: Date, Math: Math, JSON: JSON, Object: Object, Array: Array, Promise: Promise,
				String: String, Number: Number, Boolean: Boolean, Map: Map, Set: Set, WeakMap: WeakMap, WeakSet: WeakSet,
				Reflect: Reflect, Proxy: typeof Proxy !== "undefined" ? Proxy : undefined,
				Symbol: Symbol, Error: Error, TypeError: TypeError,
				Uint8Array: Uint8Array, Int8Array: Int8Array, Uint16Array: Uint16Array, Int16Array: Int16Array,
				Uint32Array: Uint32Array, Int32Array: Int32Array, Float32Array: Float32Array, Float64Array: Float64Array,
				ArrayBuffer: ArrayBuffer, DataView: typeof DataView !== "undefined" ? DataView : undefined,
				parseInt: parseInt, parseFloat: parseFloat, isNaN: isNaN, isFinite: isFinite,
				encodeURIComponent: encodeURIComponent, decodeURIComponent: decodeURIComponent,
				TextEncoder: TextEncoder, TextDecoder: TextDecoder, URL: URL, URLSearchParams: URLSearchParams,
				Buffer: Buffer, atob: atob, btoa: btoa, console: console, crypto: __sdkCrypto,
				addEventListener: __sdkAddEventListener,
				removeEventListener: __sdkRemoveEventListener,
				dispatchEvent: function(ev){ return __sdkDispatchEvent(ev && ev.type, ev); },
				postMessage: function(){},
				setTimeout: __sdkSetTimeout,
				clearTimeout: __sdkClearTimeout,
				setInterval: __sdkSetTimeout,
				clearInterval: __sdkClearTimeout,
				fetch: fetch
			};
			windowRef.window = windowRef;
			windowRef.self = windowRef;
			windowRef.parent = windowRef;
			windowRef.top = {}; // Node: top = {}

			// Node sandbox globals (vm context)
			globalThis.window = windowRef;
			globalThis.self = windowRef;
			globalThis.parent = windowRef;
			// Note: Node sets sandbox.globalThis = sandbox (the context), not window.
			globalThis.document = document;
			globalThis.navigator = navigator;
			globalThis.location = location;
			globalThis.screen = screen;
			globalThis.performance = performance;
			globalThis.localStorage = localStorage;
			globalThis.sessionStorage = sessionStorage;
			globalThis.createAnyStub = createAnyStub;
			globalThis.createDomStub = createDomStub;
		})();
	`); err != nil {
		return fmt.Errorf("sdk window install: %w", err)
	}
	return nil
}
