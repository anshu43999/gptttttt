from __future__ import annotations

import re
import time
import uuid
from typing import Any, Callable

from core.browser.session import BrowserSession, extract_chatgpt_access_token
from platforms.chatgpt.browser_register import (
    CHATGPT_APP,
    OPENAI_AUTH,
    _browser_authorize,
    _derive_registration_state_from_page,
    _extract_flow_state,
    _get_browser_csrf_token,
    _get_cookies,
    _handle_post_signup_onboarding,
    _is_about_you,
    _is_email_otp,
    _is_registration_complete,
    _random_chrome_ua,
    _start_browser_signin,
    _submit_about_you_via_page,
    _validate_browser_email_otp,
    _submit_otp_via_page,
    _submit_password_via_page,
    _click_password_continue_if_available,
    _click_first,
)

BLOCKED_DOMAINS = {
    "ab.chatgpt.com",
    "browser-intake-datadoghq.com",
}

BLOCKED_PATH_PREFIXES = (
    "/ces/v1/rgstr",
    "/backend-api/sentinel/sdk.js",
)


def _block_crash_domains(route):
    url = str(route.request.url or "")
    hostname = ""
    path = ""
    try:
        from urllib.parse import urlsplit
        parsed = urlsplit(url)
        hostname = str(parsed.hostname or "").lower()
        path = str(parsed.path or "")
    except Exception:
        pass
    if hostname in BLOCKED_DOMAINS:
        route.abort("blockedbyclient")
        return
    if path.startswith(BLOCKED_PATH_PREFIXES):
        route.abort("blockedbyclient")
        return
    route.fallback()


