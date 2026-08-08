package sentinel

import (
	"context"
	"fmt"
	"strings"
	"time"

	"github.com/dop251/goja"
)

// ComputeSessionObserverSO produces the opaque `so` string for openai-sentinel-so-token.
//
// HAR (2026-07-17 create_account): header openai-sentinel-so-token = {so,c,id,flow}
// Live 2026-07-18 diagnosis:
//   - requirements.so has collector_dx + snapshot_dx
//   - collector_dx through turnstile _n currently returns <nil> / too-short
//   - snapshot_dx through the same path returns browser-class base64 (hundreds of bytes)
//     with the same binary shape as HAR gold so (control-byte framed payload)
//
// Order:
//  1. goja SDK hook: Et(collector side-effect) + Nt(snapshot_dx)  [true SO VM]
//  2. soft fallback: snapshot_dx via Turnstile _n path (browser-class, not gold)
//  3. soft fallback: collector_dx via Turnstile _n path
func ComputeSessionObserverSO(ctx context.Context, env Env, so *SessionObserver, requestP, responseToken, flow string) (value, source string, err error) {
	if so == nil {
		return "", "", nil
	}
	collector := strings.TrimSpace(so.CollectorDX)
	snapshot := strings.TrimSpace(so.SnapshotDX)
	if collector == "" && snapshot == "" {
		return "", "", fmt.Errorf("sentinel: so collector_dx and snapshot_dx empty")
	}

	if v, e := computeSOViaSDK(ctx, env, so, requestP, responseToken, flow); e == nil && soLooksValid(v) {
		return v, "sdk", nil
	}

	// Prefer snapshot_dx: live wire shows it yields valid browser-class material.
	if snapshot != "" {
		v, src, e := ComputeTurnstileDxFull(ctx, env, snapshot, requestP, responseToken, nil)
		if e == nil && soLooksValid(v) {
			if src == "" {
				src = "vm"
			}
			return v, "snapshot:" + src, nil
		}
		if e != nil && collector == "" {
			return "", "", fmt.Errorf("so snapshot: %w", e)
		}
	}

	if collector != "" {
		v, src, e := ComputeTurnstileDxFull(ctx, env, collector, requestP, responseToken, nil)
		if e != nil {
			return "", "", fmt.Errorf("so collector: %w", e)
		}
		if !soLooksValid(v) {
			return "", "", fmt.Errorf("sentinel: so result invalid len=%d", len(v))
		}
		if src == "" {
			src = "vm"
		}
		return v, "collector:" + src, nil
	}

	return "", "", fmt.Errorf("sentinel: so compute failed")
}

func soLooksValid(v string) bool {
	v = strings.TrimSpace(v)
	// HAR gold so is 524 chars; live snapshot path yields ~300-600. Reject short/null.
	if len(v) < 64 {
		return false
	}
	switch v {
	case "null", "undefined", "bnVsbA==", "[object Object]", "PG5pbD4=":
		return false
	}
	if dec := tryDecodeBase64UTF8(v); dec == "null" || dec == "<nil>" || looksLikeEncodedError(dec) {
		return false
	}
	return true
}

func computeSOViaSDK(ctx context.Context, env Env, so *SessionObserver, requestP, responseToken, flow string) (string, error) {
	select {
	case <-ctx.Done():
		return "", ctx.Err()
	default:
	}
	src, _, err := LoadPinnedSDK()
	if err != nil {
		return "", err
	}
	patched, err := PatchSDKForSO(src)
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
		return "", fmt.Errorf("so sdk eval: %w", err)
	}
	loop.Pump(ctx, 500*time.Millisecond)

	fn, err := rt.RunString(`
(function(){
  var sdk = globalThis.__codexSentinelSdk || (typeof SentinelSDK !== 'undefined' ? SentinelSDK : null);
  if (!sdk) return null;
  if (typeof sdk.__codexSessionObserverSO === 'function') return sdk.__codexSessionObserverSO.bind(sdk);
  if (typeof sdk.sessionObserverToken === 'function') return sdk.sessionObserverToken.bind(sdk);
  return null;
})()
`)
	if err != nil {
		return "", fmt.Errorf("so sdk locate: %w", err)
	}
	if goja.IsUndefined(fn) || goja.IsNull(fn) {
		return "", fmt.Errorf("so sdk: sessionObserverToken not available")
	}
	callable, ok := goja.AssertFunction(fn)
	if !ok {
		return "", fmt.Errorf("so sdk: not callable")
	}

	reqObj := rt.NewObject()
	if strings.TrimSpace(responseToken) != "" {
		_ = reqObj.Set("token", responseToken)
	}
	soObj := rt.NewObject()
	_ = soObj.Set("required", so.Required)
	if so.CollectorDX != "" {
		_ = soObj.Set("collector_dx", so.CollectorDX)
	}
	if so.SnapshotDX != "" {
		_ = soObj.Set("snapshot_dx", so.SnapshotDX)
	}
	_ = reqObj.Set("so", soObj)
	_ = reqObj.Set("turnstile", rt.NewObject())
	powObj := rt.NewObject()
	_ = powObj.Set("required", false)
	_ = reqObj.Set("proofofwork", powObj)

	isInjected, _ := rt.RunString(`(function(){ var s=globalThis.__codexSentinelSdk; return !!(s && s.__codexSessionObserverSO); })()`)
	var val goja.Value
	if isInjected != nil && isInjected.ToBoolean() {
		val, err = callable(goja.Undefined(), reqObj, rt.ToValue(requestP), rt.ToValue(flow))
	} else {
		opt := rt.NewObject()
		_ = opt.Set("flow", flow)
		val, err = callable(goja.Undefined(), opt)
	}
	if err != nil {
		return "", fmt.Errorf("so sdk call: %w", err)
	}
	val, err = awaitGojaValue(ctx, rt, loop, val)
	if err != nil {
		return "", err
	}
	if goja.IsUndefined(val) || goja.IsNull(val) {
		return "", fmt.Errorf("so sdk returned null")
	}
	if exp := val.Export(); exp != nil {
		if m, ok := exp.(map[string]any); ok {
			if s, ok := m["so"].(string); ok && strings.TrimSpace(s) != "" {
				return s, nil
			}
		}
	}
	out := strings.TrimSpace(val.String())
	if out == "" || out == "null" || out == "[object Object]" {
		js, jerr := rt.RunString(`(function(v){ try { if (v && typeof v === 'object' && v.so) return String(v.so); return ''; } catch(e){ return ''; } })`)
		if jerr == nil {
			if cf, ok := goja.AssertFunction(js); ok {
				if vv, e2 := cf(goja.Undefined(), val); e2 == nil {
					out = strings.TrimSpace(vv.String())
				}
			}
		}
	}
	if out == "" || out == "null" {
		return "", fmt.Errorf("so sdk empty result")
	}
	return out, nil
}

