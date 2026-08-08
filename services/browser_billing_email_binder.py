from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

import yaml
from core import account_store

from core.mailbox_providers import ICloudPrivacyMailbox, LinkApiMailbox
from core.proxy_utils import build_playwright_proxy_config
from core.proxy.credential_runtime import CredentialProxyRuntime
from core.mailbox.forwarded_domain import ForwardedDomainMailbox



def _mailbox_from_config(config: dict[str, Any]):
    provider = str(config.get("mailbox_provider") or config.get("email_provider") or "icloud_api").strip().lower()
    if provider == "icloud_privacy":
        return ICloudPrivacyMailbox.from_config(config)
    if provider == "forwarded_domain":
        return ForwardedDomainMailbox.from_config(config)
    return LinkApiMailbox.from_config(config)

CHATGPT = "https://chatgpt.com"
PRICING_URL = f"{CHATGPT}/?promo_campaign=plus-1-month-free#pricing"


def _log(message: str) -> None:
    print(message, flush=True)


def _load_yaml(path: str | Path) -> dict[str, Any]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def _load_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _artifact_dir(config: dict[str, Any]) -> Path:
    task_id = str(config.get("dashboard_task_id") or "manual").strip() or "manual"
    path = Path("tmp") / "billing_email_bind" / task_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _screenshot(page: Any, config: dict[str, Any], label: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in label)[:80]
    path = _artifact_dir(config) / f"{int(time.time())}_{safe}.png"
    try:
        page.screenshot(path=str(path), full_page=False)
        _log(f"[billing-email] screenshot {label}: {path}")
    except Exception as exc:
        _log(f"[billing-email] screenshot failed {label}: {str(exc)[:160]}")
    return str(path)


def _page_text(page: Any) -> str:
    try:
        return str(page.evaluate("() => document.body?.innerText || ''") or "")
    except Exception:
        return ""


def _email_already_linked(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in [
        "already linked to another account",
        "already associated with another account",
        "別のアカウントにすでにリンクされています",
        "別のアカウントにすでにリンク",
        "已被其他账号",
        "已关联到另一个账号",
        "已链接到另一个账号",
    ])


def _checkout_ready(text: str, url: str = "") -> bool:
    lowered = str(text or "").lower()
    return "checkout" in str(url or "").lower() or (
        ("chatgpt plus" in lowered or "無料トライアル" in str(text or ""))
        and ("カード番号" in str(text or "") or "card number" in lowered)
    )


def _mark_email_used(email: str, reason: str) -> None:
    try:
        con = sqlite3.connect("data/gpt_register.db")
        con.execute("update resource_pool set status='used', last_error=?, updated_at=datetime('now') where resource_type='email' and resource_key=?", (reason[:240], email))
        con.commit()
    except Exception as exc:
        _log(f"[billing-email] mark email used failed: {str(exc)[:160]}")


def _token_from_session(page: Any) -> str:
    result = page.evaluate(
        """
        async () => {
          const r = await fetch('/api/auth/session?refresh=true&reason=billing_email_bind', {credentials: 'include'});
          const text = await r.text();
          let data = {};
          try { data = JSON.parse(text); } catch (_) {}
          return {status: r.status, token: data.accessToken || '', user: data.user || {}, text: text.slice(0, 300)};
        }
        """
    )
    if not result.get("token"):
        raise RuntimeError(f"session missing accessToken: status={result.get('status')} body={result.get('text')}")
    _log(f"[billing-email] session ok status={result.get('status')} user_plan={result.get('user', {}).get('planType') or ''}")
    return str(result["token"])


