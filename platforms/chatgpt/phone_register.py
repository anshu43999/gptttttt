"""ChatGPT phone-first registration. DOM verified 2026-06-13."""
from __future__ import annotations
import json, time
from dataclasses import dataclass
from typing import Callable
from .constants import PLATFORM_LOGIN_ENTRY
from .browser_register import _browser_fetch, _browser_pause, _mask_phone_number, _parse_phone_country_and_local

PHONE_BTN = 'button:has-text("Continue with phone")'
TEL_INPUT = 'input[type="tel"]'
SUBMIT = 'button[type="submit"]'
PWD_INPUT = 'input[type="password"]'
SMS_INPUT = 'input[name="code"]'

@dataclass
class PhoneResult:
    success: bool
    access_token: str = ""
    account_id: str = ""
    email: str = ""
    plan_type: str = ""
    phone_number: str = ""
    error: str = ""
    status: str = ""
    failed_step: str = ""
    failure_reason: str = ""
    retryable: bool = False
    next_action: str = ""

@dataclass
class SessionTokenFetchResult:
    access_token: str = ""
    status: str = "SESSION_TOKEN_MISSING"
    http_status: int = 0
    error_category: str = "SESSION_TOKEN_MISSING"
    failure_reason: str = ""
    retryable: bool = True

    @property
    def success(self) -> bool:
        return bool(self.access_token)

def _has_visible_tel_input(page) -> bool:
    try:
        return bool(page.evaluate("""()=>{
            const i = document.querySelector('#phoneNumberInput, input[name="phoneNumberInput"], input[type="tel"]');
            return !!(i && (i.offsetWidth || i.offsetHeight || i.getClientRects().length));
        }"""))
    except Exception:
        return False

def _open_phone_entry(page) -> None:
    if _has_visible_tel_input(page):
        return
    for selector in (
        'button:has-text("Continue with phone")',
        'button:has-text("phone")',
    ):
        try:
            page.locator(selector).first.click(timeout=8000)
            time.sleep(2)
            if _has_visible_tel_input(page):
                return
        except Exception:
            pass
    clicked = page.evaluate("""()=>{
        const buttons = Array.from(document.querySelectorAll('button'));
        const b = buttons.find(x => /continue\s+with\s+phone/i.test((x.innerText || x.textContent || '').trim()));
        if (!b) return false;
        b.click();
        return true;
    }""")
    if not clicked:
        raise RuntimeError("Continue with phone button not found")
    deadline = time.time() + 12
    while time.time() < deadline:
        if _has_visible_tel_input(page):
            return
        time.sleep(0.5)
    raise RuntimeError("phone input did not appear after clicking Continue with phone")

def _click_country(page, country_name, country_code):
    target = f"{country_name} +({country_code})"
    if _has_visible_tel_input(page):
        return
    current = page.evaluate(
        """({countryName, countryCode}) => {
            const buttons = Array.from(document.querySelectorAll('button[type="button"],button[role="combobox"]'));
            const current = buttons.map(b => (b.textContent || '').replace(/\s+/g, ' ').trim()).find(t => t.includes('+') && (t.includes(countryName) || t.includes(countryCode))) || '';
            return current;
        }""",
        {"countryName": country_name, "countryCode": country_code},
    )
    if current and country_name in current and country_code in current:
        return

    for _ in range(5):
        r = page.evaluate("""()=>{const b=Array.from(document.querySelectorAll('button[type="button"],button[role="combobox"]')).find(x=>{const t=x.textContent||'';return t.includes('+')&&t.includes('(')});if(b){b.click();return'OK'}return'NO'}""")
        if r == 'OK': break
        time.sleep(1.5)
    else:
        raise RuntimeError("country btn not found after retries")
    time.sleep(1.5)
    r2 = page.evaluate("""({countryName, countryCode})=>{
        const options = Array.from(document.querySelectorAll('[role="option"]'));
        for(const x of options){
            const t=(x.textContent||'').replace(/\s+/g,' ').trim();
            if(t.includes(countryName) && (t.includes('+(' + countryCode + ')') || t.includes('+' + countryCode))){x.click();return'OK'}
        }
        document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape', bubbles:true}));
        return'NO'
    }""", {"countryName": country_name, "countryCode": country_code})
    if r2 != 'OK': raise RuntimeError(f"option '{target}' not found")
    time.sleep(0.5)

