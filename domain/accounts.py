from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AccountRecord:
    id: int = 0
    account_key: str = ""
    account_id: str = ""
    platform: str = "chatgpt"
    phone_number: str = ""
    email: str = ""
    billing_email: str = ""
    codex_email: str = ""
    password: str = ""
    plan_type: str = ""
    status: str = ""
    stage: str = ""
    registration_mode: str = ""
    display_name: str = ""
    login_identifier: str = ""
    registration_status: str = ""
    registration_task_id: str = ""
    registration_started_at: str = ""
    registration_completed_at: str = ""
    registration_error: str = ""
    plus_status: str = ""
    plus_verified_at: str = ""
    plus_check_source: str = ""
    plus_check_error: str = ""
    binding_status: str = ""
    binding_task_id: str = ""
    binding_provider: str = ""
    binding_phone_number: str = ""
    binding_completed_at: str = ""
    binding_started_at: str = ""
    oauth_callback_mode: str = ""
    cpa_base_url: str = ""
    cpa_submitted_at: str = ""
    cpa_submit_status: str = ""
    cpa_submit_error: str = ""
    cpa_auth_file_name: str = ""
    cpa_auth_file_json: str = ""
    cpa_synced_at: str = ""
    cpa_sync_error: str = ""
    registration_phone_resource_id: int = 0
    binding_phone_resource_id: int = 0
    email_resource_id: int = 0
    proxy_resource_id: int = 0
    registration_proxy_exit_ip: str = ""
    registration_proxy_region: str = ""
    resume_file: str = ""
    storage_file: str = ""
    account_file: str = ""
    account_health_status: str = ""
    account_health_checked_at: str = ""
    account_health_source: str = ""
    account_health_error: str = ""
    account_health_detail_json: str = ""
    export_status: str = ""
    export_kind: str = ""
    exported_at: str = ""
    activation_provider: str = ""
    activation_client_key_hash: str = ""
    activation_status: str = ""
    activation_channel: str = ""
    activation_task_id: str = ""
    activation_idempotency_key: str = ""
    activation_attempt: int = 0
    activation_error: str = ""
    activation_display: str = ""
    activation_can_release: int = 0
    activation_cdk_consumed: int = 0
    activation_submitted_at: str = ""
    activation_finished_at: str = ""
    activation_updated_at: str = ""
    binding_error: str = ""
    created_at: str = ""
    updated_at: str = ""
    last_error: str = ""
    paths: dict[str, str] = field(default_factory=dict)
    proxy: dict[str, str] = field(default_factory=dict)
    tokens: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AccountRecord":
        allowed = {field_name for field_name in cls.__dataclass_fields__}
        payload = {key: data.get(key) for key in allowed if key in data}
        return cls(**payload)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "account_key": self.account_key,
            "account_id": self.account_id,
            "platform": self.platform,
            "phone_number": self.phone_number,
            "email": self.email,
            "billing_email": self.billing_email,
            "codex_email": self.codex_email,
            "password": self.password,
            "plan_type": self.plan_type,
            "status": self.status,
            "stage": self.stage,
            "registration_mode": self.registration_mode,
            "login_identifier": self.login_identifier,
            "registration_status": self.registration_status,
            "registration_task_id": self.registration_task_id,
            "registration_started_at": self.registration_started_at,
            "registration_completed_at": self.registration_completed_at,
            "registration_error": self.registration_error,
            "display_name": self.display_name,
            "plus_status": self.plus_status,
            "plus_verified_at": self.plus_verified_at,
            "plus_check_source": self.plus_check_source,
            "plus_check_error": self.plus_check_error,
            "binding_status": self.binding_status,
            "binding_task_id": self.binding_task_id,
            "binding_provider": self.binding_provider,
            "binding_phone_number": self.binding_phone_number,
            "binding_completed_at": self.binding_completed_at,
            "binding_started_at": self.binding_started_at,
            "oauth_callback_mode": self.oauth_callback_mode,
            "cpa_base_url": self.cpa_base_url,
            "cpa_submitted_at": self.cpa_submitted_at,
            "cpa_submit_status": self.cpa_submit_status,
            "cpa_submit_error": self.cpa_submit_error,
            "cpa_auth_file_name": self.cpa_auth_file_name,
            "cpa_auth_file_json": self.cpa_auth_file_json,
            "cpa_synced_at": self.cpa_synced_at,
            "cpa_sync_error": self.cpa_sync_error,
            "registration_phone_resource_id": self.registration_phone_resource_id,
            "binding_phone_resource_id": self.binding_phone_resource_id,
            "email_resource_id": self.email_resource_id,
            "proxy_resource_id": self.proxy_resource_id,
            "registration_proxy_exit_ip": self.registration_proxy_exit_ip,
            "registration_proxy_region": self.registration_proxy_region,
            "resume_file": self.resume_file,
            "storage_file": self.storage_file,
            "account_file": self.account_file,
            "account_health_status": self.account_health_status,
            "account_health_checked_at": self.account_health_checked_at,
            "account_health_source": self.account_health_source,
            "account_health_error": self.account_health_error,
            "account_health_detail_json": self.account_health_detail_json,
            "export_status": self.export_status,
            "export_kind": self.export_kind,
            "exported_at": self.exported_at,
            "activation_provider": self.activation_provider,
            "activation_client_key_hash": self.activation_client_key_hash,
            "activation_status": self.activation_status,
            "activation_channel": self.activation_channel,
            "activation_task_id": self.activation_task_id,
            "activation_idempotency_key": self.activation_idempotency_key,
            "activation_attempt": self.activation_attempt,
            "activation_error": self.activation_error,
            "activation_display": self.activation_display,
            "activation_can_release": self.activation_can_release,
            "activation_cdk_consumed": self.activation_cdk_consumed,
            "activation_submitted_at": self.activation_submitted_at,
            "activation_finished_at": self.activation_finished_at,
            "activation_updated_at": self.activation_updated_at,
            "binding_error": self.binding_error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_error": self.last_error,
            "paths": dict(self.paths),
            "proxy": dict(self.proxy),
            "tokens": dict(self.tokens),
        }

    @property
    def needs_manual_plus(self) -> bool:
        return self.stage == "manual_plus_required"

    @property
    def complete(self) -> bool:
        return self.stage == "complete" or self.status == "complete"


@dataclass
class AccountQuery:
    status: str = ""
    stage: str = ""
    plan_type: str = ""
    search: str = ""
    limit: int = 200


@dataclass
class AccountStats:
    total: int
    stages: dict[str, int]
    plans: dict[str, int]
