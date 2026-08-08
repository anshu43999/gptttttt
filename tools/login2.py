"""单个账号手动登录保存 storage state。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from camoufox.sync_api import Camoufox
from core.proxy_utils import build_playwright_proxy_config

PROXY = "http://127.0.0.1:7890"
OUT = Path("output/manual_storage_account2.json")

proxy_config = build_playwright_proxy_config(PROXY)
launch_kwargs = {
    "headless": False,
    "os": ["windows", "macos", "linux"],
    "humanize": False,
}
if proxy_config:
    launch_kwargs["proxy"] = proxy_config

with Camoufox(**launch_kwargs) as browser:
    context = browser.new_context(no_viewport=True)
    page = context.new_page()
    page.goto("https://auth.openai.com/login", wait_until="domcontentloaded", timeout=30000)
    print("[*] 请在浏览器中登录 +573181056482，完成后回终端按 Enter...")
    input()
    page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=15000)
    url = page.url
    if "chatgpt.com" in url and "login" not in url:
        print(f"  OK: {url}")
    else:
        print(f"  WARN: {url}")
    context.storage_state(path=str(OUT))
    print(f"  Saved: {OUT} ({OUT.stat().st_size} bytes)")
    page.close()
    context.close()
