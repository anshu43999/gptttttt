"""从 saved storage state 提取 access_token 并写入 resume JSON。"""

import json
import sys

STORAGE = r"output/manual_storage_account1.json"
RESUME = r"output/resume_manual_account1.json"


def main():
    from camoufox.sync_api import Camoufox

    with open(STORAGE, "r") as f:
        storage = json.load(f)

    print("[*] 打开 Camoufox 提取 access_token...")

    with Camoufox(headless=True, os=["windows", "macos", "linux"], humanize=False) as browser:
        context = browser.new_context(
            no_viewport=True,
            storage_state=STORAGE,
        )
        page = context.new_page()

        try:
            page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass

        current_url = page.url
        print(f"  URL: {current_url}")
        if "login" in current_url or "auth" in current_url:
            print("  ✗ session 已过期")
            page.close()
            context.close()
            sys.exit(1)

        # 提取 token
        try:
            session_data = page.evaluate("""async () => {
                const resp = await fetch('https://chatgpt.com/api/auth/session');
                if (!resp.ok) return {error: resp.status};
                return await resp.json();
            }""")
            access_token = (session_data.get("accessToken") or "").strip() if isinstance(session_data, dict) else ""
            print(f"  session result keys: {list(session_data.keys()) if isinstance(session_data, dict) else 'N/A'}")
            print(f"  access_token len: {len(access_token)}")
        except Exception as e:
            print(f"  error: {e}")
            access_token = ""

        if not access_token:
            print("  ✗ 无法获取 access_token")
            page.close()
            context.close()
            sys.exit(1)

        page.close()
        context.close()

    # 更新 resume
    with open(RESUME, "r") as f:
        resume = json.load(f)

    resume["access_token"] = access_token
    resume["chatgpt_access_token_initial"] = access_token

    with open(RESUME, "w") as f:
        json.dump(resume, f, indent=2, ensure_ascii=False)

    print(f"  ✓ 已更新 resume: {RESUME}")


if __name__ == "__main__":
    main()