// PatchSDKForSO extends the Turnstile patch with __codexSessionObserverSO.
//
// SDK truth (sdk.js sessionObserverToken):
//
//	se(requirements) → Et(requirements) kicks collector_dx VM with D-key side-effect
//	sessionObserverToken → await Nt(requirements.so.snapshot_dx)
//
// Nt/jt is a separate bytecode VM from Turnstile _n. Using _n here was wrong
// and produced short/null-shaped material.
func PatchSDKForSO(source string) (string, error) {
	base, err := PatchSDK(source)
	if err != nil {
		return "", err
	}
	hook := "t.__codexTurnstileDx=function(requirements,key,dx){D(requirements,key);return _n(requirements,dx)},t.init=we,t.sessionObserverToken=async function(t){"
	inject := "t.__codexTurnstileDx=function(requirements,key,dx){D(requirements,key);return _n(requirements,dx)}," +
		"t.__codexSessionObserverSO=async function(requirements,key,flow){" +
		// Await collector with key, fire synthetic traffic into __oai_so_* listeners,
		// then Nt(snapshot) — browser sessionObserverToken contract.
		"try{D(requirements,key);}catch(e){}" +
		"async function awaitMaybe(r){if(r&&typeof r.then==='function'){try{return await r;}catch(e){return null;}}return r;}" +
		"function sleep(ms){return new Promise(function(r){setTimeout(r,ms);});}" +
		"function fireSOTraffic(){" +
		"var w=typeof window!=='undefined'?window:globalThis;" +
		"var doc=typeof document!=='undefined'?document:w;" +
		"function emit(type,ev){" +
		"try{if(w&&typeof w.dispatchEvent==='function')w.dispatchEvent(Object.assign({type:type},ev||{}));}catch(e){}" +
		"try{if(doc&&typeof doc.dispatchEvent==='function')doc.dispatchEvent(Object.assign({type:type},ev||{}));}catch(e){}" +
		"}" +
		"var x=120,y=180;" +
		"for(var i=0;i<12;i++){x+=7+i;y+=3+(i%4);emit('pointermove',{clientX:x,clientY:y,type:'pointermove'});}" +
		"emit('click',{clientX:x,clientY:y,type:'click',button:0});" +
		"emit('scroll',{type:'scroll'});" +
		"emit('wheel',{type:'wheel',deltaY:40,clientX:x,clientY:y});" +
		"var keys=['a','b','c','Shift','Backspace','1','2'];" +
		"for(var j=0;j<keys.length;j++){emit('keydown',{type:'keydown',key:keys[j],ctrlKey:false,metaKey:false,altKey:false});}" +
		"emit('paste',{type:'paste'});" +
		"}" +
		"var so=requirements&&requirements.so;" +
		"var col=so&&so.collector_dx;" +
		"var snap=so&&so.snapshot_dx;" +
		"var k=null;try{k=$(requirements);}catch(e){}" +
		"if(!k)k=key;" +
		"if(col&&typeof Ot==='function'&&typeof jt==='function'){" +
		"try{await awaitMaybe(Ot(function(){return jt(col,k);}));}catch(e){}" +
		"}else if(typeof Et==='function'){" +
		"try{Et(requirements);await sleep(30);}catch(e){}" +
		"}" +
		"try{fireSOTraffic();}catch(e){}" +
		"await sleep(20);" +
		"if(snap&&typeof Nt==='function'){" +
		"try{var r1=await awaitMaybe(Nt(snap));if(r1)return r1;}catch(e){}" +
		"}" +
		"if(col&&typeof Ot==='function'&&typeof jt==='function'){" +
		"try{await awaitMaybe(Ot(function(){return jt(col,k);}));fireSOTraffic();await sleep(20);" +
		"if(snap&&typeof Nt==='function'){var r2=await awaitMaybe(Nt(snap));if(r2)return r2;}" +
		"}catch(e){}" +
		"}" +
		"try{var opts={flow:flow||''};" +
		"if(typeof re==='function'){var st=re(opts);st.cachedSOChatReq=requirements;st.sessionObserverCollectorActive=true;}" +
		"var o=await t.sessionObserverToken(opts);" +
		"if(o&&typeof o==='object'&&o.so)return o.so;if(typeof o==='string')return o;}catch(e){}" +
		"return null;},t.init=we,t.sessionObserverToken=async function(t){"
	if !strings.Contains(base, hook) {
		return base, nil
	}
	out := strings.Replace(base, hook, inject, 1)
	if out == base {
		return "", &Error{Code: CodeSDKHookMissing, Message: "sdk.js SO patch failed"}
	}
	return out, nil
}
