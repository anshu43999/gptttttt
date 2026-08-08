from __future__ import annotations

import base64
import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any, Callable

from core import account_store
from core.browser.session import BrowserSession, extract_chatgpt_access_token
from core.mailbox.outlook_token import OutlookTokenMailbox
from core.mailbox.forwarded_domain import ForwardedDomainMailbox
from core.mailbox_providers import CFWorkerMailbox, ICloudPrivacyMailbox, LinkApiMailbox
from core.proxy.credential_runtime import CredentialProxyRuntime
from platforms.chatgpt.utils import generate_random_password


class EmailRegistrationOrchestrator:
    def __init__(self, *, log_fn: Callable[[str], None] | None = None):
        self.log_fn = log_fn or print
        self._proxy_runtime: CredentialProxyRuntime | None = None
        self._session: BrowserSession | None = None
        self._result: dict[str, Any] = {
            "success": False,
            "status": "initialized",
            "stage": "initialized",
            "steps": [],
        }

    def log(self, message: str) -> None:
        self.log_fn(message)

    def _precheck_configured_proxy(self, config: dict[str, Any], proxy_url: str) -> tuple[bool, str]:
        if not proxy_url or not bool(config.get("openai_proxy_precheck_enabled", True)):
            return True, ""
        runtime = CredentialProxyRuntime(config, log_fn=self.log)
        ok, exit_ip = runtime.check(proxy_url)
        if ok:
            self.log(f"  邮箱注册代理 OpenAI 探针通过: proxy={proxy_url} exit_ip={exit_ip or '-'}")
        return ok, exit_ip


    def run(self, config: dict[str, Any], *, headed: bool = False, task_id: str = "") -> dict[str, Any]:
        config = dict(config or {})
        provider_key = str(config.get("mailbox_provider") or "outlook_token").strip().lower()
        provider_key = {
            "icloud": "icloud_privacy",
            "icloud_hide_my_email": "icloud_privacy",
            "icloud_private": "icloud_privacy",
            "cloudflare_worker": "cfworker_admin_api",
            "cloud_mail": "cfworker_admin_api",
            "email_link_api": "icloud_api",
            "link_api": "icloud_api",
        }.get(provider_key, provider_key)

        self._result = {
            "success": False,
            "status": "running",
            "stage": "running",
            "steps": [],
            "task_id": task_id,
            "registration_task_id": task_id,
            "registration_mode": "email",
            "binding_status": "not_ready",
        }
        mailbox: Any | None = None
        account: Any | None = None
        try:
            self.log("=" * 60)
            self.log("Step 1: 邮箱注册 + 手机号绑定")
            self.log("=" * 60)

            if config.get("rotate_proxy_each_attempt"):
                self._proxy_runtime = CredentialProxyRuntime(config, log_fn=self.log)
                runtime_proxy_url, exit_ip = self._proxy_runtime.select()
                self._result["registration_proxy"] = runtime_proxy_url
                self._result["registration_proxy_exit_ip"] = exit_ip
                config["proxy"] = runtime_proxy_url
                config["_camoufox_geoip_ip"] = exit_ip
            else:
                proxy_url = str(config.get("proxy") or "").strip()
                if proxy_url:
                    ok, exit_ip = self._precheck_configured_proxy(config, proxy_url)
                    if not ok:
                        raise RuntimeError(f"邮箱注册代理 OpenAI 预检失败，跳过启动浏览器: {proxy_url}")
                    self._result["registration_proxy"] = proxy_url
                    if exit_ip:
                        self._result["registration_proxy_exit_ip"] = exit_ip
                        config["_camoufox_geoip_ip"] = exit_ip

            password = str(config.get("chatgpt_password") or "").strip() or generate_random_password(16)
            mailbox, account = self._select_mailbox_account(provider_key, config)
            if provider_key == "outlook_token":
                config["outlook_email"] = account.email
                config["outlook_password"] = getattr(account, "password", "")
                config["outlook_client_id"] = getattr(account, "client_id", "")
                config["outlook_refresh_token"] = getattr(account, "refresh_token", "")
            self._result["email"] = account.email
            self._result["mailbox_provider"] = provider_key
            self._result["email_provider"] = provider_key
            if provider_key == "outlook_token":
                self._result["outlook_email"] = account.email
            elif provider_key == "icloud_privacy":
                self._result["icloud_privacy_email"] = account.email
            elif provider_key == "icloud_api":
                self._result["icloud_api_email"] = account.email
            self._result["password"] = password
            self._result["generated_chatgpt_password"] = password

            region = str(config.get("lajiao_proxy_expected_country") or config.get("lajiao_proxy_regions") or "").split(",", 1)[0].strip().upper()
            if region == "JP":
                config.setdefault("locale", "ja-JP")
                config.setdefault("browser_locale", "ja-JP")
                config.setdefault("timezone_id", "Asia/Tokyo")
                config.setdefault("browser_timezone", "Asia/Tokyo")
                config.setdefault("accept_language", "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7")
            config["headed"] = bool(headed or config.get("headed", False))
            config["require_camoufox"] = bool(config.get("require_camoufox", False))
            config["_force_fresh_browser_context"] = True
            config["_log_fn"] = self.log
            flow = str(config.get("email_register_flow") or "legacy").strip().lower()
            engine = str(config.get("browser_engine") or ("camoufox" if config.get("use_camoufox", True) else "playwright")).strip().lower()
            config["browser_engine"] = engine
            if str(config.get("browser_profile_mode") or "").strip().lower() == "per_task":
                profile_id = task_id or uuid.uuid4().hex[:12]
                config["browser_profile_dir"] = str(Path("data") / "browser_profiles" / engine / profile_id)

            mailbox_before_ids: set[str] = set()
            if provider_key != "outlook_token" and hasattr(mailbox, "get_current_ids"):
                try:
                    mailbox_before_ids = set(mailbox.get_current_ids(account) or set())
                    self.log(f"邮箱验证码等待基线: {len(mailbox_before_ids)} 封历史邮件")
                except Exception as exc:
                    mailbox_before_ids = set()
                    self.log(f"邮箱验证码等待基线获取失败，仍会扫描新邮件: {str(exc).splitlines()[0][:160]}")

            def otp_callback() -> str:
                timeout = int(config.get("email_otp_timeout", 200) or 200)
                code = self._wait_email_code(mailbox, account, timeout=timeout, before_ids=mailbox_before_ids)
                if code and mailbox and account and hasattr(mailbox, "get_current_ids"):
                    try:
                        mailbox_before_ids.update(set(mailbox.get_current_ids(account) or set()))
                    except Exception:
                        pass
                if not code and mailbox and account:
                    self._result["status"] = "email_otp_required"
                    self._mark_mailbox_cooldown(mailbox, account.email, reason="otp_timeout")
                return code


            self.log(
                f"邮箱注册使用 {flow} 链路 + {engine} 内核；代理仍来自当前项目代理池；注册完成后停止，不执行 add-phone/OAuth"
            )
            with BrowserSession(config) as session:
                self._session = session
                if flow == "fast":
                    from platforms.chatgpt.fast_email_register import FastEmailRegistrationFlow

                    flow_result = FastEmailRegistrationFlow(log_fn=self.log).run(
                        session,
                        email=account.email,
                        password=password,
                        otp_callback=otp_callback,
                        timeout=int(config.get("email_register_timeout", 600) or 600),
                    )
                    access_token = str(flow_result.get("access_token") or "")
                    cookies = flow_result.get("cookies") or {}
                    self.log(f"邮箱快速注册状态完成: page={(flow_result.get('state') or {}).get('page_type') or '-'}")
                else:
                    from platforms.chatgpt.browser_register import _browser_registration_flow, _get_cookies

                    final_state = _browser_registration_flow(session.page, account.email, password, otp_callback, None, self.log)
                    self.log(f"邮箱浏览器注册状态完成: page={final_state.get('page_type') or '-'}")
                    self.log("提取 ChatGPT session access_token...")
                    fetch_result = extract_chatgpt_access_token(session.page, attempts=30, delay=2.0, log_fn=self.log)
                    access_token = fetch_result.access_token if fetch_result.success else ""
                    if not access_token:
                        access_token = self._extract_access_token_via_context_request(session)
                    if not access_token:
                        raise RuntimeError(f"邮箱注册已完成但 access_token 提取失败: {fetch_result.failure_reason or fetch_result.status}")
                    cookies = _get_cookies(session.page)
                self._result["access_token"] = access_token
                self._result["chatgpt_access_token_initial"] = access_token
                self._result["session_token"] = str(cookies.get("__Secure-next-auth.session-token") or cookies.get("__Secure-authjs.session-token") or "")
                self._result["browser_engine"] = engine
                self._result["browser_channel"] = str(config.get("browser_channel") or "")
                self._result["browser_profile_dir"] = str(config.get("browser_profile_dir") or "")
                self._result["email_register_flow"] = flow
                self._populate_claims(access_token, fallback_email=account.email)
                self.log(f"  Access Token: {access_token[:50]}...")
                self._result["plan_type"] = "free"
                self._result["status"] = "email_registered"
                self._result["stage"] = "manual_plus_required"
                self._result["registration_status"] = "registered"
                self._result["success"] = True
                self._result["steps"].append("email_fast_register" if flow == "fast" else "email_browser_register")
                registered_file = self._save_registered_account_json(config, session)
                resume_file = self._save_manual_plus_handoff_json(config, session)
                self._result["registered_file"] = str(registered_file)
                self._result["resume_file"] = str(resume_file)
                self._mark_mailbox_used(mailbox, account.email, reason="registered")
                self.log("邮箱注册已完成并停止；未执行 add-phone、未租用手机号、未执行 OAuth 绑定")
                return dict(self._result)
        except Exception as exc:
            text = str(exc)
            if mailbox and account and ("验证码" in text or "otp" in text.lower()):
                reason = "wrong_otp" if "校验失败" in text or "wrong" in text.lower() else "otp_timeout"
                self._mark_mailbox_cooldown(mailbox, account.email, reason=reason)
            self._result.setdefault("failure_reason", text)
            self._result["success"] = False
            raise
        finally:
            if self._proxy_runtime:
                self._proxy_runtime.cleanup()

    def _select_mailbox_account(self, provider_key: str, config: dict[str, Any]) -> tuple[Any, Any]:
        if provider_key == "outlook_token":
            mailbox = OutlookTokenMailbox(config, log_fn=self.log)
            account = mailbox.first(str(config.get("outlook_email") or config.get("email") or ""))
            return mailbox, account
        if provider_key == "icloud_privacy":
            mailbox = ICloudPrivacyMailbox.from_config(config)
            target = str(config.get("icloud_privacy_email") or config.get("email") or "").strip()
            account = mailbox.account_for_email(target) if target else mailbox.create_account()
            return mailbox, account
        if provider_key == "icloud_api":
            mailbox = LinkApiMailbox.from_config(config)
            target = str(config.get("icloud_api_email") or config.get("email") or "").strip()
            # Prefer the exclusive resource-pool lease. create_account() only remains as
            # a fallback for order-file-only configs that never prepared resource leases.
            if target:
                account = mailbox.account_for_email(target)
            else:
                account = mailbox.create_account()
            return mailbox, account
        if provider_key == "forwarded_domain":
            mailbox = ForwardedDomainMailbox.from_config(config)
            account = mailbox.create_account()
            return mailbox, account
        if provider_key in {"cfworker_admin_api", "cfworker", "cloud_mail"}:
            mailbox = CFWorkerMailbox.from_config(config)
            account = mailbox.create_account()
            return mailbox, account
        raise RuntimeError(f"modular email runner unsupported mailbox_provider: {provider_key}")

    def _wait_email_code(
        self,
        mailbox: Any,
        account: Any,
        *,
        timeout: int,
        before_ids: set[str] | None = None,
        not_before: Any = None,
        reject_codes: set[str] | None = None,
    ) -> str:
        if hasattr(mailbox, "wait_for_openai_code"):
            kwargs: dict[str, Any] = {"timeout": timeout}
            if not_before is not None:
                kwargs["not_before"] = not_before
            if reject_codes:
                kwargs["reject_codes"] = reject_codes
            return str(mailbox.wait_for_openai_code(account, **kwargs) or "")
        seen = set(before_ids or set())
        if not seen and hasattr(mailbox, "get_current_ids"):
            try:
                seen = set(mailbox.get_current_ids(account) or set())
            except Exception:
                seen = set()
        if not hasattr(mailbox, "wait_for_code"):
            raise RuntimeError(f"邮箱 provider 不支持等待验证码: {type(mailbox).__name__}")
        return str(mailbox.wait_for_code(account, timeout=timeout, before_ids=seen) or "")

    def _mark_mailbox_used(self, mailbox: Any, email: str, *, reason: str) -> None:
        if hasattr(mailbox, "mark_used"):
            mailbox.mark_used(email, reason=reason)
        elif hasattr(mailbox, "_record_state"):
            try:
                mailbox._record_state(email, "consumed", reason=reason)
            except Exception:
                pass

    def _mark_mailbox_cooldown(self, mailbox: Any, email: str, *, reason: str) -> None:
        if hasattr(mailbox, "mark_cooldown"):
            mailbox.mark_cooldown(email, reason=reason)
        elif hasattr(mailbox, "_record_state"):
            try:
                mailbox._record_state(email, "cooldown", reason=reason)
            except Exception:
                pass

    def _extract_access_token_via_context_request(self, session: BrowserSession) -> str:
        context = getattr(session, "browser_context", None)
        page = getattr(session, "page", None)
        parsed = urlsplit(str(getattr(page, "url", "") or ""))
        if context is None or not hasattr(context, "request") or parsed.scheme != "https" or not parsed.netloc:
            return ""
        origin = f"{parsed.scheme}://{parsed.netloc}"
        for path in (
            "/api/auth/session?refresh=true&reason=email_register_extract",
            "/api/auth/session",
        ):
            try:
                response = context.request.get(f"{origin}{path}", headers={"accept": "application/json", "referer": f"{origin}/"}, timeout=30000)
                text = response.text()
                if response.status >= 400:
                    self.log(f"  context request access_token 提取失败: status={response.status} body={text[:160]}")
                    continue
                payload = response.json()
                token = str(payload.get("accessToken") or payload.get("access_token") or "") if isinstance(payload, dict) else ""
                if token:
                    self.log("  context request 已提取 access_token")
                    return token
                self.log(f"  context request access_token 为空: status={response.status} body={text[:160]}")
            except Exception as exc:
                self.log(f"  context request access_token 提取异常: {str(exc).splitlines()[0][:180]}")
        return ""

    def _populate_claims(self, access_token: str, *, fallback_email: str) -> None:
        try:
            segment = access_token.split(".")[1]
            segment += "=" * ((4 - len(segment) % 4) % 4)
            claims = json.loads(base64.urlsafe_b64decode(segment.encode("ascii")).decode("utf-8"))
            namespaced_claims = {
                str(name).rstrip("/").rsplit("/", 1)[-1]: value
                for name, value in claims.items()
                if str(name).startswith("https://") and isinstance(value, dict)
            }
            auth_claims = namespaced_claims.get("auth") or {}
            profile_claims = namespaced_claims.get("profile") or {}
            self._result["account_id"] = str(auth_claims.get("chatgpt_account_id") or claims.get("sub") or "")
            self._result["email"] = str(profile_claims.get("email") or claims.get("email") or fallback_email)
            self._result["plan_type"] = str(auth_claims.get("chatgpt_plan_type") or "free")
        except Exception:
            self._result["email"] = fallback_email
            self._result["plan_type"] = "free"

    def _output_dir(self, config: dict[str, Any]) -> Path:
        output_dir = Path(config.get("output_dir", "output"))
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir

    def _product_label(self, config: dict[str, Any], email: str = "") -> str:
        account = str(email or self._result.get("email") or config.get("outlook_email") or self._result.get("account_id") or "account").strip()
        account = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", account).strip(" ._") or "account"
        ts = str(self._result.get("product_date") or "").strip()
        if not ts:
            ts = datetime.now().strftime("%Y%m%d")
            self._result["product_date"] = ts
        return f"{account}_{ts}"

    def _save_browser_storage_state(self, config: dict[str, Any], session: BrowserSession, resume_id: str) -> str:
        storage_path = self._output_dir(config) / f"storage_{resume_id}.json"
        return session.save_storage_state(str(storage_path))

    def _ensure_browser_storage_state(self, config: dict[str, Any], session: BrowserSession, resume_id: str) -> str:
        existing = str(self._result.get("browser_storage_state_path") or config.get("_browser_storage_state") or "").strip()
        if existing:
            return existing
        storage_path = self._save_browser_storage_state(config, session, resume_id)
        if storage_path:
            self._result["browser_storage_state_path"] = storage_path
            config["_browser_storage_state"] = storage_path
        return storage_path

    def _save_registered_account_json(self, config: dict[str, Any], session: BrowserSession) -> Path:
        registered_dir = self._output_dir(config) / "registered_accounts"
        registered_dir.mkdir(parents=True, exist_ok=True)
        filename = registered_dir / f"{self._product_label(config)}.json"
        storage_state_path = self._ensure_browser_storage_state(config, session, f"registered_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}")
        data = {
            "schema_version": 1,
            "stage": "registered",
            "created_at": datetime.now().isoformat(),
            "account_key": self._result.get("email") or self._result.get("account_id") or "",
            "phone_number": "",
            "activation_id": "",
            "account_id": self._result.get("account_id", ""),
            "email": self._result.get("email", ""),
            "password": self._result.get("password", ""),
            "plan_type": self._result.get("plan_type", ""),
            "registration_mode": self._result.get("registration_mode") or "email",
            "registration_status": "registered",
            "registration_task_id": self._result.get("registration_task_id", ""),
            "binding_status": "not_ready",
            "registration_proxy": self._result.get("registration_proxy", ""),
            "registration_proxy_exit_ip": self._result.get("registration_proxy_exit_ip", ""),
            "browser_storage_state_path": storage_state_path,
            "chatgpt_access_token_initial": self._result.get("chatgpt_access_token_initial") or self._result.get("access_token", ""),
            "access_token": self._result.get("access_token", ""),
            "session_token": self._result.get("session_token", ""),
            "resume_file": self._result.get("resume_file", ""),
            "access_token_file": self._result.get("access_token_file", ""),
            "protocol_runner": self._result.get("protocol_runner", ""),
            "protocol_work_dir": self._result.get("protocol_work_dir", ""),
        }
        with filename.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
        self._result["registered_file"] = str(filename)
        account_store.upsert_account(data, source_file=str(filename), copy_artifacts=False)
        self.log(f"  注册成功账号已保存: {filename}")
        return filename

    def _write_account_text(self, filename: Path, *, stage: str) -> Path:
        text_path = filename.with_suffix(".txt")
        text_path.write_text(
            "\n".join(
                [
                    f"阶段: {stage}",
                    "手机号: ",
                    f"邮箱: {self._result.get('email', '')}",
                    f"账号: {self._result.get('email') or self._result.get('account_id') or ''}",
                    f"账号ID: {self._result.get('account_id', '')}",
                    f"密码: {self._result.get('password', '')}",
                    f"套餐: {self._result.get('plan_type', '')}",
                    "Activation ID: ",
                    f"注册代理: {self._result.get('registration_proxy', '')}",
                    f"注册出口IP: {self._result.get('registration_proxy_exit_ip', '')}",
                    f"ChatGPT access_token: {self._result.get('access_token', '')}",
                    "OAuth refresh_token: ",
                    "OAuth id_token: ",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        self._result["text_file"] = str(text_path)
        return text_path

    def _save_manual_plus_handoff_json(self, config: dict[str, Any], session: BrowserSession) -> Path:
        output_dir = self._output_dir(config)
        resume_id = config.get("resume_id") or f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        storage_state_path = self._ensure_browser_storage_state(config, session, resume_id)
        filename = Path(config.get("resume_out") or output_dir / f"resume_{resume_id}.json")
        data = {
            "schema_version": 1,
            "resume_id": resume_id,
            "stage": "manual_plus_required",
            "created_at": datetime.now().isoformat(),
            "account_key": self._result.get("email") or self._result.get("account_id") or "",
            "phone_number": "",
            "activation_id": "",
            "chatgpt_access_token_initial": self._result.get("access_token", ""),
            "access_token": self._result.get("access_token", ""),
            "session_token": self._result.get("session_token", ""),
            "chatgpt_account_id": self._result.get("account_id", ""),
            "account_id": self._result.get("account_id", ""),
            "email": self._result.get("email", ""),
            "outlook_email": config.get("outlook_email", ""),
            "generated_chatgpt_password": self._result.get("password", ""),
            "password": self._result.get("password", ""),
            "plan_type_before_activation": self._result.get("plan_type", ""),
            "plan_type": self._result.get("plan_type", ""),
            "browser_storage_state_path": storage_state_path,
            "registration_mode": self._result.get("registration_mode") or "email",
            "registration_status": "registered",
            "binding_status": "not_ready",
            "registration_task_id": self._result.get("registration_task_id", ""),
            "registration_proxy": self._result.get("registration_proxy", ""),
            "registration_proxy_exit_ip": self._result.get("registration_proxy_exit_ip", ""),
            "access_token_file": self._result.get("access_token_file", ""),
            "protocol_runner": self._result.get("protocol_runner", ""),
            "protocol_work_dir": self._result.get("protocol_work_dir", ""),
            "manual_plus_status": self._result.get("manual_plus_status") or "pending",
            "manual_plus_url": "",
            "manual_next_step": "Complete the required activation with an approved service, then run resume-oauth with --manual-plus-confirmed.",
        }
        filename.parent.mkdir(parents=True, exist_ok=True)
        with filename.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
        self._write_account_text(filename, stage="manual_plus_required")
        data["resume_file"] = str(filename)
        account_store.upsert_account(data, source_file=str(filename), copy_artifacts=True)
        return filename
