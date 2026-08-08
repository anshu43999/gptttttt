from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Iterable

from services.task_runtime import subprocess_env

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class PipelineRunner:
    def __init__(self, project_root: str | Path = PROJECT_ROOT):
        self.project_root = Path(project_root)

    def register_token_command(self, config_path: str, *, headed: bool = True) -> list[str]:
        command = [sys.executable, "full_pipeline.py", "--config", config_path, "--step", "register-token"]
        if headed:
            command.append("--headed")
        return command

    def protocol_register_token_command(self, config_path: str, *, headed: bool = True, task_id: str = "") -> list[str]:
        command = [sys.executable, "-m", "services.codex_protocol_runner", "--config", config_path]
        if task_id:
            command.extend(["--task-id", task_id])
        return command

    def email_register_token_command(self, config_path: str, *, headed: bool = True) -> list[str]:
        command = [sys.executable, "-m", "services.modular_runner", "--task-type", "email-register-token", "--config", config_path]
        if headed:
            command.append("--headed")
        return command

    def email_protocol_register_command(self, config_path: str, *, task_id: str = "") -> list[str]:
        command = [sys.executable, "-m", "services.mailat_email_protocol_task", "--config", config_path]
        if task_id:
            command.extend(["--task-id", task_id])
        return command

    def protocol_cpa_bind_command(self, config_path: str, account_key: str, *, task_id: str = "") -> list[str]:
        command = [sys.executable, "-m", "services.mailat_protocol_bind_task", "--config", config_path, "--account-key", account_key]
        if task_id:
            command.extend(["--task-id", task_id])
        return command

    def resume_oauth_command(self, config_path: str, resume_file: str, *, headed: bool = True) -> list[str]:
        command = [sys.executable, "full_pipeline.py", "--config", config_path, "--step", "resume-oauth", "--resume-file", resume_file, "--manual-plus-confirmed"]
        if headed:
            command.append("--headed")
        return command

    def billing_email_bind_command(self, config_path: str, resume_file: str, *, headed: bool = True) -> list[str]:
        command = [sys.executable, "-m", "services.browser_billing_email_binder", "--config", config_path, "--resume-file", resume_file]
        if headed:
            command.append("--headed")
        return command

    def run_iter(self, command: list[str]) -> Iterable[str]:
        process = subprocess.Popen(
            command,
            cwd=str(self.project_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=subprocess_env(),
        )
        assert process.stdout is not None
        for line in process.stdout:
            yield line
        code = process.wait()
        if code != 0:
            raise subprocess.CalledProcessError(code, command)
