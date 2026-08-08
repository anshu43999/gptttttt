from __future__ import annotations

from services.pipeline_runner import PipelineRunner


class ChatGptPipelineAdapter:
    """Thin platform adapter around the existing full_pipeline.py entrypoint."""

    platform = "chatgpt"

    def __init__(self, runner: PipelineRunner | None = None):
        self.runner = runner or PipelineRunner()

    def register_token_command(self, config_path: str, *, headed: bool = True) -> list[str]:
        return self.runner.register_token_command(config_path, headed=headed)

    def protocol_register_token_command(self, config_path: str, *, headed: bool = True, task_id: str = "") -> list[str]:
        return self.runner.protocol_register_token_command(config_path, headed=headed, task_id=task_id)

    def email_register_token_command(self, config_path: str, *, headed: bool = True) -> list[str]:
        return self.runner.email_register_token_command(config_path, headed=headed)

    def email_protocol_register_command(self, config_path: str, *, task_id: str = "") -> list[str]:
        return self.runner.email_protocol_register_command(config_path, task_id=task_id)

    def protocol_cpa_bind_command(self, config_path: str, account_key: str, *, task_id: str = "") -> list[str]:
        return self.runner.protocol_cpa_bind_command(config_path, account_key, task_id=task_id)

    def resume_oauth_command(self, config_path: str, resume_file: str, *, headed: bool = True) -> list[str]:
        return self.runner.resume_oauth_command(config_path, resume_file, headed=headed)

    def billing_email_bind_command(self, config_path: str, resume_file: str, *, headed: bool = True) -> list[str]:
        return self.runner.billing_email_bind_command(config_path, resume_file, headed=headed)
