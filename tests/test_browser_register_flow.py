from __future__ import annotations

from platforms.chatgpt import browser_register


class FakePage:
    def __init__(self):
        self.url = "https://chatgpt.com/api/auth/callback/openai?code=abc&scope=openid"
        self.loaded = False
        self.waited = False

    def wait_for_load_state(self, *args, **kwargs):
        self.loaded = True

    def wait_for_url(self, predicate, **kwargs):
        self.waited = True
        assert predicate("https://chatgpt.com/")
        self.url = "https://chatgpt.com/"


def test_oauth_callback_navigation_waits_for_session(monkeypatch) -> None:
    page = FakePage()
    logs: list[str] = []
    monkeypatch.setattr(browser_register, "_wait_for_access_token", lambda page, timeout=60: "token")

    browser_register._complete_oauth_callback_navigation(
        page,
        {"page_type": "oauth_callback", "current_url": page.url},
        logs.append,
    )

    assert page.loaded is True
    assert page.waited is True
    assert page.url == "https://chatgpt.com/"
    assert any("access_token" in item for item in logs)