def _is_wrong_email_otp_text(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(
        marker in lowered
        for marker in (
            "wrong_email_otp_code",
            "wrong code",
            "incorrect code",
            "invalid code",
            "invalid otp",
            "不正確なコード",
            "コードが正しくありません",
            "コードが無効",
            "验证码错误",
            "验证码无效",
        )
    )


def _click_email_otp_resend(page) -> bool:
    try:
        _click_first(page, [
            'button:has-text("メールを再送信する")',
            'button:has-text("再送信")',
            'button:has-text("Resend")',
            'button:has-text("resend")',
            'button:has-text("Resend code")',
            'button:has-text("Resend email")',
            'button:has-text("重新发送")',
            'a:has-text("メールを再送信する")',
            'a:has-text("再送信")',
            'a:has-text("Resend")',
        ], timeout=3)
        return True
    except Exception:
        return False



class FastEmailRegistrationFlow:
    """Lean email registration flow for Patchright/Playwright browser contexts.

    This intentionally keeps the current project's mailbox/proxy/storage contracts and only
    replaces the heavy Camoufox registration loop. The loop mirrors the simpler flow from
    openai-register-paylink-ui: open signup, fill password, validate email OTP, submit
    about-you, then read /api/auth/session.
    """

    def __init__(self, *, log_fn: Callable[[str], None] | None = None):
        self.log = log_fn or print

    def run(
        self,
        session: BrowserSession,
        *,
        email: str,
        password: str,
        otp_callback: Callable[[], str],
        timeout: int = 600,
    ) -> dict[str, Any]:
        page = session.page
        if page is None:
            raise RuntimeError("browser session has no page")

        if False:
            try:
                page.route("**/*", _block_crash_domains)
            except Exception:
                pass
        device_id = session.device_id or str(uuid.uuid4())
        try:
            user_agent = str(page.evaluate("() => navigator.userAgent") or "").strip() or _random_chrome_ua()
        except Exception:
            user_agent = _random_chrome_ua()

        self.log("快速邮箱注册: 打开 ChatGPT 注册入口")
        self._goto_chatgpt_entry(page)
        csrf_token = _get_browser_csrf_token(page)
        if not csrf_token:
            raise RuntimeError("快速邮箱注册无法获取 ChatGPT CSRF token")
        signin_url = _start_browser_signin(page, email, device_id, csrf_token, screen_hint="signup")
        if not signin_url:
            raise RuntimeError("快速邮箱注册无法获取 OpenAI authorize URL")
        _browser_authorize(page, signin_url, self.log)

        deadline = time.time() + max(60, int(timeout or 600))
        state = _derive_registration_state_from_page(page)
        seen: dict[str, int] = {}
        otp_submitted = False
        while time.time() < deadline:
            current_url = str(page.url or "")
            state = self._state_from_page(page, state)
            signature = "|".join([
                str(state.get("page_type") or ""),
                str(state.get("current_url") or current_url),
                str(state.get("continue_url") or ""),
            ])
            seen[signature] = seen.get(signature, 0) + 1
            blocked_reason = self._blocked_authorize_reason(page, state)
            if blocked_reason and seen[signature] > 2:
                raise RuntimeError(blocked_reason)
            self.log(f"快速邮箱注册状态: page={state.get('page_type') or '-'} seen={seen[signature]} url={current_url[:100]}")
            if seen[signature] > 6 and str(state.get("page_type") or "") not in {"email_otp_verification", "create_account_password", "login_password", "about_you"}:
                raise RuntimeError(f"快速邮箱注册状态卡住: page={state.get('page_type') or '-'}")

            on_chatgpt = self._is_chatgpt_page(current_url)
            if (on_chatgpt and _is_registration_complete(state)) or self._has_chatgpt_session(page):
                _handle_post_signup_onboarding(page, self.log)
                return self._extract_session(session)

            page_type = str(state.get("page_type") or "")
            if page_type in {"create_account_password", "password", "login_password"} or self._has_password_input(page):
                if page_type == "login_password":
                    self.log("快速邮箱注册: 落在登录密码页，交由密码提交函数自行恢复")
                self.log("快速邮箱注册: 提交密码")
                resp = _submit_password_via_page(page, password, self.log)
                if not resp.get("ok"):
                    raise RuntimeError(f"快速邮箱注册密码提交失败: {str(resp.get('text') or '')[:300]}")
                state = _extract_flow_state(resp.get("data"), resp.get("url", page.url))
                otp_submitted = False
                continue

            if _is_about_you(state) or self._has_about_you(page):
                self.log("快速邮箱注册: 提交 about-you")
                resp = _submit_about_you_via_page(page, self.log)
                if resp is None:
                    raise RuntimeError("快速邮箱注册 about-you 提交无返回结果，未验证成功")
                if not resp.get("ok"):
                    raise RuntimeError(f"快速邮箱注册 about-you 提交失败: {str(resp.get('text') or '')[:300]}")
                state = _extract_flow_state(resp.get("data"), resp.get("url", page.url))
                otp_submitted = False
                continue
            if _is_email_otp(state) and _click_password_continue_if_available(page, self.log, context="快速邮箱注册邮箱页"):
                state = _derive_registration_state_from_page(page)
                continue


            if _is_email_otp(state) or self._has_otp_input(page):
                if otp_submitted:
                    time.sleep(2)
                    state = _derive_registration_state_from_page(page)
                    continue
                self.log("快速邮箱注册: 等待邮箱验证码")
                code = self._wait_email_code_with_resend(page, otp_callback)
                if not code:
                    raise RuntimeError("快速邮箱注册未获取到邮箱验证码")
                otp_mode = str(getattr(session, "config", {}).get("email_otp_submit_mode") or "dom_first").strip().lower()
                if otp_mode in {"dom_first", "ui", "page"}:
                    self.log("快速邮箱注册: 页面提交邮箱验证码")
                    resp = _submit_otp_via_page(page, code, self.log, device_id=device_id, user_agent=user_agent)
                    if not resp.get("ok"):
                        text = str(resp.get("text") or "")
                        lowered = text.lower()
                        if "missing" in lowered or "未找到" in text or "验证码页填写失败" in text:
                            self.log(f"快速邮箱注册页面验证码提交失败，回退接口提交: {text[:160]}")
                            resp = _validate_browser_email_otp(page, code, device_id, user_agent, str(page.url or ""))
                else:
                    resp = _validate_browser_email_otp(page, code, device_id, user_agent, str(page.url or ""))
                if not resp.get("ok"):
                    text = str(resp.get("text") or "")
                    if _is_wrong_email_otp_text(text):
                        resent = _click_email_otp_resend(page)
                        self.log(f"快速邮箱注册验证码错误，已{'点击' if resent else '尝试'}重新发送，等待新验证码")
                        otp_submitted = False
                        state = _derive_registration_state_from_page(page)
                        time.sleep(2)
                        continue
                    raise RuntimeError(f"快速邮箱注册验证码提交失败: {text[:300]}")
                continue_url = str((resp.get("data") or {}).get("continue_url") or resp.get("url") or "").strip()
                if continue_url and continue_url.startswith("http") and continue_url != str(page.url or ""):
                    page.goto(continue_url, wait_until="domcontentloaded", timeout=60000)
                state = _extract_flow_state(resp.get("data"), resp.get("url", page.url))
                otp_submitted = True
                continue


            if "add-phone" in current_url or "phone-verification" in current_url:
                raise RuntimeError("快速邮箱注册触发 add-phone；请改用 legacy 链路或绑定手机号池流程")

            continue_url = str(state.get("continue_url") or state.get("current_url") or "").strip()
            if continue_url and continue_url != current_url and (continue_url.startswith("http") or continue_url.startswith("/")):
                target = continue_url if continue_url.startswith("http") else f"{OPENAI_AUTH}{continue_url}"
                page.goto(target, wait_until="domcontentloaded", timeout=60000)
                state = _derive_registration_state_from_page(page)
                continue

            time.sleep(2)

        raise TimeoutError("快速邮箱注册流程超时")

    def _goto_chatgpt_entry(self, page: Any) -> None:
        last_error = ""
        for attempt in range(1, 4):
            try:
                page.goto(CHATGPT_APP, wait_until="domcontentloaded", timeout=60000)
                return
            except Exception as exc:
                last_error = str(exc).splitlines()[0][:240]
                self.log(f"快速邮箱注册: ChatGPT 入口打开失败 attempt={attempt}/3 error={last_error}")
                if attempt >= 3:
                    break
                try:
                    if page.is_closed():
                        break
                    page.goto("about:blank", wait_until="domcontentloaded", timeout=10000)
                except Exception:
                    pass
                time.sleep(2 * attempt)
        raise RuntimeError(f"快速邮箱注册入口网络失败: {last_error}")

    def _extract_session(self, session: BrowserSession) -> dict[str, Any]:
        page = session.page
        if page is None:
            raise RuntimeError("browser session has no page")
        if page.is_closed():
            self.log("快速邮箱注册: 原页面已关闭，从 browser_context 创建新页面提取 session")
            ctx = getattr(session, "browser_context", None)
            if ctx is None:
                raise RuntimeError("browser session 没有 browser_context，无法创建新页面")
            page = ctx.new_page()
        try:
            if not self._is_chatgpt_page(str(page.url or "")):
                page.goto(CHATGPT_APP, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            self.log(f"快速邮箱注册: ChatGPT 首页等待失败，继续提取 session: {str(exc).splitlines()[0][:160]}")
        token_result = extract_chatgpt_access_token(page, attempts=30, delay=2.0, log_fn=self.log)
        access_token = token_result.access_token if token_result.success else ""
        if not access_token:
            try:
                page.goto("https://chatgpt.com/api/auth/session", wait_until="domcontentloaded", timeout=10000)
                body = page.evaluate("() => document.body?.innerText || document.body?.textContent || ''")
            except Exception:
                body = ""
            import json as _json, re as _re
            match = _re.search(r'"accessToken"\s*:\s*"([^"]+)"', str(body))
            if match:
                access_token = match.group(1)
                self.log(f"快速邮箱注册: 从页面 body 提取到 access_token: {access_token[:20]}...")
        if not access_token:
            raise RuntimeError(f"快速邮箱注册已完成但 access_token 提取失败: {token_result.failure_reason or token_result.status}")
        return {
            "access_token": access_token,
            "cookies": _get_cookies(page),
            "state": _derive_registration_state_from_page(page),
        }
    def _state_from_page(self, page: Any, fallback: dict[str, Any]) -> dict[str, Any]:
        try:
            state = _derive_registration_state_from_page(page)
            return state or fallback
        except Exception:
            return fallback

    def _blocked_authorize_reason(self, page: Any, state: dict[str, Any]) -> str:
        current_url = str(getattr(page, "url", "") or state.get("current_url") or "")
        if "/api/accounts/authorize" not in current_url or state.get("page_type"):
            return ""
        try:
            page_text = str(page.locator("body").inner_text(timeout=700) or "")
        except Exception:
            try:
                page_text = str(page.evaluate("() => document.body?.innerText || document.body?.textContent || document.title || ''") or "")
            except Exception:
                page_text = ""
        lowered = f"{current_url}\n{page_text}".lower()
        markers = (
            "cloudflare",
            "cf_chl_",
            "turnstile",
            "verify you are human",
            "checking your browser",
            "enable javascript",
            "403 forbidden",
        )
        if any(marker in lowered for marker in markers):
            return "快速邮箱注册被 OpenAI/Cloudflare 风控卡在 authorize；当前是 headless/隐藏窗口或代理被挑战。请使用显示浏览器窗口并更换干净代理。"
        return ""



    def _wait_email_code_with_resend(self, page: Any, otp_callback: Callable[[], str]) -> str:
        try:
            return str(otp_callback() or "")
        except TimeoutError as exc:
            resent = _click_email_otp_resend(page)
            self.log(f"快速邮箱注册验证码等待超时，已{'点击' if resent else '尝试'}重新发送，继续等待新验证码: {str(exc).splitlines()[0][:180]}")
            if not resent:
                raise
            time.sleep(2)
            return str(otp_callback() or "")

    def _is_chatgpt_page(self, url: str) -> bool:
        try:
            from urllib.parse import urlsplit

            return str(urlsplit(str(url or "")).hostname or "").lower().endswith("chatgpt.com")
        except Exception:
            return False

    def _has_chatgpt_session(self, page: Any) -> bool:
        try:
            payload = page.evaluate(
                """async () => {
                    if (!location.hostname.endsWith('chatgpt.com')) return null;
                    const resp = await fetch('/api/auth/session', { credentials: 'include' });
                    if (!resp.ok) return null;
                    return await resp.json();
                }"""
            )
            return bool(payload and payload.get("accessToken"))
        except Exception:
            return False

    def _has_password_input(self, page: Any) -> bool:
        try:
            return bool(page.locator('input[type="password"], input[name="password"]').first.is_visible(timeout=500))
        except Exception:
            return False

    def _has_otp_input(self, page: Any) -> bool:
        try:
            if self._has_about_you(page):
                return False
            return bool(page.locator('input[autocomplete="one-time-code"], input[inputmode="numeric"], input[name*="code" i]').first.is_visible(timeout=500))
        except Exception:
            return False

    def _has_about_you(self, page: Any) -> bool:
        try:
            text = re.sub(r"\s+", " ", page.locator("body").inner_text(timeout=700)).lower()
            has_en = "about you" in text or "birth" in text or "full name" in text or "finish creating account" in text
            has_jp = "氏名" in text or "生年月日" in text or "年齢を確認" in text or "誕生日" in text
            return has_en or has_jp
        except Exception:
            return False
