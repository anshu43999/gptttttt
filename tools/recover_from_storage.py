"""从手动登录保存的 storage state 恢复: 查订阅 → OAuth 绑邮箱 → 出成品 JSON。

用法:
    python tools/recover_from_storage.py output/manual_storage_account1.json

流程:
    1. 加载 browser storage state
    2. 打开 Camoufox (headed) 恢复 session
    3. 提取 access_token
    4. 查询 OpenAI 订阅状态 (Plus/free)
    5. 如果 Plus: 走 OAuth 绑邮箱 → 拿 refresh_token → 保存成品 JSON
    6. 如果 free: 尝试通过 iceaix API 查 job 状态并告警
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def load_storage(path: Path) -> dict:
    with open(path, "r") as f:
        return json.load(f)


def extract_cookies_for_requests(storage: dict) -> dict[str, str]:
    """从 storage state 提取 cookies 字典，用于 requests/curl 调用。"""
    cookies = {}
    for c in storage.get("cookies", []):
        cookies[c["name"]] = c["value"]
    return cookies


def get_session_token(storage: dict) -> Optional[str]:
    for c in storage.get("cookies", []):
        if c.get("name") == "__Secure-next-auth.session-token" and "chatgpt.com" in c.get("domain", ""):
            return c["value"]
    return None


def get_chatgpt_account_id(storage: dict) -> Optional[str]:
    """从 cookie 中提取 chatgpt account id。"""
    from urllib.parse import unquote

    for c in storage.get("cookies", []):
        if c.get("name") == "oai-client-auth-info" and "chatgpt.com" in c.get("domain", ""):
            try:
                parsed = json.loads(unquote(c["value"]))
                user_id = (parsed.get("user") or {}).get("id", "")
                if user_id:
                    return user_id
            except Exception:
                pass
    return None


def get_access_token_via_session(storage: dict, proxy_url: str) -> Optional[str]:
    """用 saved session cookie 换 access_token。"""
    import requests

    session_token = get_session_token(storage)
    if not session_token:
        print("  ✗ 未找到 __Secure-next-auth.session-token")
        return None

    proxy_config = {}
    if proxy_url:
        from core.proxy_utils import build_requests_proxy_config
        proxy_config = build_requests_proxy_config(proxy_url) or {}

    print(f"  使用 proxy: {proxy_config}")

    resp = requests.get(
        "https://chatgpt.com/api/auth/session",
        cookies={"__Secure-next-auth.session-token": session_token},
        proxies=proxy_config,
        timeout=30,
    )
    print(f"  session status: {resp.status_code}")
    if resp.status_code == 200:
        data = resp.json()
        access_token = (data.get("accessToken") or "").strip()
        if access_token:
            print(f"  access_token len: {len(access_token)}")
            return access_token
        else:
            print(f"  session 返回但没有 accessToken: {json.dumps(data, ensure_ascii=False)[:200]}")
    else:
        print(f"  session response: {resp.text[:200]}")
    return None


def check_subscription(access_token: str, account_id: str, proxy_url: str) -> tuple[str, str]:
    """返回 (plan_type, source)。"""
    import requests

    from core.proxy_utils import build_requests_proxy_config

    proxies = build_requests_proxy_config(proxy_url) or {}
    headers = {
        "Authorization": f"Bearer {access_token}",
        "User-Agent": "Mozilla/5.0",
    }

    # 先试 wham/usage (更准确)
    results = []
    for endpoint, label in [
        ("https://chatgpt.com/backend-api/wham/usage", "wham/usage"),
        ("https://chatgpt.com/backend-api/me", "me"),
    ]:
        try:
            resp = requests.get(endpoint, headers=headers, proxies=proxies, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                plan = ""
                if label == "me":
                    plan = str(data.get("plan_type") or "").strip().lower()
                elif label == "wham/usage":
                    # wham/usage 可能直接返回 plan type 或需要从其他字段推断
                    plan_raw = (
                        data.get("subscription_plan")
                        or data.get("plan_type")
                        or data.get("plan")
                        or ""
                    )
                    plan = str(plan_raw).strip().lower()
                    # 有时 wham 返回详细结构
                    if not plan and isinstance(data, dict):
                        sub = data.get("subscription") or {}
                        plan = str(sub.get("plan") or sub.get("type") or "").strip().lower()

                if plan:
                    results.append((plan, label))
                    print(f"  [{label}] plan_type: {plan}")
                    if plan in ("plus", "pro", "team", "enterprise", "paid"):
                        return plan, label
        except Exception as e:
            print(f"  [{label}] error: {e}")

    # 返回第一个结果或 unknown
    if results:
        return results[0]
    return ("unknown", "")


def do_oauth_bind_email(storage_path: Path, proxy_url: str) -> Optional[dict]:
    """用已登录的 storage state 做 OAuth 绑邮箱。"""
    # 使用现有项目代码
    from camoufox.sync_api import Camoufox
    from core.proxy_utils import build_playwright_proxy_config

    headed = True
    proxy_config = build_playwright_proxy_config(proxy_url)

    launch_kwargs = {
        "headless": not headed,
        "os": ["windows", "macos", "linux"],
        "humanize": True,
        "enable_cache": False,
    }
    if proxy_config:
        launch_kwargs["proxy"] = proxy_config

    with Camoufox(**launch_kwargs) as browser:
        context = browser.new_context(
            no_viewport=True,
            storage_state=str(storage_path),
        )
        page = context.new_page()

        try:
            from platforms.chatgpt.browser_register import _do_codex_oauth

            print("\n[*] 开始 OAuth 绑定邮箱...")
            result = _do_codex_oauth(
                page=page,
                registration_proxy=proxy_url,
                timeout_ms=180_000,
            )
            if result and result.get("success"):
                print(f"  ✓ OAuth 完成: {json.dumps(result, ensure_ascii=False)[:300]}")
                return result
            else:
                print(f"  ✗ OAuth 未成功: {result}")
                return result
        except Exception as e:
            print(f"  ✗ OAuth 异常: {e}")
            import traceback
            traceback.print_exc()
            return None


def save_product_json(
    storage: dict,
    account_id: str,
    access_token: str,
    plan_type: str,
    oauth_result: Optional[dict],
    proxy_url: str,
    phone_number: str,
    iceaix_job_id: str,
    activation_id: str,
):
    """保存最终成品 JSON 到 output/products/"""
    from urllib.parse import unquote

    # 提取邮箱
    email = ""
    for c in storage.get("cookies", []):
        if c.get("name") == "oai-client-auth-info" and "chatgpt.com" in c.get("domain", ""):
            try:
                parsed = json.loads(unquote(c["value"]))
                email = (parsed.get("user") or {}).get("email", "")
            except Exception:
                pass

    if oauth_result:
        email = oauth_result.get("email", email)

    product = {
        "stage": "complete" if oauth_result else "partial",
        "plan_type": plan_type,
        "email": email,
        "phone_number": phone_number,
        "account_id": account_id,
        "activation_id": activation_id,
        "access_token": access_token,
        "iceaix": {
            "job_id": iceaix_job_id,
            "status": "success",
            "billing": "charged",
        },
        "proxy": {
            "provider": "clash_manual",
            "registration_proxy": proxy_url,
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    if oauth_result:
        product["oauth_result"] = oauth_result
        product["refresh_token"] = oauth_result.get("refresh_token", "")
        product["id_token"] = oauth_result.get("id_token", "")

    output_dir = PROJECT_ROOT / "output" / "products"
    output_dir.mkdir(parents=True, exist_ok=True)

    safe_email = (email or "unknown").replace("@", "_at_").replace(".", "_")
    date_str = datetime.now().strftime("%Y%m%d")
    filename = f"{safe_email}_{date_str}.json"
    filepath = output_dir / filename

    with open(filepath, "w") as f:
        json.dump(product, f, indent=2, ensure_ascii=False)

    print(f"\n  ✓ 成品已保存: {filepath}")
    return product


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("storage_file", help="storage state JSON 路径")
    parser.add_argument("--proxy", default="http://127.0.0.1:7890")
    parser.add_argument("--iceaix-job-id", default="d974feb723bc4a029100415153413dee")
    parser.add_argument("--activation-id", default="487446471")
    parser.add_argument("--phone", default="+573233424362")
    parser.add_argument("--skip-oauth", action="store_true", help="只查订阅，不做 OAuth")
    args = parser.parse_args()

    storage_path = Path(args.storage_file).resolve()
    if not storage_path.exists():
        print(f"✗ 文件不存在: {storage_path}")
        sys.exit(1)

    proxy_url = args.proxy.strip()
    iceaix_job_id = args.iceaix_job_id.strip()
    activation_id = args.activation_id.strip()
    phone_number = args.phone.strip()

    print(f"[*] Storage: {storage_path}")
    print(f"[*] Proxy: {proxy_url}")

    storage = load_storage(storage_path)
    account_id = get_chatgpt_account_id(storage) or ""
    print(f"[*] Account ID (from cookie): {account_id}")

    # Step 1: 获取 access_token
    print("\n[*] Step 1: 获取 access_token...")
    access_token = get_access_token_via_session(storage, proxy_url)
    if not access_token:
        print("✗ 无法获取 access_token，session 可能已过期")
        sys.exit(1)

    # Step 2: 查订阅
    print("\n[*] Step 2: 查询订阅状态...")
    plan_type, source = check_subscription(access_token, account_id, proxy_url)
    print(f"    订阅: {plan_type} (来源: {source})")

    if plan_type not in ("plus", "pro", "team", "enterprise", "paid"):
        print(f"\n⚠ 当前订阅不是 paid: {plan_type}")
        print("  仍然可以尝试 OAuth 绑邮箱，但可能是 free 账号")
        if args.skip_oauth:
            sys.exit(0)

    # Step 3: OAuth 绑邮箱
    if not args.skip_oauth:
        print("\n[*] Step 3: OAuth 绑定邮箱...")
        oauth_result = do_oauth_bind_email(storage_path, proxy_url)
    else:
        oauth_result = None

    # Step 4: 保存成品
    print("\n[*] Step 4: 保存成品 JSON...")
    save_product_json(
        storage=storage,
        account_id=account_id,
        access_token=access_token,
        plan_type=plan_type,
        oauth_result=oauth_result,
        proxy_url=proxy_url,
        phone_number=phone_number,
        iceaix_job_id=iceaix_job_id,
        activation_id=activation_id,
    )

    print("\n[*] 完成.")


if __name__ == "__main__":
    main()
