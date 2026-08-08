from __future__ import annotations

from pathlib import Path
from typing import Any

from infrastructure import db


class ConfigRepository:
    def __init__(self, db_path: str | Path | None = None):
        self.db_path = db_path
        db.init_db(db_path)

    def get_all(self) -> dict[str, Any]:
        return db.get_config(path=self.db_path)

    def set_many(self, values: dict[str, Any]) -> dict[str, Any]:
        for key, value in values.items():
            db.set_config(str(key), value, path=self.db_path)
        return self.get_all()
