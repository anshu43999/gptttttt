from __future__ import annotations

from pathlib import Path
from typing import Any

from application.accounts_service import AccountsService
from application.config_service import ConfigService
from application.providers_service import ProvidersService
from application.tasks_service import TasksService
from core import account_store


class DashboardContext:
    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.task_root = project_root / "data" / "tasks"
        self.ui_path = project_root / "tools" / "register_web_dist" / "index.html"
        self.legacy_ui_path = project_root / "tools" / "register_web.html"
        self.ui_dist_path = project_root / "tools" / "register_web_dist"
        self.accounts = AccountsService()
        self.tasks = TasksService()
        self.config = ConfigService()
        self.providers = ProvidersService(config_service=self.config)

    def import_legacy(self, *, copy_artifacts: bool = False) -> int:
        return account_store.import_legacy_outputs(copy_artifacts=copy_artifacts)

    def summary(self) -> dict[str, Any]:
        return account_store.summary()