def _fetch_json(page: Any, url: str, *, method: str = "GET", token: str = "", body: dict[str, Any] | None = None) -> dict[str, Any]:
    return page.evaluate(
        """
        async ({url, method, token, body}) => {
          const headers = {accept: 'application/json'};
          if (token) headers.authorization = `Bearer ${token}`;
          if (body !== null) headers['content-type'] = 'application/json';
          const r = await fetch(url, {
            method,
            credentials: 'include',
            headers,
            body: body === null ? undefined : JSON.stringify(body),
          });
          const text = await r.text();
          let data = {};
          try { data = JSON.parse(text); } catch (_) {}
          return {ok: r.ok, status: r.status, data, text: text.slice(0, 800)};
        }
        """,
        {"url": url, "method": method, "token": token, "body": body},
    )


def _click_free_trial_if_visible(page: Any) -> str:
    role_names = ["無料オファーを受け取る", "オファーを受け取る", "领取免费试用", "免费试用", "Start free trial", "Try for free"]
    for name in role_names:
        try:
            target = page.get_by_role("button", name=re.compile(re.escape(name))).last
            if target.count() > 0 and target.is_visible(timeout=1500) and target.is_enabled(timeout=1500):
                target.scroll_into_view_if_needed(timeout=3000)
                target.click(timeout=5000)
                return f"role:{name}"
        except Exception:
            continue
    selectors = [
        "button:has-text('领取免费试用')",
        "button:has-text('免费试用')",
        "button:has-text('無料オファーを受け取る')",
        "button:has-text('オファーを受け取る')",
        "button:has-text('無料で試す')",
        "button:has-text('無料トライアル')",
        "button:has-text('Start free trial')",
        "button:has-text('Try for free')",
    ]
    for selector in selectors:
        try:
            target = page.locator(selector).first
            if target.count() > 0 and target.is_visible(timeout=1500):
                target.scroll_into_view_if_needed(timeout=3000)
                target.click(timeout=5000)
                return selector
        except Exception:
            continue
    try:
        clicked = page.evaluate(
            """
            () => {
              const needles = ['無料オファーを受け取る', 'オファーを受け取る', 'Plus を1か月無料で試す', '無料で試す', '無料トライアル', '领取免费试用', '免费试用', 'Start free trial', 'Try for free'];
              const visible = (el) => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
              const candidates = [];
              for (const el of Array.from(document.querySelectorAll('button, [role="button"], a'))) {
                const text = String(el.innerText || el.textContent || el.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim();
                if (!text || !needles.some((needle) => text.includes(needle))) continue;
                const rect = el.getBoundingClientRect();
                if (visible(el)) {
                  candidates.push({el, text, score: (rect.width * rect.height) + (rect.left > window.innerWidth * 0.35 ? 100000 : 0) + (rect.top > 180 ? 50000 : 0) - Math.abs(text.length - 10)});
                }
              }
              candidates.sort((a, b) => b.score - a.score);
              if (candidates.length) {
                candidates[0].el.scrollIntoView({block: 'center', inline: 'center'});
                candidates[0].el.click();
                return candidates[0].text;
              }
              return '';
            }
            """
        )
        if clicked:
            return f"text:{clicked}"
    except Exception:
        pass
    return ""


def _wait_click_free_trial(page: Any, timeout: int = 30) -> str:
    deadline = time.time() + timeout
    clicked = ""
    while time.time() < deadline:
        clicked = _click_free_trial_if_visible(page)
        if clicked:
            return clicked
        time.sleep(1)
    return ""


def _click_ready_continue_if_visible(page: Any) -> str:
    body = _page_text(page)
    if not any(marker in body for marker in ["準備が完了しました", "準備完了", "Ready", "利用条件", "プライバシーポリシー"]):
        return ""
    for selector in ["button:has-text('続行')", "button:has-text('Continue')", "button[type='submit']"]:
        try:
            target = page.locator(selector).last
            if target.count() > 0 and target.is_visible(timeout=1500) and target.is_enabled(timeout=1500):
                target.scroll_into_view_if_needed(timeout=3000)
                target.click(timeout=5000)
                time.sleep(2)
                return selector
        except Exception:
            continue
    return ""


