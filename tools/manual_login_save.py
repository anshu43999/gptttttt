"""手动登录 OpenAI 账号并保存浏览器 session。

用法:
    python tools/manual_login_save.py --proxy http://127.0.0.1:7890

流程:
    1. 打开 Camoufox 浏览器 (headed)，代理走指定 proxy
    2. 导航到 auth.openai.com/login
    3. 人工在浏览器窗口登录
    4. 登录完成后在此终端按 Enter
    5. 保存 browser storage state → output/manual_storage_account1.json
    6. 浏览器登出，回到登录页，人工登录第二个号
    7. 按 Enter 保存 → output/manual_storage_account2.json
    8. 浏览器关闭
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 确保项目根在 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def main():
    parser = argparse.ArgumentParser(description="手动登录 OpenAI 并保存 browser storage state")
    parser.add_argument("--proxy", default="http://127.0.0.1:7890", help="代理 URL (默认 http://127.0.0.1:7890)")
    args = parser.parse_args()

    proxy_url = args.proxy.strip()

    print(f"[*] 代理: {proxy_url}")
    print("[*] 启动 Camoufox headed 浏览器...")

    from camoufox.sync_api import Camoufox

    from core.proxy_utils import build_playwright_proxy_config

    proxy_config = build_playwright_proxy_config(proxy_url)

    launch_kwargs = {
        "headless": False,
        "os": ["windows", "macos", "linux"],
        "humanize": False,
    }
    if proxy_config:
        launch_kwargs["proxy"] = proxy_config
        print(f"     proxy config: {proxy_config}")

    output_dir = PROJECT_ROOT / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    with Camoufox(**launch_kwargs) as browser:
        context = browser.new_context(no_viewport=True)
        page = context.new_page()

        for idx in (1, 2):
            print(f"\n{'='*60}")
            print(f"[*] 账号 {idx}: 请开始手动登录")
            print(f"    浏览器已打开 auth.openai.com/login")
            if idx == 2:
                print(f"    (如果还登录着上一个号，请先登出再登录新号)")
            print(f"    登录完成后，回到本终端按 Enter...")

            page.goto("https://auth.openai.com/login", wait_until="domcontentloaded", timeout=30000)

            input()  # 等用户按 Enter

            # 检查是否真的登录了
            try:
                page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=15000)
                current_url = page.url
                print(f"    当前 URL: {current_url}")
            except Exception:
                current_url = page.url
                print(f"    当前 URL: {current_url}")

            if "chatgpt.com" in current_url and "login" not in current_url:
                print(f"    ✓ 已登录 chatgpt.com")
            else:
                print(f"    ⚠ 未确认是否登录成功，URL = {current_url}，仍然保存 session...")

            # 保存 storage state
            storage_path = output_dir / f"manual_storage_account{idx}.json"
            context.storage_state(path=str(storage_path))
            print(f"    ✓ 浏览器 session 已保存: {storage_path}")

            if idx == 1:
                # 为第二个号登出
                print("\n[*] 登出当前账号，准备登录第二个号...")
                try:
                    page.goto("https://auth.openai.com/logout", wait_until="domcontentloaded", timeout=15000)
                except Exception:
                    pass
                try:
                    page.goto("https://chatgpt.com/api/auth/logout", wait_until="domcontentloaded", timeout=10000)
                except Exception:
                    pass
                input("    登出完成后按 Enter (浏览器会回到登录页)...")

        page.close()
        context.close()

    print("\n[*] 完成。已保存:")
    for idx in (1, 2):
        p = output_dir / f"manual_storage_account{idx}.json"
        if p.exists():
            print(f"    {p}  ({p.stat().st_size} bytes)")
        else:
            print(f"    {p}  (未找到)")


if __name__ == "__main__":
    main()
