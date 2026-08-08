"""
统计服务 — 聚合 dashboard 数据。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from infrastructure.db import connect


class StatsService:
    def __init__(self, db_path=None):
        self.db_path = db_path

    def overview(self) -> dict:
        with connect(self.db_path) as conn:
            total = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
            active_plus = conn.execute(
                "SELECT COUNT(*) FROM accounts WHERE plan_type='plus' AND stage='complete'"
            ).fetchone()[0]
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            today_success = conn.execute(
                "SELECT COUNT(*) FROM registration_runs WHERE status='success' AND started_at LIKE ?",
                (f"{today}%",),
            ).fetchone()[0]
            today_fail = conn.execute(
                "SELECT COUNT(*) FROM registration_runs WHERE status='failed' AND started_at LIKE ?",
                (f"{today}%",),
            ).fetchone()[0]
        return {
            "total_accounts": total,
            "active_plus": active_plus,
            "today_success": today_success,
            "today_fail": today_fail,
        }

    def by_day(self, days: int = 7) -> list[dict]:
        results = []
        for i in range(days):
            date = (datetime.now(timezone.utc) - timedelta(days=i)).strftime("%Y-%m-%d")
            with connect(self.db_path) as conn:
                success = conn.execute(
                    "SELECT COUNT(*) FROM registration_runs WHERE status='success' AND started_at LIKE ?",
                    (f"{date}%",),
                ).fetchone()[0]
                fail = conn.execute(
                    "SELECT COUNT(*) FROM registration_runs WHERE status='failed' AND started_at LIKE ?",
                    (f"{date}%",),
                ).fetchone()[0]
            results.append({"date": date, "success": success, "fail": fail, "total": success + fail})
        return results

    def by_proxy(self) -> list[dict]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT exit_ip, region, success_count, fail_count, is_active FROM proxies ORDER BY success_count DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def errors(self, limit: int = 20) -> list[dict]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """SELECT errors, COUNT(*) as cnt FROM registration_runs
                   WHERE status='failed' AND errors!='[]'
                   GROUP BY errors ORDER BY cnt DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]
