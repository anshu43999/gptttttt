from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from full_pipeline import RegisterPipeline, load_config
from core.proxy_utils import build_playwright_proxy_config


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def mask_proxy(proxy: str) -> str:
    try:
        p = urlparse(proxy)
        if p.username or p.password:
            host = f"{p.hostname}:{p.port}" if p.port else str(p.hostname or "")
            return f"{p.scheme}://***:***@{host}"
    except Exception:
        pass
    return proxy


def safe_url(url: str) -> str:
    try:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}{p.path}"[:260]
    except Exception:
        return str(url)[:260]


def main() -> int:
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    cfg = load_config(config_path)

    out_dir = Path(cfg.get("output_dir") or "output") / "manual_observer"
    out_dir.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = out_dir / f"manual_register_observer_{run_id}.jsonl"
    text_log_path = out_dir / f"manual_register_observer_{run_id}.log"

    def emit(event: str, **data):
        item = {"ts": now(), "event": event, **data}
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
        msg = f"[{item['ts']}] {event} " + " ".join(f"{k}={v}" for k, v in data.items())
        with text_log_path.open("a", encoding="utf-8") as f:
            f.write(msg + "\n")
        print(msg, flush=True)

    # Prefer an explicit residential proxy if configured; otherwise try Lajiao.
    # If Lajiao rejects the current whitelist, fall back to the user's system/global proxy for manual observation only.
    explicit_proxy = str(cfg.get("manual_observer_proxy") or cfg.get("proxy") or "").strip()
    use_system_proxy = False
    if explicit_proxy:
        proxy = explicit_proxy
        exit_ip = ""
        emit("proxy_selected_explicit", proxy=mask_proxy(proxy))
    else:
        selector = RegisterPipeline(dict(cfg))
        try:
            proxy = selector._select_fresh_proxy_for_attempt()
        except Exception as exc:
            proxy = ""
            use_system_proxy = True
            emit("proxy_selection_failed_using_system", error=str(exc)[:500])
        exit_ip = str(selector.config.get("_camoufox_geoip_ip") or "")
        if proxy:
            emit("proxy_selected_lajiao_br", proxy=mask_proxy(proxy), exit_ip=exit_ip)
        elif not use_system_proxy:
            emit("proxy_selection_failed", reason=selector.result.get("failure_reason", ""))
            return 2

    proxy_config = build_playwright_proxy_config(proxy) if proxy else None

    from camoufox.sync_api import Camoufox

    launch_kwargs = {
        "headless": False,
        "os": ["windows", "macos", "linux"],
        "enable_cache": False,
        "humanize": True,
    }
    if proxy_config:
        launch_kwargs["proxy"] = proxy_config
        launch_kwargs["geoip"] = exit_ip or True
    elif use_system_proxy:
        launch_kwargs["geoip"] = True

    emit("browser_launching", engine="Camoufox", headed=True, proxy_mode=("system" if use_system_proxy else "explicit" if explicit_proxy else "lajiao"), geoip=exit_ip or "auto")
    with Camoufox(**launch_kwargs) as browser:
        ctx = browser.new_context(no_viewport=True)

        def binding(source, payload):
            try:
                if not isinstance(payload, dict):
                    payload = {"payload": str(payload)[:500]}
                emit("dom_action", **payload)
            except Exception as exc:
                emit("observer_binding_error", error=str(exc)[:300])

        ctx.expose_binding("__manualObserverLog", binding)
        ctx.add_init_script(
            r'''
(() => {
  if (window.__manualObserverInstalled) return;
  window.__manualObserverInstalled = true;
  const scrub = (value) => {
    value = String(value || '');
    if (!value) return '';
    return `[len=${value.length}]`;
  };
  const labelFor = (el) => {
    try {
      const id = el.id ? `#${el.id}` : '';
      const name = el.getAttribute('name') ? `[name=${el.getAttribute('name')}]` : '';
      const type = el.getAttribute('type') ? `[type=${el.getAttribute('type')}]` : '';
      const aria = el.getAttribute('aria-label') ? `[aria=${el.getAttribute('aria-label').slice(0,80)}]` : '';
      const text = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 100);
      return `${el.tagName.toLowerCase()}${id}${name}${type}${aria}${text ? ` text=${text}` : ''}`;
    } catch (e) { return 'unknown'; }
  };
  const send = (payload) => {
    try { window.__manualObserverLog(payload); } catch (e) {}
  };
  document.addEventListener('click', (e) => {
    const el = e.target && e.target.closest ? e.target.closest('button,a,input,textarea,select,[role="button"],[data-testid]') : e.target;
    send({kind:'click', url: location.href.slice(0,220), target: labelFor(el || e.target)});
  }, true);
  document.addEventListener('input', (e) => {
    const el = e.target;
    const type = (el && el.getAttribute && (el.getAttribute('type') || '') || '').toLowerCase();
    send({kind:'input', url: location.href.slice(0,220), target: labelFor(el), value: scrub(type === 'password' ? '' : el.value)});
  }, true);
  document.addEventListener('change', (e) => {
    const el = e.target;
    send({kind:'change', url: location.href.slice(0,220), target: labelFor(el), value: scrub(el && el.value)});
  }, true);
  document.addEventListener('submit', (e) => {
    send({kind:'submit', url: location.href.slice(0,220), target: labelFor(e.target)});
  }, true);
})();
            '''
        )

        page = ctx.new_page()

        def request_failed(req):
            try:
                failure = req.failure
                if callable(failure):
                    failure = failure()
            except Exception as exc:
                failure = f"failure-read-error:{exc}"
            emit("request_failed", method=req.method, url=safe_url(req.url), failure=str(failure)[:240])

        def response_seen(resp):
            url = resp.url
            if not re.search(r"auth\.openai\.com|chatgpt\.com|openai\.com", url, re.I):
                return
            path = safe_url(url)
            if re.search(r"authorize|create-account|log-in|phone-otp|user/register|create_account|callback|api/auth/session|api/auth/csrf|contact-verification", path, re.I):
                emit("response", status=resp.status, url=path)

        page.on("framenavigated", lambda frame: emit("navigated", url=safe_url(frame.url)) if frame == page.main_frame else None)
        page.on("requestfailed", request_failed)
        page.on("response", response_seen)
        page.on("console", lambda msg: emit("console", type=msg.type, text=str(msg.text)[:500]) if msg.type in ("error", "warning") else None)
        page.on("pageerror", lambda exc: emit("pageerror", error=str(exc)[:500]))
        page.on("close", lambda: emit("page_closed"))

        start_url = "https://chatgpt.com/auth/login"
        emit("goto", url=start_url)
        page.goto(start_url, wait_until="domcontentloaded", timeout=90000)
        emit("ready", log=str(text_log_path), jsonl=str(log_path))
        print("\n=== 手动注册观察窗口已打开 ===", flush=True)
        print("请只在这个 Camoufox 窗口里操作。关闭浏览器窗口即结束监听。", flush=True)
        print(f"日志: {text_log_path}", flush=True)

        while True:
            try:
                if page.is_closed():
                    break
                time.sleep(1)
            except KeyboardInterrupt:
                emit("keyboard_interrupt")
                break
            except Exception as exc:
                emit("observer_loop_error", error=str(exc)[:300])
                break

    emit("browser_closed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
