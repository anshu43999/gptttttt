from __future__ import annotations

import argparse
from datetime import datetime
from typing import Any

from core import account_store
from core.config_loader import load_config
from services.mailat_protocol_runtime import PROJECT_ROOT
from services.mailat_protocol_bind_runner import normalize_oauth_callback_mode, run_mailat_protocol_cpa_bind
from infrastructure import db


def _persist_local_oauth_result(account: dict[str, Any], result: dict[str, Any], *, task_id: str) -> dict[str, Any]:
    oauth_result = result.get("oauth_result") if isinstance(result.get("oauth_result"), dict) else {}
    tokens = {name: str(oauth_result.get(name) or "").strip() for name in ("access_token", "refresh_token", "id_token")}
    missing = [name for name, value in tokens.items() if not value]
    if missing:
        raise RuntimeError(f"协议本地 OAuth 结果缺少 token: {', '.join(missing)}")

    updated = dict(account)
    raw_tokens = dict(account.get("tokens") or {}) if isinstance(account.get("tokens"), dict) else {}
    raw_tokens.update(tokens)
    expired = oauth_result.get("expired")
    if expired not in (None, ""):
        raw_tokens["token_expires_at"] = str(expired)
    updated["raw_tokens"] = raw_tokens
    if oauth_result.get("account_id"):
        updated["account_id"] = str(oauth_result.get("account_id") or "")
    if oauth_result.get("email"):
        updated["email"] = str(oauth_result.get("email") or "")
    updated.update(
        {
            "oauth_callback_mode": "local",
            "binding_status": "bound",
            "binding_completed_at": datetime.now().isoformat(timespec="seconds"),
            "binding_error": "",
            "cpa_submitted_at": "",
            "cpa_submit_status": "",
            "cpa_submit_error": "",
        }
    )
    db.upsert_account(updated)
    db.add_account_event(
        str(updated.get("account_key") or ""),
        "protocol_local_tokens_persisted",
        task_id=task_id,
        status="bound",
        message="协议本地 OAuth token 已写入账号数据库",
        payload={
            "oauth_auth_file": str(result.get("oauth_auth_file") or ""),
            "account_id": str(updated.get("account_id") or ""),
            "email": str(updated.get("email") or ""),
            "expired": str(expired or ""),
        },
    )
    return db.get_account(str(updated.get("account_key") or "")) or updated


def _account_identity(account: dict[str, Any]) -> str:
    return str(account.get("email") or account.get("login_identifier") or account.get("account_key") or "").strip()


def _restore_registration_mailbox_config(config: dict[str, Any], account: dict[str, Any]) -> dict[str, Any]:
    registration_task_id = str(account.get("registration_task_id") or "").strip()
    if not registration_task_id:
        return config
    source_path = PROJECT_ROOT / "data" / "tasks" / f"{registration_task_id}_config.yaml"
    if not source_path.exists():
        return config
    source_config = load_config(str(source_path))
    restored = dict(config)
    for key, value in source_config.items():
        key_lower = key.lower()
        if key_lower == "mailbox_provider" or any(part in key_lower for part in ("mailbox", "icloud", "outlook", "email_link", "forwarded")):
            if value not in (None, ""):
                restored[key] = value
    return restored


def run(config_path: str, *, account_key: str, task_id: str = "") -> dict[str, Any]:
    config = load_config(config_path)
    task_id = task_id or str(config.get("dashboard_task_id") or f"protocol_bind_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    account = account_store.get_account(account_key)
    if not account:
        raise RuntimeError(f"未找到账号: {account_key}")
    config = _restore_registration_mailbox_config(config, account)
    callback_mode = normalize_oauth_callback_mode(config.get("oauth_callback_mode"))
    config["oauth_callback_mode"] = callback_mode
    email = _account_identity(account)
    password = str(account.get("password") or config.get("chatgpt_password") or config.get("defaultPassword") or "").strip()
    if not email:
        raise RuntimeError(f"账号缺少邮箱/login_identifier: {account_key}")
    if not password:
        raise RuntimeError(f"账号缺少密码: {account_key}")

    mode_label = "本地 OAuth" if callback_mode == "local" else "CPA"
    print("=" * 60)
    print(f"Step: 协议 {mode_label} 绑定（项目内置 Mailat/Node 运行时）")
    print("=" * 60)
    print(f"账号: {email}")
    result = run_mailat_protocol_cpa_bind(
        config,
        email=email,
        password=password,
        task_id=task_id,
        log=print,
    )
    if callback_mode == "local":
        _persist_local_oauth_result(account, result, task_id=task_id)
    print("=" * 60)
    print("执行完成")
    print("=" * 60)
    print(f"  状态: {'protocol_local_bound' if callback_mode == 'local' else 'protocol_cpa_submitted'}")
    print("  成功: True")
    print(f"  邮箱: {email}")
    if result.get("binding_phone_number"):
        print(f"  绑定手机号: {result.get('binding_phone_number')}")
    if result.get("oauth_auth_file"):
        print(f"  OAuth 文件: {result.get('oauth_auth_file')}")
    print(f"  工作目录: {result.get('protocol_work_dir', '')}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Run project-local Mailat protocol binding for an existing account")
    parser.add_argument("--config", required=True)
    parser.add_argument("--account-key", required=True)
    parser.add_argument("--task-id", default="")
    args = parser.parse_args()
    run(args.config, account_key=args.account_key, task_id=args.task_id)


if __name__ == "__main__":
    main()