def _fill_tel(page, val):
    selector = '#phoneNumberInput, input[name="phoneNumberInput"], input[type="tel"]'
    page.locator(selector).first.wait_for(state="visible", timeout=15000)
    page.evaluate("""(v)=>{const i=document.querySelector('#phoneNumberInput, input[name="phoneNumberInput"], input[type="tel"]');const s=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;s.call(i,v);i.dispatchEvent(new Event('input',{bubbles:true}));i.dispatchEvent(new Event('change',{bubbles:true}))}""", val)
def _wait_redirect(page, cur_url, timeout=120):
    dl = time.time() + timeout
    while time.time() < dl:
        u = str(page.url or "")
        if u != cur_url and "challenge" not in u.lower(): return u
        try:
            if page.evaluate("!!(document.querySelector('.cf-turnstile,iframe[src*=\"turnstile\"]'))"):
                time.sleep(5); continue
        except Exception: pass
        time.sleep(2)
    return ""

def _session_token_failure_category(http_status: int, text: str) -> tuple[str, bool]:
    lowered = str(text or "").lower()
    if http_status == 0:
        return "SESSION_FETCH_ERROR", True
    if "csrf" in lowered:
        return "SESSION_CSRF_FAILED", True
    if http_status in (401, 403):
        return "SESSION_BROWSER_AUTH_FAILED", True
    if http_status in (429, 500, 502, 503, 504):
        return "SESSION_HTTP_RETRYABLE", True
    if http_status >= 400:
        return "SESSION_HTTP_FAILED", False
    return "SESSION_TOKEN_MISSING", True

def _fetch_access_token(page, *, attempts: int = 30, delay: float = 2.0) -> SessionTokenFetchResult:
    """Get access token from chatgpt.com/api/auth/session with last failure detail."""
    last = SessionTokenFetchResult()
    for _ in range(attempts):
        try:
            result = _browser_fetch(page, "https://chatgpt.com/api/auth/session", method="GET", headers={"Accept": "application/json"})
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except Exception:
                    result = {"ok": False, "status": 0, "text": result, "data": None}
            if isinstance(result, dict):
                http_status = int(result.get("status") or 0)
                data = result.get("data") if isinstance(result.get("data"), dict) else result
                token = (data.get("accessToken") or data.get("access_token") or "") if isinstance(data, dict) else ""
                if token:
                    return SessionTokenFetchResult(access_token=str(token), status="SESSION_TOKEN_OK", http_status=http_status, error_category="", failure_reason="", retryable=False)
                text = str(result.get("text") or "")
                if text:
                    try:
                        parsed = json.loads(text)
                        token = parsed.get("accessToken") or parsed.get("access_token")
                        if token:
                            return SessionTokenFetchResult(access_token=str(token), status="SESSION_TOKEN_OK", http_status=http_status, error_category="", failure_reason="", retryable=False)
                    except Exception:
                        pass
                category, retryable = _session_token_failure_category(http_status, text)
                reason = text[:300] if text else f"/api/auth/session returned status {http_status} without accessToken"
                last = SessionTokenFetchResult(status=category, http_status=http_status, error_category=category, failure_reason=reason, retryable=retryable)
            else:
                last = SessionTokenFetchResult(status="SESSION_FETCH_ERROR", http_status=0, error_category="SESSION_FETCH_ERROR", failure_reason=f"unexpected fetch result: {type(result).__name__}", retryable=True)
        except Exception as exc:
            last = SessionTokenFetchResult(status="SESSION_FETCH_ERROR", http_status=0, error_category="SESSION_FETCH_ERROR", failure_reason=str(exc), retryable=True)
        time.sleep(delay)
    return last

def _phone_result_from_session_failure(fetch_result: SessionTokenFetchResult) -> PhoneResult:
    reason = fetch_result.failure_reason or fetch_result.error_category or "no session"
    return PhoneResult(
        success=False,
        error=f"no session: {fetch_result.error_category or fetch_result.status}",
        status=fetch_result.status,
        failed_step="fetch_session_token",
        failure_reason=reason,
        retryable=fetch_result.retryable,
        next_action="retry_browser_auth" if fetch_result.retryable else "inspect_session_response",
    )

