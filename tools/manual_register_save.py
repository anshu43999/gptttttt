from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import datetime
from urllib.parse import urlsplit
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from core.proxy_utils import build_playwright_proxy_config
from full_pipeline import RegisterPipeline, load_config


def _fetch_access_token(page) -> str:
    parsed = urlsplit(str(getattr(page, "url", "") or ""))
    if parsed.scheme != "https" or not parsed.netloc:
        return ""
    try:
        data = page.evaluate(
            """
            async () => {
              const resp = await fetch('/api/auth/session', {headers: {'accept': 'application/json'}});
              const text = await resp.text();
              try { return {status: resp.status, data: JSON.parse(text), text}; }
              catch { return {status: resp.status, data: null, text}; }
            }
            """
        )
        if isinstance(data, dict):
            payload = data.get("data") if isinstance(data.get("data"), dict) else {}
            return str(payload.get("accessToken") or payload.get("access_token") or "").strip()
    except Exception as exc:
        print(f"[!] access_token 提取失败: {exc}", flush=True)
    return ""


def _extract_account_id_from_storage(storage_path: Path) -> str:
    try:
        storage = json.loads(storage_path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    from urllib.parse import unquote

    for cookie in storage.get("cookies", []):
        if cookie.get("name") == "oai-client-auth-info":
            try:
                parsed = json.loads(unquote(str(cookie.get("value") or "")))
                return str((parsed.get("user") or {}).get("id") or "")
            except Exception:
                return ""
    return ""


def _write_resume_scripts(output_dir: Path, resume_path: Path, config_path: str) -> None:
    bat_path = output_dir / f"{resume_path.stem}.bat"
    ps1_path = output_dir / f"{resume_path.stem}.ps1"
    cmd = f'python full_pipeline.py --config "{config_path}" --step resume-oauth --resume-file "{resume_path}" --manual-plus-confirmed --headed'
    bat_path.write_text("@echo off\n" + cmd + "\npause\n", encoding="utf-8")
    ps1_path.write_text(cmd + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="手动注册后保存 OpenAI 浏览器缓存和 register-token 交接文件")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--phone", default="")
    parser.add_argument("--activation-id", default="")
    parser.add_argument("--password", default="")
    parser.add_argument("--done-file", default="tmp/manual_register_done.txt")
    parser.add_argument("--login-url", default="", help="可选：打开浏览器后的初始 HTTPS 地址")
    parser.add_argument("--no-proxy", action="store_true")
    args = parser.parse_args()

    config = load_config(args.config)
    config["headed"] = True
    pipeline = RegisterPipeline(config)

    proxy = ""
    exit_ip = ""
    if not args.no_proxy:
        proxy = pipeline._select_fresh_proxy_for_attempt()
        exit_ip = str(pipeline.result.get("registration_proxy_exit_ip") or "")

    from camoufox.sync_api import Camoufox

    launch_kwargs = {
        "headless": False,
        "os": ["windows", "macos", "linux"],
        "humanize": True,
    }
    proxy_config = build_playwright_proxy_config(proxy) if proxy else None
    if proxy_config:
        launch_kwargs["proxy"] = proxy_config
        launch_kwargs["geoip"] = exit_ip or True

    output_dir = PROJECT_ROOT / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    done_file = PROJECT_ROOT / args.done_file
    done_file.parent.mkdir(parents=True, exist_ok=True)
    if done_file.exists():
        done_file.unlink()

    resume_id = f"manual_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    storage_path = output_dir / f"storage_{resume_id}.json"
    registered_path = output_dir / "registered_accounts" / f"manual_{resume_id}.json"
    resume_path = output_dir / f"resume_{resume_id}.json"
    registered_path.parent.mkdir(parents=True, exist_ok=True)

    print("=" * 60, flush=True)
    print("手动注册窗口已准备", flush=True)
    print(f"proxy={proxy} exit_ip={exit_ip}", flush=True)
    print(f"phone={args.phone}", flush=True)
    print(f"activation_id={args.activation_id}", flush=True)
    print("请在打开的浏览器里手动完成注册。", flush=True)
    print(f"完成并确认已登录 ChatGPT 后，让主助手创建文件: {done_file}", flush=True)
    print("不要关闭浏览器；脚本检测到 done-file 后会保存缓存和交接 JSON。", flush=True)
    print("=" * 60, flush=True)

    with Camoufox(**launch_kwargs) as browser:
        context = browser.new_context(no_viewport=True, locale="ja-JP")
        page = context.new_page()
        if args.login_url:
            page.goto(args.login_url, wait_until="domcontentloaded", timeout=60000)

        while not done_file.exists():
            time.sleep(2)

        access_token = _fetch_access_token(page)
        context.storage_state(path=str(storage_path))
        account_id = _extract_account_id_from_storage(storage_path)

        base = {
            "schema_version": 1,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "phone_number": args.phone,
            "activation_id": args.activation_id,
            "account_id": account_id,
            "email": "",
            "password": args.password,
            "plan_type": "",
            "registration_proxy": proxy,
            "registration_proxy_exit_ip": exit_ip,
            "browser_storage_state_path": str(storage_path),
            "chatgpt_access_token_initial": access_token,
            "access_token": access_token,
        }
        registered_data = dict(base, stage="registered", resume_file=str(resume_path))
        resume_data = dict(
            base,
            resume_id=resume_id,
            stage="manual_plus_required",
            chatgpt_account_id=account_id,
            outlook_email=config.get("outlook_email", ""),
            generated_chatgpt_password=args.password,
            plan_type_before_activation="",
            manual_plus_status="pending",
            manual_plus_url="",
            manual_next_step="Complete the required activation with an approved service, then run resume-oauth with --manual-plus-confirmed.",
        )
        registered_path.write_text(json.dumps(registered_data, ensure_ascii=False, indent=2), encoding="utf-8")
        resume_path.write_text(json.dumps(resume_data, ensure_ascii=False, indent=2), encoding="utf-8")
        _write_resume_scripts(output_dir, resume_path, args.config)

        print(f"[OK] storage: {storage_path}", flush=True)
        print(f"[OK] registered: {registered_path}", flush=True)
        print(f"[OK] resume: {resume_path}", flush=True)
        print(f"[OK] access_token_len: {len(access_token)} account_id={account_id}", flush=True)
        page.close()
        context.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