def _click_submit_like(page: Any) -> str:
    selectors = [
        "button[type='submit']",
        "button:has-text('続行')",
        "button:has-text('確認')",
        "button:has-text('送信')",
        "button:has-text('Continue')",
        "button:has-text('Verify')",
        "button:has-text('Send')",
        "button:has-text('验证')",
        "button:has-text('继续')",
    ]
    for selector in selectors:
        try:
            target = page.locator(selector).first
            if target.count() > 0 and target.is_visible(timeout=1200) and target.is_enabled(timeout=1200):
                target.scroll_into_view_if_needed(timeout=3000)
                target.click(timeout=5000)
                return selector
        except Exception:
            continue
    clicked = page.evaluate(
        """
        () => {
          const needles = ['続行', '確認', '送信', 'Continue', 'Verify', 'Send', '验证', '继续'];
          const visible = (el) => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
          for (const el of Array.from(document.querySelectorAll('button, [role="button"]'))) {
            const text = String(el.innerText || el.textContent || el.getAttribute('aria-label') || '').trim();
            if (visible(el) && !el.disabled && needles.some((needle) => text.includes(needle))) {
              el.scrollIntoView({block: 'center', inline: 'center'});
              el.click();
              return text;
            }
          }
          return '';
        }
        """
    )
    return f"text:{clicked}" if clicked else ""



def _has_billing_email_input(page: Any) -> bool:
    selectors = [
        "input[type='email']",
        "input[name='email']",
        "input[autocomplete='email']",
        "input[placeholder*='メール']",
        "input[placeholder*='Email']",
        "input[placeholder*='邮箱']",
    ]
    for selector in selectors:
        try:
            target = page.locator(selector).first
            if target.count() > 0 and target.is_visible(timeout=500):
                return True
        except Exception:
            continue
    return False


def _force_click_offer_button(page: Any) -> str:
    return str(page.evaluate(
        """
        () => {
          const needles = ['無料オファーを受け取る', 'オファーを受け取る', 'Plus を1か月無料で試す', '無料トライアル', 'Try Plus for free', 'Start free trial'];
          const visible = (el) => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
          const candidates = Array.from(document.querySelectorAll('button, a, [role="button"]'))
            .map((el) => ({el, text: String(el.innerText || el.textContent || el.getAttribute('aria-label') || '').trim()}))
            .filter(({el, text}) => visible(el) && needles.some((needle) => text.includes(needle)) && !el.disabled && el.getAttribute('aria-disabled') !== 'true');
          const picked = candidates[candidates.length - 1];
          if (!picked) return '';
          picked.el.scrollIntoView({block: 'center', inline: 'center'});
          picked.el.click();
          return picked.text;
        }
        """
    ) or "")


def _ensure_billing_entrypoint(page: Any, config: dict[str, Any], timeout: int = 90) -> str:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        text = _page_text(page)
        if _checkout_ready(text, str(page.url)):
            return "checkout_ready"
        if _has_billing_email_input(page):
            return "email_input_ready"
        ready_clicked = _click_ready_continue_if_visible(page)
        if ready_clicked:
            last = f"ready:{ready_clicked}"
            _log(f"[billing-email] clicked ready/terms modal while opening billing: {ready_clicked}")
            _screenshot(page, config, "entrypoint_after_ready_continue")
            time.sleep(2)
            continue
        clicked = _wait_click_free_trial(page, timeout=10)
        if not clicked:
            forced = _force_click_offer_button(page)
            clicked = f"force:{forced}" if forced else ""
        if clicked:
            last = clicked
            _log(f"[billing-email] billing entry click={clicked} url={page.url}")
            time.sleep(3)
            continue
        time.sleep(1)
    raise RuntimeError(f"未进入邮箱输入或 checkout: last_click={last} url={page.url} body={_page_text(page)[:240]}")