def phone_registration_flow(page, phone_number, password, *, country_code="57", country_name="Colombia", headed=False, log=None) -> PhoneResult:
    if log is None: log = print
    log(f"=== phone register: {_mask_phone_number(phone_number)} / {country_name} ===")
    _, local, _ = _parse_phone_country_and_local(phone_number)
    if not local: local = phone_number.lstrip("+").lstrip(country_code)
    try:
        try: page.evaluate("""()=>{document.querySelectorAll('button').forEach(b=>{if((b.textContent||'').includes('Accept'))b.click()})}"""); time.sleep(1)
        except Exception: pass
        page.goto(PLATFORM_LOGIN_ENTRY, wait_until="domcontentloaded", timeout=30000)
        cur = str(page.url or "")
        _browser_pause(page, headed=headed)
        log("1. phone entry")
        _open_phone_entry(page); time.sleep(1)
        log(f"2. country: {country_name} +({country_code})")
        try: _click_country(page, country_name, country_code); time.sleep(2)
        except Exception as exc:
            log(f"  country skip: {exc}")
            try: page.keyboard.press("Escape")
            except Exception: pass
            time.sleep(0.5)
        log(f"3. fill: {local}")
        _fill_tel(page, local); time.sleep(0.5)
        log("4. submit")
        page.locator(SUBMIT).first.click(timeout=5000)
        log("5. waiting...")
        time.sleep(2)
        try:
            body = page.evaluate("()=>document.body.innerText.substring(0,500)")
            if "already registered" in body.lower() or "incorrect phone" in body.lower():
                return PhoneResult(success=False, error="PHONE_ALREADY_REGISTERED")
        except Exception: pass
        new = _wait_redirect(page, cur, timeout=120)
        if not new: return PhoneResult(success=False, error="timeout")
        if "password" in new.lower() or "create-account" in new.lower():
            log("6. password")
            page.locator(PWD_INPUT).first.wait_for(state="visible", timeout=15000)
            page.locator(PWD_INPUT).first.fill(password); time.sleep(0.3)
            log("7. submit pwd")
            page.locator(SUBMIT).first.click(timeout=5000); time.sleep(3)
        log("8. wait SMS")
        try: page.locator(SMS_INPUT).first.wait_for(state="visible", timeout=60000)
        except Exception: pass
        return PhoneResult(success=False, error="AWAITING_SMS_CODE", phone_number=phone_number)
    except Exception as e:
        try:
            from pathlib import Path
            snapshot = {
                "url": page.url,
                "title": page.title(),
                "text": page.evaluate("() => (document.body?.innerText || '').slice(0, 3000)"),
                "inputs": page.evaluate("""() => Array.from(document.querySelectorAll('input')).map((i, idx) => ({idx, type:i.type, name:i.name, id:i.id, placeholder:i.placeholder, aria:i.getAttribute('aria-label'), visible:!!(i.offsetWidth || i.offsetHeight || i.getClientRects().length), outer:i.outerHTML.slice(0,300)}))"""),
                "buttons": page.evaluate("""() => Array.from(document.querySelectorAll('button')).slice(0,30).map((b, idx) => ({idx, text:(b.innerText||b.textContent||'').trim().slice(0,200), type:b.type, aria:b.getAttribute('aria-label'), visible:!!(b.offsetWidth || b.offsetHeight || b.getClientRects().length), outer:b.outerHTML.slice(0,300)}))"""),
            }
            Path("tmp/phone_register_error_snapshot.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        log(f"  err: {e}")
        return PhoneResult(success=False, error=str(e))

def continue_after_sms(page, code, log=None) -> PhoneResult:
    if log is None: log = print
    try:
        log(f"SMS code: {code}")
        page.locator(SMS_INPUT).first.fill(str(code)); time.sleep(0.3)
        try:
            page.locator(SUBMIT).first.click(timeout=8000)
        except Exception as exc:
            log(f"  SMS 普通点击失败，改用 JS 提交: {exc}")
            submitted = page.evaluate(
                """
                () => {
                    const button = document.querySelector('button[type="submit"]');
                    if (button) { button.click(); return 'button.click'; }
                    const form = document.querySelector('form');
                    if (form) {
                        if (form.requestSubmit) form.requestSubmit();
                        else form.submit();
                        return 'form.submit';
                    }
                    return '';
                }
                """
            )
            if not submitted:
                raise

        at = ""
        fetch_result = SessionTokenFetchResult()
        last_url = ""
        last_body = ""
        landed_frontend = False
        deadline = time.time() + 120
        while time.time() < deadline:
            try:
                url = str(page.url or "")
                body = page.evaluate("()=>(document.body?.innerText||'').substring(0,1000)")
            except Exception as exc:
                log(f"  post-SMS state read failed: {exc}")
                time.sleep(2)
                continue
            if url != last_url or body[:120] != last_body[:120]:
                log(f"  post-SMS state: url={url[:100]} body_start={body[:120]}")
                last_url = url
                last_body = body

            body_lower = body.lower()
            if any(marker in body_lower for marker in (
                "incorrect code",
                "invalid code",
                "wrong code",
                "verification code is invalid",
                "認証コードが正しくありません",
                "コードが正しくありません",
                "验证码错误",
                "验证码无效",
            )):
                return PhoneResult(success=False, error=f"invalid sms code: {body[:200]}", failed_step="sms_code", failure_reason="invalid_code", retryable=True)

            if "contact-verification" in url or any(marker in body_lower for marker in (
                "verify your phone",
                "smartphone",
                "スマートフォンを確認",
                "確認してください",
                "verification code",
            )):
                time.sleep(0.5)
                continue

            if "How old are you" in body or "Full name" in body or "creating account" in body.lower() or "about-you" in url or "年齢を確認" in body or "氏名" in body or "生年月日" in body:
                log("  filling about_you...")
                try:
                    from .browser_register import _submit_about_you_via_page
                    about_resp = _submit_about_you_via_page(page, log)
                    log(f"  about_you submit status={about_resp.get('status', 0)} url={str(about_resp.get('url', ''))[:100]}")
                    if not about_resp.get("ok"):
                        return PhoneResult(success=False, error=f"about_you failed: {(about_resp.get('text') or '')[:200]}")
                except Exception as exc:
                    return PhoneResult(success=False, error=f"about_you failed: {exc}")
                time.sleep(2)
                continue

            fetch_result = _fetch_access_token(page, attempts=1, delay=0)
            if fetch_result.success:
                at = fetch_result.access_token
                break

            if "chatgpt.com/auth/login" in url:
                try:
                    log("  chatgpt auth/login landing seen; opening ChatGPT root before session fetch")
                    page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=20000)
                    landed_frontend = True
                except Exception as exc:
                    log(f"  ChatGPT root landing failed: {exc}")
                time.sleep(2)
                continue

            if "chatgpt.com" in url and "/api/auth/session" not in url:
                landed_frontend = True

            if landed_frontend:
                time.sleep(2)
                continue

            time.sleep(2)

        if not at:
            log("  fetching session via same-page fetch after landing wait...")
            fetch_result = _fetch_access_token(page, attempts=20, delay=2)
            at = fetch_result.access_token

        if not at:
            return _phone_result_from_session_failure(fetch_result)

        # Decode JWT for account info
        try:
            import base64
            payload = at.split(".")[1]
            payload += "=" * (4 - len(payload) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload))
            aid = claims.get("https://api.openai.com/auth",{}).get("chatgpt_account_id","") or claims.get("sub","")
            email = claims.get("https://api.openai.com/profile",{}).get("email","") or claims.get("email","")
            plan = claims.get("https://api.openai.com/auth",{}).get("chatgpt_plan_type","")
            log(f"  OK! id={aid[:16]} plan={plan} email={email}")
            return PhoneResult(success=True, access_token=at, account_id=aid, email=email, plan_type=plan)
        except Exception:
            log(f"  OK! token={at[:30]}...")
            return PhoneResult(success=True, access_token=at)

    except Exception as e:
        return PhoneResult(success=False, error=str(e))

def wait_for_sms_and_continue(page, sms_provider, activation_id, *, timeout=120, poll_interval=3.0, log=None) -> PhoneResult:
    if log is None: log = print
    dl = time.time() + timeout
    while time.time() < dl:
        st = sms_provider.get_status(activation_id)
        code = st.get("code", "") if isinstance(st, dict) else ""
        if code and str(code).strip():
            log(f"  SMS: {code}")
            return continue_after_sms(page, str(code).strip(), log=log)
        time.sleep(poll_interval)
    return PhoneResult(success=False, error="SMS timeout")
