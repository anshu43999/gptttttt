from __future__ import annotations

from pathlib import Path

HTML = Path("tools/register_web.html").read_text(encoding="utf-8")


def require(fragment: str) -> None:
    assert fragment in HTML, fragment


def test_account_filters_present() -> None:
    require('id="accountSearch"')
    require('id="accountStage"')
    require('function filteredAccounts()')
    require('renderAccounts()')


def test_task_timeline_present() -> None:
    require('id="taskEvents"')
    require('function renderTaskEvents(items)')
    require('/events`')


def test_queue_controls_present() -> None:
    require('id="cfgMaxParallel"')
    require('id="cfgRefreshSeconds"')
    require('max_parallel_tasks')
    require('dashboard_refresh_seconds')


def test_sensitive_controls_remain_masked() -> None:
    require("secret&&!visibleSecrets")
    require("toggleSecrets")


if __name__ == "__main__":
    test_account_filters_present()
    test_task_timeline_present()
    test_queue_controls_present()
    test_sensitive_controls_remain_masked()
    print("ui behavior tests passed")