def _fill_billing_email_ui(page: Any, email: str) -> str:
    selectors = [
        "input[type='email']",
        "input[name='email']",
        "input[autocomplete='email']",
        "input[placeholder*='メール']",
        "input[placeholder*='Email']",
        "input[placeholder*='邮箱']",
    ]
    deadline = time.time() + 60
    last_body = ""
    while time.time() < deadline:
        for selector in selectors:
            try:
                target = page.locator(selector).first
                if target.count() > 0 and target.is_visible(timeout=1200):
                    target.fill(email, timeout=8000)
                    clicked = _click_submit_like(page)
                    return f"{selector}; submit={clicked or 'none'}"
            except Exception:
                continue
        try:
            last_body = page.evaluate("() => (document.body?.innerText || '').slice(0, 800)")
        except Exception:
            last_body = ""
        time.sleep(1)
    raise RuntimeError(f"未找到邮箱输入框: url={page.url} body={last_body[:240]}")


def _fill_billing_otp_ui(page: Any, code: str) -> str:
    code = str(code or "").strip()
    deadline = time.time() + 90
    selectors = [
        "input[autocomplete='one-time-code']",
        "input[inputmode='numeric']",
        "input[name*='code']",
        "input[placeholder*='コード']",
        "input[placeholder*='Code']",
        "input[placeholder*='验证码']",
        "input[placeholder*='XXXXXX']",
        "input[placeholder*='xxxxxx']",
        "input[type='text']",
    ]
    last_body = ""
    while time.time() < deadline:
        try:
            boxes = page.locator("input[inputmode='numeric'], input[autocomplete='one-time-code'], input[name*='code']")
            count = boxes.count()
            visible_boxes = []
            for index in range(min(count, 8)):
                candidate = boxes.nth(index)
                if candidate.is_visible(timeout=500):
                    visible_boxes.append(candidate)
            if len(visible_boxes) >= len(code) and len(code) >= 4:
                for index, char in enumerate(code):
                    visible_boxes[index].fill(char, timeout=3000)
                clicked = _click_submit_like(page)
                return f"split-inputs={len(visible_boxes)}; submit={clicked or 'none'}"
        except Exception:
            pass
        for selector in selectors:
            try:
                target = page.locator(selector).first
                if target.count() > 0 and target.is_visible(timeout=1200):
                    target.fill(code, timeout=8000)
                    clicked = _click_submit_like(page)
                    return f"{selector}; submit={clicked or 'none'}"
            except Exception:
                continue
        try:
            last_body = page.evaluate("() => (document.body?.innerText || '').slice(0, 800)")
        except Exception:
            last_body = ""
        time.sleep(1)
    raise RuntimeError(f"未找到邮箱 OTP 输入框: url={page.url} body={last_body[:240]}")


def _wait_email_bound(page: Any, token: str, email: str, config: dict[str, Any], timeout: int = 180) -> tuple[bool, dict[str, Any]]:
    deadline = time.time() + timeout
    last_me: dict[str, Any] = {}
    shot_at = 0.0
    while time.time() < deadline:
        text = _page_text(page)
        if _checkout_ready(text, str(page.url)):
            _log(f"[billing-email] checkout UI reached while waiting email bind: {page.url}")
            return True, {"status": 200, "data": {"email": email}, "source": "checkout_ui"}
        if "checkout" in str(page.url):
            _log(f"[billing-email] checkout reached while waiting email bind: {page.url}")
        try:
            me = _fetch_json(page, f"{CHATGPT}/backend-api/me", token=token)
            last_me = me
            me_email = str((me.get("data") or {}).get("email") or "")
            _log(f"[billing-email] wait_me status={me.get('status')} email={me_email} url={page.url}")
            if me_email.lower() == email.lower():
                return True, me
        except Exception as exc:
            _log(f"[billing-email] wait_me error: {str(exc)[:160]}")
        now = time.time()
        if now - shot_at > 30:
            _screenshot(page, config, "waiting_email_bound")
            shot_at = now
        time.sleep(5)
    return False, last_me


