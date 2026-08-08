from __future__ import annotations

import argparse
import sys
import traceback
from typing import Any

from core.config_loader import load_config
from registration.email_register import EmailRegistrationOrchestrator


def _print_summary(result: dict[str, Any]) -> None:
    print("=" * 60)
    print("执行完成")
    print("=" * 60)
    print(f"  状态: {result.get('status', '')}")
    print(f"  成功: {bool(result.get('success'))}")
    print(f"  邮箱: {result.get('email') or result.get('outlook_email') or ''}")
    print(f"  账号ID: {result.get('account_id', '')}")
    print(f"  已执行步骤: {', '.join(result.get('steps', []))}")
    if result.get("resume_file"):
        print(f"  交接文件: {result.get('resume_file')}")
    if result.get("registered_file"):
        print(f"  注册文件: {result.get('registered_file')}")
    if result.get("text_file"):
        print(f"  文本文件: {result.get('text_file')}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GPT Register modular task runner")
    parser.add_argument("--task-type", choices={"email-register-token"}, required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
        if args.headed:
            config["headed"] = True
        if args.task_type == "email-register-token":
            result = EmailRegistrationOrchestrator(log_fn=print).run(
                config,
                headed=bool(args.headed or config.get("headed", False)),
                task_id=str(config.get("dashboard_task_id") or ""),
            )
        else:
            raise RuntimeError(f"unsupported task type: {args.task_type}")
        _print_summary(result)
        if result.get("success") is True:
            return 0
        reason = result.get("failure_reason") or result.get("status") or "unknown failure"
        print(f"流水线异常: {reason}")
        return 1
    except Exception as exc:
        print(f"流水线异常: {exc}")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
