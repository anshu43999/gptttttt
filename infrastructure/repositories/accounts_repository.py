from __future__ import annotations

from pathlib import Path
from typing import Any

from domain.accounts import AccountQuery, AccountRecord, AccountStats
from infrastructure import db


class AccountsRepository:
    def __init__(self, db_path: str | Path | None = None):
        self.db_path = db_path
        db.init_db(db_path)

    def upsert(self, record: dict[str, Any]) -> AccountRecord:
        db.upsert_account(record, path=self.db_path)
        key = str(record.get("account_key") or record.get("account_id") or record.get("email") or record.get("phone_number") or "")
        return self.get(key)

    def activation_snapshot(self, key: str) -> dict[str, Any]:
        """Return the full account row needed by the durable UPI submit worker."""
        return db.get_account(key, path=self.db_path)

    def claim_queued_activation_submission(self, key: str, claim_id: str, client_key_hash: str) -> dict[str, Any]:
        return db.claim_queued_activation_submission(
            key,
            claim_id,
            client_key_hash,
            path=self.db_path,
        )

    def persist_claimed_activation_submission(
        self,
        key: str,
        claim_id: str,
        updates: dict[str, Any],
        *,
        event: str = "",
        message: str = "",
        clear_claim: bool = True,
    ) -> bool:
        return db.persist_claimed_activation_submission(
            key,
            claim_id,
            updates,
            event=event,
            message=message,
            clear_claim=clear_claim,
            path=self.db_path,
        )

    def list(self, query: AccountQuery | None = None) -> list[AccountRecord]:
        query = query or AccountQuery()
        items = [AccountRecord.from_dict(item) for item in db.list_accounts(path=self.db_path)]
        if query.stage:
            items = [item for item in items if item.stage == query.stage]
        if query.status:
            items = [item for item in items if item.status == query.status]
        if query.plan_type:
            items = [item for item in items if item.plan_type == query.plan_type]
        if query.search:
            needle = query.search.lower()
            items = [item for item in items if needle in " ".join([item.account_key, item.account_id, item.email, item.phone_number]).lower()]
        return items[: max(1, query.limit)]

    def get(self, key: str) -> AccountRecord:
        return AccountRecord.from_dict(db.get_account(key, path=self.db_path))

    def archive(self, key: str) -> bool:
        return db.archive_account(key, path=self.db_path)

    def stats(self) -> AccountStats:
        summary = db.summary(path=self.db_path)
        return AccountStats(total=int(summary.get("total") or 0), stages=summary.get("stages") or {}, plans=summary.get("plans") or {})
