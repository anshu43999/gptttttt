import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from camoufox.sync_api import Camoufox
from core.proxy_utils import build_playwright_proxy_config

resume_path = PROJECT_ROOT / "output/resume_20260616_152329_2e27c2ec.json"
resume = json.loads(resume_path.read_text(encoding="utf-8"))
storage_state = str(PROJECT_ROOT / resume["browser_storage_state_path"])
proxy = resume.get("registration_proxy") or ""
exit_ip = resume.get("registration_proxy_exit_ip") or ""

launch_kwargs = {
    "headless": False,
    "os": ["windows"],
    "enable_cache": True,
    "humanize": False,
}
if proxy:
    launch_kwargs["proxy"] = build_playwright_proxy_config(proxy)
    launch_kwargs["geoip"] = exit_ip or True

BLOCK_HOSTS = (
    "browser-intake-datadoghq.com",
    "statsigapi.net",
    "events.statsigapi.net",
    "segment.io",
    "api.segment.io",
    "cdn.segment.com",
    "oaiusercontent.com",
)
BLOCK_PATHS = (
    "/ces/",
    "/backend-api/lat/r",
)
BLOCK_TYPES = {"image", "media", "font"}

print(f"Opening lightweight ChatGPT viewer with storage={storage_state}", flush=True)
print(f"Proxy={proxy} saved_exit_ip={exit_ip}", flush=True)

with Camoufox(**launch_kwargs) as browser:
    context = browser.new_context(
        no_viewport=False,
        viewport={"width": 1365, "height": 900},
        storage_state=storage_state,
        locale="zh-CN",
    )

    def route_handler(route, request):
        url = request.url.lower()
        if request.resource_type in BLOCK_TYPES:
            route.abort()
            return
        if any(host in url for host in BLOCK_HOSTS) or any(path in url for path in BLOCK_PATHS):
            route.abort()
            return
        route.continue_()

    context.route("**/*", route_handler)
    page = context.new_page()
    page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=60000)
    print(f"Lightweight ChatGPT window ready: {page.url}", flush=True)
    while True:
        time.sleep(5)