def run(resume_file: str, config_path: str, *, headed: bool = True) -> dict[str, Any]:
    config = _load_yaml(config_path)
    resume = _load_json(resume_file)
    storage = str(resume.get("browser_storage_state_path") or resume.get("storage_file") or "").strip()
    if not storage or not Path(storage).exists():
        raise RuntimeError(f"storage_state 不存在: {storage}")

    mailbox = _mailbox_from_config(config)
    preferred_email = ""
    raw_order = str(config.get("icloud_privacy_order_text") or config.get("icloud_api_order_text") or "")
    if "----" in raw_order:
        preferred_email = raw_order.split("----", 1)[0].strip().lower()
    elif raw_order.strip() and "@" in raw_order:
        preferred_email = raw_order.strip().lower()
    account = mailbox.account_for_email(preferred_email) if preferred_email else mailbox.create_account()
    before_ids = mailbox.get_current_ids(account)
    email = account.email
    _log(f"[billing-email] using {config.get('mailbox_provider') or 'icloud_api'} email={email}")

    proxy_url = str(resume.get("registration_proxy") or config.get("proxy") or "").strip()
    runtime = None
    browser_ctx = None
    browser = None
    context = None
    page = None
    try:
        proxy_config = None
        if proxy_url:
            runtime = CredentialProxyRuntime({**config, "lajiao_proxy_credential_protocol": config.get("lajiao_proxy_credential_protocol") or "socks5"}, log_fn=lambda m: _log(f"[billing-email]{m}"))
            bridge = runtime.start_browser_bridge(proxy_url)
            proxy_config = build_playwright_proxy_config(bridge)
            _log(f"[billing-email] proxy bridge={bridge}")

        from camoufox.sync_api import Camoufox

        failed = False
        kwargs: dict[str, Any] = {"headless": not headed, "os": ["windows", "macos", "linux"], "enable_cache": False, "humanize": True}
        if proxy_config:
            kwargs["proxy"] = proxy_config
        browser_ctx = Camoufox(**kwargs)
        browser = browser_ctx.__enter__()
        context = browser.new_context(no_viewport=True, storage_state=storage, locale="ja-JP", timezone_id="Asia/Tokyo")
        page = context.new_page()

        _log(f"[billing-email] opening pricing url={PRICING_URL}")
        page.goto(PRICING_URL, wait_until="domcontentloaded", timeout=90000)
        _screenshot(page, config, "pricing_loaded")
        time.sleep(4)
        entrypoint = _ensure_billing_entrypoint(page, config, timeout=120)
        _log(f"[billing-email] billing_entrypoint={entrypoint} url={page.url}")
        _screenshot(page, config, "after_billing_entrypoint")
        time.sleep(2)

        token = _token_from_session(page)
        me_before = _fetch_json(page, f"{CHATGPT}/backend-api/me", token=token)
        existing_email = str((me_before.get("data") or {}).get("email") or "")
        _log(f"[billing-email] me_before status={me_before.get('status')} email={existing_email} phone={(me_before.get('data') or {}).get('phone_number') or ''}")
        if existing_email:
            _log(f"[billing-email] account already has email={existing_email}; skip add_email/begin")
            email = existing_email
        else:
            filled = _fill_billing_email_ui(page, email)
            _log(f"[billing-email] ui_email_filled {filled}")
            time.sleep(3)
            _screenshot(page, config, "after_email_submit")
            page_text = _page_text(page)
            if _email_already_linked(page_text):
                _mark_email_used(email, "Email already linked to another account")
                raise RuntimeError(f"邮箱已被其他账号绑定，已标记 used: {email}")
            code = mailbox.wait_for_code(account, timeout=int(config.get("email_otp_timeout") or 600), before_ids=before_ids)
            _log(f"[billing-email] otp length={len(code or '')}")
            if "checkout" in str(page.url) or "無料トライアルを開始" in _page_text(page) or "free trial" in _page_text(page).lower():
                _log(f"[billing-email] otp already accepted; now on checkout url={page.url}")
                _screenshot(page, config, "checkout_after_manual_or_auto_otp")
            else:
                try:
                    otp_filled = _fill_billing_otp_ui(page, code)
                    _log(f"[billing-email] ui_otp_filled {otp_filled}")
                    _screenshot(page, config, "after_otp_submit")
                except RuntimeError as exc:
                    _log(f"[billing-email] otp input missing after code, retrying email modal: {str(exc)[:200]}")
                    _screenshot(page, config, "otp_input_missing_retry_email")
                    retry_before_ids = mailbox.get_current_ids(account)
                    page.goto(PRICING_URL, wait_until="domcontentloaded", timeout=90000)
                    _screenshot(page, config, "retry_pricing_loaded")
                    retry_click = _wait_click_free_trial(page, timeout=90)
                    _log(f"[billing-email] retry_free_trial_click={retry_click or 'not_found'} url={page.url}")
                    _screenshot(page, config, "retry_after_free_trial_click")
                    retry_fill = _fill_billing_email_ui(page, email)
                    _log(f"[billing-email] retry_ui_email_filled {retry_fill}")
                    time.sleep(3)
                    _screenshot(page, config, "retry_after_email_submit")
                    retry_text = _page_text(page)
                    if _email_already_linked(retry_text):
                        _mark_email_used(email, "Email already linked to another account")
                        raise RuntimeError(f"邮箱已被其他账号绑定，已标记 used: {email}")
                    retry_code = mailbox.wait_for_code(account, timeout=int(config.get("email_otp_timeout") or 600), before_ids=retry_before_ids)
                    _log(f"[billing-email] retry otp length={len(retry_code or '')}")
                    otp_filled = _fill_billing_otp_ui(page, retry_code)
                    _log(f"[billing-email] retry_ui_otp_filled {otp_filled}")
                    _screenshot(page, config, "retry_after_otp_submit")
            page.evaluate("async () => await fetch('/api/auth/session?refresh=true&reason=verify_otp', {credentials: 'include'})")
        bound_ok, me = _wait_email_bound(page, token, email, config, timeout=180)
        me_email = str((me.get("data") or {}).get("email") or "")
        _screenshot(page, config, "after_me_check")
        if not bound_ok:
            raise RuntimeError(f"邮箱绑定校验失败: expected={email} actual={me_email}")
        page.evaluate("async () => await fetch('/api/auth/session?refresh=true&reason=billing_email_bound', {credentials: 'include'})")
        refreshed_token = _token_from_session(page)

        context.storage_state(path=storage)
        result = {"ok": True, "email": email, "storage_state": storage, "url": str(page.url), "me_email": me_email, "token_length": len(refreshed_token)}
        updated_resume = {**resume, "access_token": refreshed_token, "chatgpt_access_token_initial": refreshed_token, "billing_email": email, "codex_email": email, "binding_status": "email_bound", "browser_storage_state_path": storage}
        Path(resume_file).write_text(json.dumps(updated_resume, ensure_ascii=False, indent=2), encoding="utf-8")
        account_store.upsert_account(updated_resume, source_file=str(resume_file), copy_artifacts=True)
        _log(f"[billing-email] done email={email} url={page.url}")
        if headed:
            _log("[billing-email] headed mode: success reached; closing after token refresh")
        return result
    finally:
        try:
            if context is not None:
                context.storage_state(path=storage)
        except Exception:
            pass
        try:
            if browser is not None:
                browser.close()
        except Exception:
            pass
        try:
            if browser_ctx is not None:
                browser_ctx.__exit__(None, None, None)
        except Exception:
            pass
        if runtime is not None:
            runtime.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description="Bind billing email in a saved ChatGPT browser session")
    parser.add_argument("--resume-file", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    result = run(args.resume_file, args.config, headed=args.headed)
    _log(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
