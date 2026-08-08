"""FastAPI dependency injection."""
from fastapi import Depends

from application.accounts_service import AccountsService
from application.account_health_service import AccountHealthService
from application.tasks_service import TasksService
from application.config_service import ConfigService
from application.providers_service import ProvidersService
from application.browser_session_service import BrowserSessionService

_config_service = ConfigService()
_tasks_service = TasksService(config_service=_config_service)
_browser_session_service = BrowserSessionService(config_service=_config_service)
try:
    _tasks_service.ensure_reconcile_loop()
except Exception:
    pass




def get_accounts_service() -> AccountsService:
    return AccountsService()


def get_account_health_service() -> AccountHealthService:
    return AccountHealthService(config_service=_config_service)


def get_tasks_service() -> TasksService:
    return _tasks_service


def get_config_service() -> ConfigService:
    return _config_service


def get_providers_service(config_svc=Depends(get_config_service)) -> ProvidersService:
    return ProvidersService(config_service=config_svc)


def get_browser_session_service() -> BrowserSessionService:
    return _browser_session_service
