"""Patch resume-bind JSON OAuth engine.

This module owns the JSON-first OAuth bind flow for resumed ChatGPT sessions.
The caller keeps the durable `resume_file + browser_storage_state_path` contract and
launches/restores `BrowserSession`; this engine runs *inside* that restored session.

Flow order:
1. prepare authorize URL
2. authorize continue (JSON, when supported)
3. email OTP send/validate
4. add-phone send/validate
5. workspace/org select
6. follow redirects
7. extract callback/code
8. CPA callback submit or local token exchange

When the flow lands in an unsupported or brittle page-only branch, the engine falls
back to the existing `_do_codex_oauth` state machine instead of inventing a second
contract.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from platforms.chatgpt.constants import CODEX_CLIENT_ID, CODEX_REDIRECT_URI, CODEX_SCOPE


LogFn = Callable[[str], None]
OtpFn = Callable[[], str]



class _PatchResumeBindPageFallback(RuntimeError):
    """Internal signal for JSON branches that should continue in page fallback."""


class ResumeOAuthProxyChallenge(RuntimeError):
    """Raised when auth.openai.com reaches a Cloudflare/Turnstile challenge instead of OAuth state."""



@dataclass(frozen=True)
class ResumeBindContract:
    resume_file: str
    browser_storage_state_path: str
    registration_proxy: str
    registration_proxy_exit_ip: str
    login_identity: str
    email: str
    phone_number: str
    password: str
    account_id: str
    plan_type: str


@dataclass(frozen=True)
class PatchResumeBindResult:
    payload: dict[str, Any]
    path: str


class PatchResumeBindEngine:
    def __init__(
        self,
        browser_session,
        *,
        config: dict[str, Any] | None = None,
        contract: ResumeBindContract | None = None,
        log_fn: LogFn | None = None,
    ):
        self.browser_session = browser_session
        self.page = getattr(browser_session, "page", None)
        self.config = dict(config or {})
        self.contract = contract
        self.log = log_fn or (lambda _msg: None)
        self._user_agent = ""
        self._device_id = ""

    def run(
        self,
        *,
        login_identity: str = "",
        password: str = "",
        bind_email: str = "",
        otp_callback: OtpFn | None = None,
        phone_callback: Any | None = None,
        proxy: str | None = None,
        redirect_uri: str | None = None,
        client_id: str | None = None,
        authorize_url: str | None = None,
        callback_handler=None,
        allow_page_fallback: bool = True,
    ) -> dict[str, Any]:
        if self.page is None:
            raise RuntimeError("PatchResumeBindEngine 缺少可用 page")

        identity = str(login_identity or (self.contract.login_identity if self.contract else "") or "").strip()
        secret = str(password or (self.contract.password if self.contract else "") or "").strip()
        bind_target = str(bind_email or "").strip()
        proxy_url = str(proxy or (self.contract.registration_proxy if self.contract else "") or self.config.get("proxy") or "").strip() or None

        if not identity:
            raise RuntimeError("resume-bind 缺少登录身份（email 或 phone_number）")
        if not secret:
            raise RuntimeError("resume-bind 缺少账号密码")

        oauth_start = self._build_oauth_start(
            authorize_url=authorize_url,
            redirect_uri=redirect_uri,
            client_id=client_id,
        )
        self._prime_browser_identity()
        self.log(f"  JSON resume-bind 启动: identity={identity} state={oauth_start.state[:16]}...")

        current_url = self._browser_authorize(oauth_start.auth_url)
        if self._is_callback_url(current_url):
            return self._finalize_callback(current_url, oauth_start, proxy_url, callback_handler, path="json")

        last_state: dict[str, Any] = self._derive_state(current_url)
        email_otp_sent = False

        for step in range(24):
            state = self._derive_state()
            if state.get("page_type"):
                last_state = state
            else:
                state = last_state

            current_url = str(state.get("current_url") or self.page.url or "")
            next_url = str(state.get("continue_url") or "")
            page_type = str(state.get("page_type") or "")
            self.log(
                f"  JSON bind state[{step + 1}/24]: "
                f"page={page_type or '-'} next={next_url[:72]} url={current_url[:120]}"
            )

            callback_url = current_url if self._is_callback_url(current_url) else next_url if self._is_callback_url(next_url) else self._captured_callback_url()
            if callback_url:
                return self._finalize_callback(callback_url, oauth_start, proxy_url, callback_handler, path="json")

            if page_type in {"consent", "workspace_selection", "organization_selection", "external_url"}:
                session_result = self._complete_with_session(oauth_start, proxy_url, callback_handler)
                if session_result:
                    session_result.setdefault("flow_path", "json")
                    return session_result
                callback_url = self._captured_callback_url()
                if callback_url:
                    self.log("  从浏览器失败请求中捕获 OAuth callback，直接提交回调")
                    return self._finalize_callback(callback_url, oauth_start, proxy_url, callback_handler, path="captured_callback")
                break

            if page_type == "login_email":
                if identity.startswith("+"):
                    response = self._submit_login_phone(identity)
                    self._raise_if_bad_response("手机号登录提交失败", response)
                    self._sync_page_to_state(self._derive_state(str(response.get("url") or self.page.url or "")))
                    continue
                next_state = self._authorize_continue(identity)
                self._sync_page_to_state(next_state)
                last_state = next_state
                if self._is_email_otp_send_state(next_state):
                    sent_state = self._send_email_otp(next_state)
                    email_otp_sent = True
                    self._sync_page_to_state(sent_state)
                    last_state = sent_state
                continue

            if page_type == "add_email":
                if not bind_target:
                    raise RuntimeError("JSON resume-bind 命中 add_email，但没有可绑定邮箱")
                response = self._submit_bind_email(bind_target)
                if response.get("error") == "EMAIL_ALREADY_USED":
                    payload = dict(response)
                    payload.setdefault("flow_path", "json")
                    return payload
                self._raise_if_bad_response("绑定邮箱提交失败", response)
                self._sync_page_to_state(self._derive_state(str(response.get("url") or self.page.url or "")))
                continue

            if page_type in {"login_password", "create_account_password"}:
                response = self._submit_password(secret)
                self._raise_if_bad_response("密码提交失败", response)
                self._sync_page_to_state(self._derive_state(str(response.get("url") or self.page.url or "")))
                continue

            if page_type == "email_otp_verification":
                if not otp_callback:
                    raise RuntimeError("JSON resume-bind 需要邮箱 OTP，但未提供 otp_callback")
                if self._is_email_otp_send_state(state):
                    sent_state = self._send_email_otp(state)
                    email_otp_sent = True
                    self._sync_page_to_state(sent_state)
                    last_state = sent_state
                    continue
                if not email_otp_sent:
                    email_otp_sent = True
                next_state = self._validate_email_otp(otp_callback, state)
                self._sync_page_to_state(next_state)
                last_state = next_state
                continue

            if page_type == "about_you":
                next_state = self._submit_about_you(state)
                self._sync_page_to_state(next_state)
                last_state = next_state
                continue

            if page_type == "add_phone":
                if not phone_callback:
                    break
                try:
                    next_state = self._handle_add_phone_json(phone_callback, state)
                except _PatchResumeBindPageFallback as exc:
                    self.log(f"  JSON add-phone 转入页面 fallback: {str(exc)[:180]}")
                    break
                self._sync_page_to_state(next_state)
                last_state = next_state
                continue

            if next_url and next_url != current_url:
                self._goto(next_url)
                last_state = self._derive_state(next_url)
                continue

            break

        if not allow_page_fallback:
            raise RuntimeError(
                "JSON resume-bind 未能推进到 callback，且已禁用页面 fallback"
                f": page={last_state.get('page_type') or '-'} url={str(last_state.get('current_url') or '')[:160]}"
            )
        self.log("  JSON resume-bind 转入现有页面状态机 fallback...")
        return self._run_page_fallback(
            oauth_start=oauth_start,
            login_identity=identity,
            password=secret,
            bind_email=bind_target or None,
            otp_callback=otp_callback,
            phone_callback=phone_callback,
            proxy=proxy_url,
            callback_handler=callback_handler,
        )

    def _prime_browser_identity(self) -> None:
        from platforms.chatgpt.browser_register import _random_chrome_ua

        cookies = self._get_cookies()
        try:
            self._user_agent = str(self.page.evaluate("() => navigator.userAgent") or "").strip()
        except Exception:
            self._user_agent = ""
        if not self._user_agent:
            self._user_agent = _random_chrome_ua()
        self._device_id = str(cookies.get("oai-did") or getattr(self.browser_session, "device_id", "") or uuid.uuid4()).strip()

    def _build_oauth_start(
        self,
        *,
        authorize_url: str | None,
        redirect_uri: str | None,
        client_id: str | None,
    ) -> OAuthStart:
        from platforms.chatgpt.oauth import OAuthStart, generate_oauth_url
        resolved_redirect_uri = str(redirect_uri or self.config.get("oauth_redirect_uri") or CODEX_REDIRECT_URI).strip() or CODEX_REDIRECT_URI
        resolved_client_id = str(client_id or self.config.get("oauth_client_id") or CODEX_CLIENT_ID).strip() or CODEX_CLIENT_ID
        if authorize_url:
            parsed = urlparse(str(authorize_url))
            state = str((parse_qs(parsed.query).get("state") or [""])[0] or "").strip()
            return OAuthStart(
                auth_url=str(authorize_url),
                state=state,
                code_verifier="",
                redirect_uri=resolved_redirect_uri,
                client_id=resolved_client_id,
            )
        return generate_oauth_url(
            redirect_uri=resolved_redirect_uri,
            scope=CODEX_SCOPE,
            client_id=resolved_client_id,
        )

    def _browser_authorize(self, auth_url: str) -> str:
        from platforms.chatgpt.browser_register import _browser_authorize

        final_url = _browser_authorize(self.page, auth_url, self.log)
        if not final_url:
            raise RuntimeError("JSON resume-bind authorize 启动失败")
        return str(final_url)

    def _derive_state(self, current_url: str = "") -> dict[str, Any]:
        from platforms.chatgpt.browser_register import _derive_oauth_state_from_page, _extract_flow_state

        try:
            state = _derive_oauth_state_from_page(self.page)
        except Exception:
            state = {}
        if state and state.get("page_type"):
            return state
        return _extract_flow_state(None, current_url or str(self.page.url or ""))

    def _authorize_continue(self, identity: str) -> dict[str, Any]:
        from platforms.chatgpt.browser_register import (
            OPENAI_AUTH,
            _browser_fetch,
            _build_browser_headers,
            _build_browser_sentinel_token,
            _extract_flow_state,
            _generate_datadog_trace_headers,
        )

        request_url = f"{OPENAI_AUTH}/api/accounts/authorize/continue"
        referer = str(self.page.url or "") or f"{OPENAI_AUTH}/log-in"
        headers = _build_browser_headers(
            user_agent=self._user_agent,
            accept="application/json",
            referer=referer,
            origin=OPENAI_AUTH,
            content_type="application/json",
            extra_headers={
                "sec-fetch-site": "same-origin",
                "oai-device-id": self._device_id,
                **_generate_datadog_trace_headers(),
            },
        )
        sentinel = _build_browser_sentinel_token(self.page, self._device_id, "authorize_continue", self._user_agent)
        if sentinel:
            headers["openai-sentinel-token"] = sentinel

        last_response: dict[str, Any] = {}
        for attempt in range(2):
            response = _browser_fetch(
                self.page,
                request_url,
                method="POST",
                headers=headers,
                body=json.dumps({"username": {"kind": "email", "value": identity}}),
                redirect="follow",
            )
            last_response = response
            status = int(response.get("status") or 0)
            text = str(response.get("text") or "")
            self.log(f"  authorize/continue -> {status}")
            if response.get("ok") or status in {200, 201}:
                return _extract_flow_state(response.get("data"), str(response.get("url") or request_url))
            if attempt == 0 and "invalid_auth_step" in text:
                self.log("  authorize/continue 返回 invalid_auth_step，重新访问 authorize 后重试")
                self._browser_authorize(str(self.page.url or referer))
                continue
            raise RuntimeError(f"JSON authorize/continue 失败: {status} {text[:220]}")
        raise RuntimeError(f"JSON authorize/continue 失败: {str(last_response.get('text') or '')[:220]}")

    def _send_email_otp(self, state: dict[str, Any]) -> dict[str, Any]:
        from platforms.chatgpt.browser_register import OPENAI_AUTH, _browser_fetch, _extract_flow_state

        request_url = f"{OPENAI_AUTH}/api/accounts/email-otp/send"
        referer = str(state.get("current_url") or self.page.url or "") or f"{OPENAI_AUTH}/log-in"
        response = _browser_fetch(
            self.page,
            request_url,
            method="GET",
            headers={
                "accept": "application/json, text/plain, */*",
                "referer": referer,
                "sec-fetch-site": "same-origin",
                "sec-fetch-mode": "cors",
                "sec-fetch-dest": "empty",
                "accept-language": "en-US,en;q=0.9",
            },
            redirect="follow",
        )
        status = int(response.get("status") or 0)
        self.log(f"  email-otp/send -> {status}")
        if not (response.get("ok") or status in {200, 201}):
            raise RuntimeError(f"JSON email-otp/send 失败: {str(response.get('text') or '')[:220]}")
        return _extract_flow_state(response.get("data"), str(response.get("url") or request_url))

    def _validate_email_otp(self, otp_callback: OtpFn, state: dict[str, Any]) -> dict[str, Any]:
        from platforms.chatgpt.browser_register import _extract_flow_state, _validate_browser_email_otp

        last_error = ""
        for attempt in range(1, 4):
            code = str(otp_callback() or "").strip()
            if not code:
                raise RuntimeError("邮箱 OTP 回调未返回验证码")
            response = _validate_browser_email_otp(
                self.page,
                code,
                self._device_id,
                self._user_agent,
                str(state.get("current_url") or self.page.url or ""),
            )
            status = int(response.get("status") or 0)
            self.log(f"  email-otp/validate -> {status}")
            if response.get("ok") or status in {200, 201, 204}:
                return _extract_flow_state(response.get("data"), str(response.get("url") or self.page.url or ""))
            text = str(response.get("text") or "")
            lowered = text.lower()
            last_error = text or f"status={status}"
            wrong_code = any(fragment in lowered for fragment in (
                "wrong_email_otp_code",
                "wrong code",
                "incorrect code",
                "invalid code",
                "验证码错误",
                "验证码无效",
            ))
            if wrong_code and attempt < 3:
                self.log(f"  邮箱 OTP 无效，重新发送并重试: attempt={attempt}")
                state = self._send_email_otp(state)
                self._sync_page_to_state(state)
                continue
            raise RuntimeError(f"JSON email-otp/validate 失败: {last_error[:220]}")
        raise RuntimeError(f"JSON email-otp/validate 失败: {last_error[:220]}")

    def _submit_about_you(self, state: dict[str, Any]) -> dict[str, Any]:
        from platforms.chatgpt.browser_register import _extract_flow_state, _submit_browser_about_you

        response = _submit_browser_about_you(
            self.page,
            self._device_id,
            self._user_agent,
            str(state.get("current_url") or self.page.url or ""),
        )
        self._raise_if_bad_response("about_you 提交失败", response)
        return _extract_flow_state(response.get("data"), str(response.get("url") or self.page.url or ""))

    def _handle_add_phone_json(self, phone_callback: Any, state: dict[str, Any]) -> dict[str, Any]:
        from platforms.chatgpt.browser_register import _classify_add_phone_api_issue, _extract_add_phone_api_detail, _extract_flow_state, _is_invalid_phone_otp_response, _is_phone_recently_or_already_used_error, _validate_browser_phone_otp

        last_error = ""
        for phone_attempt in range(1, 4):
            if phone_attempt > 1:
                self.log(f"  JSON add-phone 换号重试 {phone_attempt}/3")
            phone_number = str(phone_callback() or "").strip()
            if not phone_number:
                raise RuntimeError("add-phone callback 未返回手机号")
            send_response = self._send_phone_otp(phone_number, state)
            send_status = int(send_response.get("status") or 0)
            self.log(f"  add-phone/send -> {send_status}")
            if not (send_response.get("ok") or send_status in {200, 201}):
                send_error = _extract_add_phone_api_detail(send_response) or f"status={send_status}"
                send_issue = _classify_add_phone_api_issue(send_response)
                last_error = send_error
                if send_issue:
                    raise _PatchResumeBindPageFallback(f"add-phone/send API {send_issue}: {send_error[:160]}")
                if _is_phone_recently_or_already_used_error(send_error) or "invalid" in send_error.lower() or "無効" in send_error:
                    if hasattr(phone_callback, "mark_send_failed"):
                        phone_callback.mark_send_failed(send_error)
                    self._reset_phone_callback(phone_callback)
                    continue
                raise RuntimeError(f"JSON add-phone/send 失败: {send_error[:220]}")
            if hasattr(phone_callback, "mark_send_succeeded"):
                phone_callback.mark_send_succeeded()

            next_state = _extract_flow_state(send_response.get("data"), str(send_response.get("url") or self.page.url or ""))
            self._sync_page_to_state(next_state)

            for code_attempt in range(1, 4):
                sms_code = str(phone_callback() or "").strip()
                if not sms_code:
                    raise RuntimeError("add-phone callback 未返回短信验证码")
                validate_response = _validate_browser_phone_otp(
                    self.page,
                    sms_code,
                    self._device_id,
                    self._user_agent,
                    str(next_state.get("current_url") or self.page.url or ""),
                )
                validate_status = int(validate_response.get("status") or 0)
                self.log(f"  phone-otp/validate -> {validate_status}")
                if validate_response.get("ok") or validate_status in {200, 201, 204}:
                    if hasattr(phone_callback, "report_success"):
                        phone_callback.report_success()
                    return _extract_flow_state(
                        validate_response.get("data"),
                        str(validate_response.get("url") or self.page.url or ""),
                    )
                text = _extract_add_phone_api_detail(validate_response)
                last_error = text or f"status={validate_status}"
                validate_issue = _classify_add_phone_api_issue(validate_response)
                if validate_issue:
                    raise _PatchResumeBindPageFallback(f"phone-otp/validate API {validate_issue}: {last_error[:160]}")
                if _is_phone_recently_or_already_used_error(text):
                    if hasattr(phone_callback, "mark_send_failed"):
                        phone_callback.mark_send_failed(text or "phone recently used")
                    self._reset_phone_callback(phone_callback)
                    break
                if _is_invalid_phone_otp_response(validate_response):
                    if hasattr(phone_callback, "mark_code_failed"):
                        phone_callback.mark_code_failed(text or "invalid otp code")
                    if code_attempt < 3:
                        self.log(f"  phone OTP 无效，等待下一条验证码: attempt={code_attempt}")
                        continue
                if hasattr(phone_callback, "mark_code_failed"):
                    phone_callback.mark_code_failed(text or f"status={validate_status}")
                raise RuntimeError(f"JSON phone-otp/validate 失败: {last_error[:220]}")
        raise RuntimeError(f"JSON add-phone 失败: {last_error[:220]}")

    def _send_phone_otp(self, phone_number: str, state: dict[str, Any]) -> dict[str, Any]:
        from platforms.chatgpt.browser_register import (
            OPENAI_AUTH,
            _browser_fetch,
            _build_browser_headers,
            _generate_datadog_trace_headers,
        )

        request_url = f"{OPENAI_AUTH}/api/accounts/add-phone/send"
        referer = str(state.get("current_url") or self.page.url or "") or f"{OPENAI_AUTH}/add-phone"
        headers = _build_browser_headers(
            user_agent=self._user_agent,
            accept="application/json",
            referer=referer,
            origin=OPENAI_AUTH,
            content_type="application/json",
            extra_headers={
                "sec-fetch-site": "same-origin",
                "oai-device-id": self._device_id,
                **_generate_datadog_trace_headers(),
            },
        )
        return _browser_fetch(
            self.page,
            request_url,
            method="POST",
            headers=headers,
            body=json.dumps({"phone_number": phone_number}),
            redirect="follow",
        )

    def _submit_bind_email(self, bind_email: str) -> dict[str, Any]:
        from platforms.chatgpt.browser_register import _submit_login_email_via_page

        return _submit_login_email_via_page(self.page, bind_email, self.log)

    def _submit_login_phone(self, phone_number: str) -> dict[str, Any]:
        from platforms.chatgpt.browser_register import _submit_login_phone_via_page

        return _submit_login_phone_via_page(self.page, phone_number, self.log)

    def _submit_password(self, password: str) -> dict[str, Any]:
        from platforms.chatgpt.browser_register import _submit_oauth_password_direct

        return _submit_oauth_password_direct(self.page, password, self.log)

    def _captured_callback_url(self) -> str:
        page = self.page
        if page is None:
            return ""
        try:
            value = str(getattr(page, "_omp_last_oauth_callback_url", "") or "")
        except Exception:
            value = ""
        return value if self._is_callback_url(value) else ""

    def _complete_with_session(self, oauth_start: OAuthStart, proxy: str | None, callback_handler=None) -> dict[str, Any] | None:
        from platforms.chatgpt.browser_register import _complete_oauth_with_session

        result = _complete_oauth_with_session(self._get_cookies(), oauth_start, proxy, self.log, callback_handler=callback_handler)
        if isinstance(result, dict):
            result.setdefault("flow_path", "json")
        return result

    def _finalize_callback(
        self,
        callback_url: str,
        oauth_start: OAuthStart,
        proxy: str | None,
        callback_handler=None,
        *,
        path: str,
    ) -> dict[str, Any]:
        from platforms.chatgpt.browser_register import _submit_callback_result

        result = _submit_callback_result(callback_url, oauth_start, proxy, callback_handler=callback_handler)
        result.setdefault("callback_url", callback_url)
        result.setdefault("flow_path", path)
        return result

    def _current_proxy_challenge_reason(self) -> str:
        page = self.page
        if page is None:
            return ""
        try:
            current_url = str(getattr(page, "url", "") or "")
        except Exception:
            current_url = ""
        try:
            body_text = str(page.locator("body").inner_text(timeout=700) or "")
        except Exception:
            try:
                body_text = str(page.evaluate("() => document.body?.innerText || document.body?.textContent || document.title || ''") or "")
            except Exception:
                body_text = ""
        haystack = f"{current_url}\n{body_text}".lower()
        markers = (
            "cloudflare",
            "turnstile",
            "cf_chl_",
            "verify you are human",
            "checking your browser",
            "security verification",
            "403 forbidden",
            "なぜ検証に時間がかかる",
            "検証に時間がかかる",
        )
        for marker in markers:
            if marker in haystack:
                return f"{marker}; url={current_url[:160]}"
        if "choose-an-account" in current_url.lower():
            return f"choose_account_stuck; url={current_url[:160]}"
        return ""

    def _run_page_fallback(
        self,
        *,
        oauth_start: OAuthStart,
        login_identity: str,
        password: str,
        bind_email: str | None,
        otp_callback: OtpFn | None,
        phone_callback: Any | None,
        proxy: str | None,
        callback_handler=None,
    ) -> dict[str, Any]:
        from platforms.chatgpt.browser_register import _do_codex_oauth

        result = _do_codex_oauth(
            self.page,
            self._get_cookies(),
            email=login_identity,
            password=password,
            otp_callback=otp_callback,
            phone_callback=phone_callback,
            proxy=proxy,
            log=self.log,
            bind_email=bind_email,
            redirect_uri=oauth_start.redirect_uri,
            client_id=oauth_start.client_id,
            authorize_url=oauth_start.auth_url,
            callback_handler=callback_handler,
        )
        if not result:
            callback_url = self._captured_callback_url()
            if callback_url:
                self.log("  页面 fallback 后捕获 OAuth callback，直接提交回调")
                return self._finalize_callback(callback_url, oauth_start, proxy, callback_handler, path="captured_callback")
            challenge_reason = self._current_proxy_challenge_reason()
            if challenge_reason:
                raise ResumeOAuthProxyChallenge(f"resume-oauth 触发 OpenAI/Cloudflare 验证，当前代理不可用于 OAuth: {challenge_reason}")
            raise RuntimeError("JSON resume-bind 与页面 fallback 均未返回结果")
        result.setdefault("flow_path", "page_fallback")
        return result

    def _sync_page_to_state(self, state: dict[str, Any]) -> None:
        target = str(state.get("current_url") or state.get("continue_url") or "").strip()
        if not target:
            return
        current = str(self.page.url or "").strip()
        if current == target:
            return
        if not target.startswith("http"):
            return
        if self._is_callback_url(target):
            return
        try:
            self._goto(target)
        except Exception as exc:
            self.log(f"  页面同步到下一状态失败，继续使用现有 cookie/session: {str(exc)[:180]}")

    def _goto(self, url: str) -> None:
        self.page.goto(url, wait_until="domcontentloaded", timeout=30000)

    def _get_cookies(self) -> dict[str, str]:
        from platforms.chatgpt.browser_register import _get_cookies

        return _get_cookies(self.page)

    @staticmethod
    def _extract_code(url: str) -> str:
        from platforms.chatgpt.browser_register import _extract_code_from_url

        return _extract_code_from_url(url)

    @staticmethod
    def _is_callback_url(url: str) -> bool:
        value = str(url or "")
        if not value or "state=" not in value:
            return False
        if "code=" not in value and "error=" not in value:
            return False
        return "/auth/callback" in value or "localhost" in value or "code=" in value or "error=" in value

    @staticmethod
    def _is_email_otp_send_state(state: dict[str, Any]) -> bool:
        target = f"{state.get('continue_url') or ''} {state.get('current_url') or ''}".lower()
        return "/api/accounts/email-otp/send" in target

    @staticmethod
    def _raise_if_bad_response(prefix: str, response: dict[str, Any]) -> None:
        status = int(response.get("status") or 0)
        if response.get("ok") or status in {200, 201, 204}:
            return
        text = str(response.get("text") or "")
        raise RuntimeError(f"{prefix}: {status} {text[:220]}")

    @staticmethod
    def _reset_phone_callback(phone_callback: Any) -> None:
        if hasattr(phone_callback, "cleanup"):
            phone_callback.cleanup()
        if hasattr(phone_callback, "phase"):
            phone_callback.phase = "need_number"
        if hasattr(phone_callback, "activation"):
            phone_callback.activation = None
        if hasattr(phone_callback, "completed"):
            phone_callback.completed = False


def load_resume_bind_contract(resume_file: str | Path) -> ResumeBindContract:
    path = Path(str(resume_file)).expanduser()
    if not path.exists():
        raise RuntimeError(f"resume 文件不存在: {path}")
    data = json.loads(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise RuntimeError(f"resume 文件格式错误: {path}")
    email = str(data.get("email") or "").strip()
    phone_number = str(data.get("phone_number") or "").strip()
    login_identity = email or phone_number
    if not login_identity:
        raise RuntimeError("resume 文件缺少 email/phone_number，无法恢复 bind")
    return ResumeBindContract(
        resume_file=str(path),
        browser_storage_state_path=str(data.get("browser_storage_state_path") or "").strip(),
        registration_proxy=str(data.get("registration_proxy") or "").strip(),
        registration_proxy_exit_ip=str(data.get("registration_proxy_exit_ip") or "").strip(),
        login_identity=login_identity,
        email=email,
        phone_number=phone_number,
        password=str(data.get("password") or data.get("generated_chatgpt_password") or "").strip(),
        account_id=str(data.get("account_id") or data.get("chatgpt_account_id") or "").strip(),
        plan_type=str(data.get("plan_type") or "").strip(),
    )


def merge_resume_bind_config(config: dict[str, Any], contract: ResumeBindContract) -> dict[str, Any]:
    merged = dict(config or {})
    merged["resume_file"] = contract.resume_file
    if contract.browser_storage_state_path:
        merged["_browser_storage_state"] = contract.browser_storage_state_path
    if contract.registration_proxy and "127.0.0.1" not in contract.registration_proxy and "localhost" not in contract.registration_proxy:
        merged["proxy"] = contract.registration_proxy
    if contract.registration_proxy_exit_ip:
        merged["_camoufox_geoip_ip"] = contract.registration_proxy_exit_ip
    return merged


def run_patch_resume_bind(
    browser_session,
    *,
    config: dict[str, Any],
    resume_file: str | Path,
    log_fn: LogFn | None = None,
    login_identity: str = "",
    password: str = "",
    bind_email: str = "",
    otp_callback: OtpFn | None = None,
    phone_callback: Any | None = None,
    proxy: str | None = None,
    redirect_uri: str | None = None,
    client_id: str | None = None,
    authorize_url: str | None = None,
    callback_handler=None,
    allow_page_fallback: bool = True,
) -> dict[str, Any]:
    contract = load_resume_bind_contract(resume_file)
    engine = PatchResumeBindEngine(
        browser_session,
        config=merge_resume_bind_config(config, contract),
        contract=contract,
        log_fn=log_fn,
    )
    result = engine.run(
        login_identity=login_identity or contract.login_identity,
        password=password or contract.password,
        bind_email=bind_email,
        otp_callback=otp_callback,
        phone_callback=phone_callback,
        proxy=proxy or contract.registration_proxy,
        redirect_uri=redirect_uri,
        client_id=client_id,
        authorize_url=authorize_url,
        callback_handler=callback_handler,
        allow_page_fallback=allow_page_fallback,
    )
    result.setdefault("resume_file", contract.resume_file)
    if contract.browser_storage_state_path:
        result.setdefault("browser_storage_state_path", contract.browser_storage_state_path)
    return result
