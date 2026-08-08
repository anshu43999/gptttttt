"""Phone binding support for resumed ChatGPT/Codex OAuth flows."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from application.resource_pool_service import (
    BIND_PHONE_FAILURE_OTP_SUBMIT_TRANSPORT,
    BIND_PHONE_FAILURE_RELEASED,
    BIND_PHONE_FAILURE_TRANSPORT,
    BIND_PHONE_OUTCOME_RELEASED,
    BIND_PHONE_OUTCOME_SUCCESS,
    classify_bind_phone_failure,
)
from core.base_sms import BaseSmsProvider, PhoneCallbackController, _HERO_SMS_VERIFY_LOCK


class BindPhoneCallbackController(PhoneCallbackController):
    def __init__(self, provider_key: str, config: dict[str, Any], *, service: str, country: str = "", log_fn: Callable[[str], None] | None = None):
        super().__init__(provider_key, config, service=service, country=country, log_fn=log_fn)
        self.bind_status = ""
        self.bind_failure_reason = ""
        self.bind_failure_detail = ""
        self.bind_lifecycle_reported = False
        self.phone_submitted = False
        self.code_obtained = False

    def _reset_bind_state(self) -> None:
        self.bind_status = ""
        self.bind_failure_reason = ""
        self.bind_failure_detail = ""
        self.bind_lifecycle_reported = False
        self.phone_submitted = False
        self.code_obtained = False

    def _record_bind_failure(self, outcome: str, failure_code: str, reason: str, *, lifecycle_reported: bool) -> None:
        self.bind_status = str(outcome or "").strip()
        self.bind_failure_reason = str(failure_code or "").strip()
        self.bind_failure_detail = str(reason or "").strip()
        self.bind_lifecycle_reported = self.bind_lifecycle_reported or lifecycle_reported
        self.awaiting_external_success = False

    def __call__(self) -> str:
        if self.phase == "need_number":
            self._reset_bind_state()
        expecting_code = self.phase == "need_code" and self.activation is not None
        try:
            value = super().__call__()
        except Exception as exc:
            if expecting_code:
                outcome, failure_code = classify_bind_phone_failure(str(exc), phase="code")
                self._record_bind_failure(outcome, failure_code, str(exc), lifecycle_reported=False)
            raise
        if expecting_code and value:
            self.code_obtained = True
        return value

    def mark_send_failed(self, reason: str = "") -> None:
        super().mark_send_failed(reason)
        outcome, failure_code = classify_bind_phone_failure(reason, phase="send")
        lifecycle_reported = bool(getattr(self.provider, "current_lifecycle_reported", False))
        self._record_bind_failure(outcome, failure_code, reason, lifecycle_reported=lifecycle_reported)

    def mark_send_succeeded(self) -> None:
        self.phone_submitted = True
        super().mark_send_succeeded()

    def mark_code_failed(self, reason: str = "") -> None:
        self.code_obtained = True
        super().mark_code_failed(reason)
        outcome, failure_code = classify_bind_phone_failure(reason, phase="otp")
        self._record_bind_failure(outcome, failure_code, reason, lifecycle_reported=False)

    def report_success(self) -> None:
        self.phone_submitted = True
        self.code_obtained = True
        super().report_success()
        if self.completed:
            self.bind_status = BIND_PHONE_OUTCOME_SUCCESS
            self.bind_failure_reason = ""
            self.bind_failure_detail = ""
            self.bind_lifecycle_reported = True

    def cleanup(self) -> None:
        if self.activation and not self.completed:
            try:
                provider = self._provider()
                activation_id = self.activation.activation_id
                outcome = self.bind_status
                failure_code = self.bind_failure_reason
                reason = self.bind_failure_detail
                if not outcome:
                    if self.awaiting_external_success or self.code_obtained:
                        outcome, failure_code = classify_bind_phone_failure(reason or BIND_PHONE_FAILURE_OTP_SUBMIT_TRANSPORT, phase="otp")
                        reason = reason or "otp submit finished without provider success callback"
                    elif self.phone_submitted:
                        outcome, failure_code = classify_bind_phone_failure(reason or BIND_PHONE_FAILURE_TRANSPORT, phase="send")
                        reason = reason or "phone send accepted but bind flow aborted before completion"
                    else:
                        outcome, failure_code = classify_bind_phone_failure(reason or BIND_PHONE_FAILURE_RELEASED, phase="cleanup")
                        reason = reason or "bind flow released before phone submit"
                if outcome == BIND_PHONE_OUTCOME_RELEASED:
                    provider.cancel(activation_id)
                    self.bind_lifecycle_reported = True
                    self.log(f"已释放绑定号码: activation_id={activation_id}")
                elif self.bind_lifecycle_reported:
                    pass
                else:
                    hook = getattr(provider, "mark_attempt_failed", None)
                    hook_defined = callable(hook) and getattr(type(provider), "mark_attempt_failed", None) is not BaseSmsProvider.mark_attempt_failed
                    if hook_defined:
                        hook(activation_id, outcome=outcome, failure_code=failure_code, reason=reason)
                        self.bind_lifecycle_reported = True
                        self.log(f"已回写绑定号码失败: activation_id={activation_id} outcome={outcome} reason={failure_code}")
                    else:
                        provider.cancel(activation_id)
                        self.bind_lifecycle_reported = True
                        self.log(f"绑定号码失败但 provider 无失败回写钩子，已释放: activation_id={activation_id}")
                self.bind_status = outcome
                self.bind_failure_reason = failure_code
                self.bind_failure_detail = reason
            except Exception:
                pass
        if self._verify_lock_acquired:
            _HERO_SMS_VERIFY_LOCK.release()
            self._verify_lock_acquired = False


def create_binding_phone_callback(config: dict[str, Any], *, log_fn: Callable[[str], None]) -> tuple[Any | None, Callable[[], None]]:
    """Build the add-phone callback used during resume/OAuth binding.

    The callback is intentionally optional: resume flows that do not need
    add-phone should not allocate a phone. When OpenAI redirects to add-phone,
    the caller passes this callback into the browser OAuth state machine.
    """
    provider_key = str(config.get("bind_sms_provider") or "").strip()
    if not provider_key:
        provider_key = str(config.get("sms_provider") or "").strip()
    if not provider_key:
        return None, lambda: None

    bind_config = dict(config)
    if provider_key == "bind_user_phone_url":
        bind_config["_resource_provider"] = "bind_user_phone_url"
        bind_config.pop("sms_phone_url", None)
        bind_config.pop("sms_phone_urls", None)
        bind_config.pop("sms_phone_url_file", None)
        bind_config.pop("bind_sms_phone_url", None)
        bind_config.pop("bind_sms_phone_urls", None)
        bind_config.pop("bind_sms_phone_url_file", None)
    key_map = {
        "bind_sms_phone_url": "sms_phone_url",
        "bind_sms_phone_urls": "sms_phone_urls",
        "bind_sms_phone_url_file": "sms_phone_url_file",
        "bind_sms_country": "sms_country",
        "bind_sms_service": "sms_service",
        "bind_country_code": "country_code",
        "bind_country_name": "country_name",
    }
    for source, target in key_map.items():
        if provider_key == "bind_user_phone_url" and source.startswith("bind_sms_phone_url"):
            continue
        value = config.get(source)
        if str(value or "").strip():
            bind_config[target] = value

    service = str(config.get("bind_sms_service") or bind_config.get("sms_service") or "dr").strip()
    country = str(config.get("bind_sms_country") or bind_config.get("sms_country") or "").strip()
    controller = BindPhoneCallbackController(
        provider_key,
        bind_config,
        service=service,
        country=country,
        log_fn=log_fn,
    )
    return controller, controller.cleanup
