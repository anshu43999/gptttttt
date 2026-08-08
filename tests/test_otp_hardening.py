from __future__ import annotations

from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from core.base_sms import UserProvidedSmsProvider, extract_verification_code
from core import account_store
from full_pipeline import RegisterPipeline


def test_sms_extractor_prefers_openai_context() -> None:
    text = '{"code":0,"phone":"+13522560344","msg":"Your OpenAI verification code is 847219. Do not share it."}'
    assert UserProvidedSmsProvider._extract_code(text) == "847219"


def test_sms_extractor_ignores_dates_and_phone() -> None:
    text = "2026-06-17 +13522560344 OpenAI 验证码：593104，有效期 10 分钟"
    assert extract_verification_code(text, ignored_numbers={"13522560344"}, expected_lengths=(6,)) == "593104"


def test_email_extractor_multilingual() -> None:
    pipeline = RegisterPipeline({})
    assert pipeline._extract_openai_code_from_text("Código de verificação OpenAI: 618204") == "618204"
    assert pipeline._extract_openai_code_from_text("ChatGPT 認証コード 904177 を入力してください") == "904177"


def test_failed_run_upserts_password_account(tmp_path: Path) -> None:
    old_root = account_store.ACCOUNTS_ROOT
    try:
        account_store.ACCOUNTS_ROOT = tmp_path / "accounts"
        pipeline = RegisterPipeline({"output_dir": str(tmp_path / "output")})
        pipeline.result["status"] = "sms_code_pending"
        pipeline.result["failed_step"] = "sms_code"
        pipeline.result["failure_reason"] = "sms timeout after password submitted"
        pipeline.result["phone_number"] = "+15550000000"
        pipeline.result["activation_id"] = "act-1"
        pipeline.result["password"] = "GeneratedPass123!"
        pipeline.result["generated_chatgpt_password"] = "GeneratedPass123!"
        failed = pipeline._save_failed_run_json()
        assert failed.exists()
        saved = account_store.get_account("+15550000000")
        assert saved["stage"] == "failed"
        assert saved["password"] == "GeneratedPass123!"
    finally:
        account_store.ACCOUNTS_ROOT = old_root


def test_identityless_failed_run_is_not_account(tmp_path: Path) -> None:
    old_root = account_store.ACCOUNTS_ROOT
    old_output = account_store.OUTPUT_ROOT
    try:
        account_store.ACCOUNTS_ROOT = tmp_path / "accounts"
        account_store.OUTPUT_ROOT = tmp_path / "output"
        saved = account_store.upsert_account({"stage": "failed", "status": "error", "created_at": "2026-06-20T00:36:50"})
        assert saved == {}
        assert account_store.import_legacy_outputs(copy_artifacts=False) == 0
    finally:
        account_store.ACCOUNTS_ROOT = old_root
        account_store.OUTPUT_ROOT = old_output


if __name__ == "__main__":
    test_sms_extractor_prefers_openai_context()
    test_sms_extractor_ignores_dates_and_phone()
    test_email_extractor_multilingual()
    test_failed_run_upserts_password_account(Path("tmp/test_otp_hardening"))
    print("otp hardening tests passed")
