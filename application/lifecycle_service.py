"""
账号生命周期管理 — 定时刷新 token / 检查有效性 / 试用到期预警。
"""
from __future__ import annotations

import time
import threading
from datetime import datetime, timedelta

import requests

from infrastructure.repositories.accounts_repository import AccountsRepository
from infrastructure.db import connect


class LifecycleService:
    def __init__(self, db_path=None):
        self.db_path = db_path
        self.repo = AccountsRepository(db_path)

    def check_validity(self) -> dict:
        """检查所有活跃账号的 session token 有效性"""
        results = {"ok": 0, "expired": 0, "error": 0}
        for account in self.repo.list_active():
            try:
                access_token = (account.tokens or {}).get("access_token", "")
                if not access_token:
                    continue
                resp = requests.get(
                    "https://chatgpt.com/api/auth/session",
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=15,
                )
                if resp.status_code == 200:
                    results["ok"] += 1
                else:
                    self.repo.mark_expired(account.key)
                    results["expired"] += 1
            except Exception:
                results["error"] += 1
        return results

    def refresh_tokens(self) -> dict:
        """用 refresh_token 刷新 access_token"""
        results = {"refreshed": 0, "failed": 0, "skipped": 0}
        for account in self.repo.list_active():
            refresh_token = (account.tokens or {}).get("refresh_token", "")
            if not refresh_token:
                results["skipped"] += 1
                continue
            try:
                resp = requests.post(
                    "https://auth.openai.com/oauth/token",
                    data={
                        "grant_type": "refresh_token",
                        "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
                        "refresh_token": refresh_token,
                    },
                    timeout=30,
                )
                if resp.status_code == 200:
                    tokens = resp.json()
                    self.repo.update_tokens(account.key, tokens)
                    results["refreshed"] += 1
                else:
                    results["failed"] += 1
            except Exception:
                results["failed"] += 1
        return results

    def flag_expiring_trials(self, hours: int = 48) -> int:
        """标记即将到期的试用账号"""
        count = 0
        for account in self.repo.list_active():
            if account.plan_type == "free" and account.stage == "registered":
                created = account.created_at
                if created:
                    try:
                        created_dt = datetime.fromisoformat(created)
                        if datetime.utcnow() - created_dt > timedelta(days=28):
                            self.repo.update(account.key, stage="trial_expiring")
                            count += 1
                    except ValueError:
                        pass
        return count
