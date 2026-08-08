from __future__ import annotations

import json
import os
from pathlib import Path
import pytest


def _patch_mailat_runtime(monkeypatch, runner, root: Path) -> None:
    from services.mailat_protocol_runtime import MailatProtocolRuntime

    monkeypatch.setattr(
        runner,
        "validate_mailat_protocol_runtime",
        lambda: MailatProtocolRuntime(
            root=root,
            package_json=root / "package.json",
            sdk=root / "sdk.js",
            entry=root / "src" / "index.ts",
            tsx=(
                root
                / "node_modules"
                / ".bin"
                / ("tsx.cmd" if os.name == "nt" else "tsx")
            ),
        ),
    )


def test_mailat_runtime_is_fixed_project_local() -> None:
    from services.mailat_protocol_runtime import (
        PROJECT_ROOT,
        validate_mailat_protocol_runtime,
    )

    runtime = validate_mailat_protocol_runtime()

    assert runtime.root == PROJECT_ROOT / "vendor" / "mailat-codex-register"
    assert runtime.entry == runtime.root / "src" / "index.ts"
    assert runtime.sdk == runtime.root / "sdk.js"


def test_cpa_binding_configures_smsbower_and_clears_stale_phone_url() -> None:
    from services.mailat_protocol_bind_runner import _apply_binding_config

    mailat_config: dict[str, object] = {"gptRegisterBindSmsPhoneUrl": "https://stale.invalid/phone"}

    _apply_binding_config(mailat_config, {
        "bind_sms_provider": "smsbower_api",
        "bind_sms_api_key": "smsbower-test-key",
        "bind_sms_country": "BR",
        "bind_sms_service": "dr",
        "smsbower_min_price": 0.054,
        "smsbower_max_price": 0.054,
        "smsbower_provider_ids": "3160",
    })

    assert mailat_config == {
        "gptRegisterSmsProvider": "smsbower_api",
        "gptRegisterSmsService": "dr",
        "smsbowerApiKey": "smsbower-test-key",
        "smsbowerService": "dr",
        "smsbowerCountry": 73,
        "smsbowerMinPrice": 0.054,
        "smsbowerMaxPrice": 0.054,
        "smsbowerProviderIds": "3160",
        "gptRegisterBindSmsPhoneUrl": "",
    }


def test_cpa_binding_requires_smsbower_api_key() -> None:
    from services.mailat_protocol_bind_runner import _apply_binding_config

    with pytest.raises(RuntimeError, match="smsbower_api_key"):
        _apply_binding_config({}, {
            "bind_sms_provider": "smsbower_api",
            "bind_sms_country": "BR",
        })


def test_mailat_runner_invokes_source_with_project_proxy_and_otp(monkeypatch, tmp_path: Path) -> None:
    from services import mailat_email_protocol_runner as runner

    mailat_dir = tmp_path / "codex_register"
    bin_dir = mailat_dir / "node_modules" / ".bin"
    src_dir = mailat_dir / "src"
    bin_dir.mkdir(parents=True)
    src_dir.mkdir(parents=True)
    (bin_dir / ("tsx.cmd" if os.name == "nt" else "tsx")).write_text("", encoding="utf-8")
    (src_dir / "index.ts").write_text("// mailat entry\n", encoding="utf-8")
    (mailat_dir / "sdk.js").write_text("// sentinel sdk\n", encoding="utf-8")
    _patch_mailat_runtime(monkeypatch, runner, mailat_dir)

    captured: dict[str, object] = {}

    def fake_select(proxy_url, config, log, *, task_id=""):
        captured["selected_proxy"] = proxy_url
        captured["precheck_expected_country"] = config.get("lajiao_proxy_expected_country")
        captured["task_id"] = task_id
        return "socks5://selected:pass@proxy.local:1080", "http://127.0.0.1:18080", "203.0.113.9", None

    class Stdin:
        def __init__(self) -> None:
            self.values: list[str] = []

        def write(self, value: str) -> None:
            self.values.append(value)

        def flush(self) -> None:
            pass

    class FakeProcess:
        def __init__(self, command, *, cwd, stdin, stdout, stderr, text, encoding, errors, bufsize, env):
            captured["command"] = command
            captured["cwd"] = cwd
            captured["env"] = env
            self.stdin = Stdin()
            self.stdout = iter([
                "manualEmailOtp: targetEmail=mail@example.com\n",
                "[✅️注册成功] 邮箱：mail@example.com 密码：pw\n",
                "[access_token_file] auth/at/mail.json\n",
                "[access_token] token-value\n",
            ])
            self.pid = 12345

        def poll(self):
            return 0

        def wait(self):
            captured["stdin_values"] = list(self.stdin.values)
            return 0

    monkeypatch.setattr(runner, "_select_proxy_url", fake_select)
    monkeypatch.setattr(runner.subprocess, "Popen", FakeProcess)

    result = runner.run_mailat_email_protocol(
        {
            "lajiao_proxy_credentials": "user:pass@proxy.local:1080",
            "lajiao_proxy_credential_protocol": "socks5",
            "lajiao_proxy_expected_country": "JP",
        },
        email="mail@example.com",
        password="pw",
        otp_callback=lambda: "123456",
        task_id="task-mailat-test",
        log=lambda _message: None,
    )

    assert captured["selected_proxy"] == "socks5://user:pass@proxy.local:1080"
    assert captured["precheck_expected_country"] == "JP"
    assert captured["task_id"] == "task-mailat-test"
    assert captured["stdin_values"] == ["123456\n"]
    assert "--skip-phone" in captured["command"]
    assert captured["env"]["CODEX_AT_OUT_DIR"] == str(Path(result["protocol_work_dir"]))
    config = json.loads((Path(result["protocol_work_dir"]) / "config.json").read_text(encoding="utf-8"))
    assert config["defaultProxyUrl"] == "http://127.0.0.1:18080"
    assert config["defaultPassword"] == "pw"
    assert config["gptRegisterExternalEmail"] == "mail@example.com"
    assert (Path(result["protocol_work_dir"]) / "sdk.js").read_text(encoding="utf-8") == "// sentinel sdk\n"
    assert result["access_token"] == "token-value"
    assert result["registration_proxy"] == "socks5://selected:pass@proxy.local:1080"
    assert result["registration_proxy_exit_ip"] == "203.0.113.9"
    assert config["heroSMSApiKey"] == ""


def test_mailat_runner_only_passes_hero_key_for_herosms_provider(monkeypatch, tmp_path: Path) -> None:
    from services import mailat_email_protocol_runner as runner

    mailat_dir = tmp_path / "codex_register"
    bin_dir = mailat_dir / "node_modules" / ".bin"
    src_dir = mailat_dir / "src"
    bin_dir.mkdir(parents=True)
    src_dir.mkdir(parents=True)
    (bin_dir / ("tsx.cmd" if os.name == "nt" else "tsx")).write_text("", encoding="utf-8")
    (src_dir / "index.ts").write_text("// mailat entry\n", encoding="utf-8")
    (mailat_dir / "sdk.js").write_text("// sentinel sdk\n", encoding="utf-8")
    _patch_mailat_runtime(monkeypatch, runner, mailat_dir)

    class Stdin:
        def write(self, _value: str) -> None:
            pass

        def flush(self) -> None:
            pass

    class FakeProcess:
        def __init__(self, *args, **kwargs):
            self.stdin = Stdin()
            self.stdout = iter([
                "[✅️注册成功] 邮箱：mail@example.com 密码：pw\n",
                "[access_token] token-value\n",
            ])
            self.pid = 12345

        def poll(self):
            return 0

        def wait(self):
            return 0

    monkeypatch.setattr(runner, "_select_proxy_url", lambda proxy_url, config, log, *, task_id="": (proxy_url, proxy_url, "skip_check", None))
    monkeypatch.setattr(runner.subprocess, "Popen", FakeProcess)

    result = runner.run_mailat_email_protocol(
        {
            "sms_provider": "herosms_api",
            "sms_api_key": "secret-key",
            "mailat_protocol_skip_phone": False,
        },
        email="mail@example.com",
        password="pw",
        otp_callback=lambda: "123456",
        task_id="task-mailat-herosms",
        log=lambda _message: None,
    )

    config = json.loads((Path(result["protocol_work_dir"]) / "config.json").read_text(encoding="utf-8"))
    assert config["heroSMSApiKey"] == "secret-key"

def test_mailat_proxy_country_check_cools_mismatch_and_leases_next(monkeypatch, tmp_path: Path) -> None:
    from services import mailat_email_protocol_runner as runner
    from application.resource_pool_service import ResourcePoolService
    from infrastructure.repositories.resource_pool_repository import ResourcePoolRepository
    from infrastructure import db_backend

    monkeypatch.setattr(db_backend, "_RESOLVED_BACKEND", "sqlite")

    db_path = tmp_path / "proxy-country.db"
    pool = ResourcePoolService(ResourcePoolRepository(db_path))
    pool.import_lajiao_credentials("bad:pass@proxy.local:1000\ngood:pass@proxy.local:1001", protocol="socks5")
    first = pool.repo.lease("proxy", "lajiao_credentials", "task-country")
    config = {
        "_resource_pool_db_path": str(db_path),
        "resource_leases": [{"type": "proxy", "provider": "lajiao_credentials", "key": first.resource_key}],
        "lajiao_proxy_expected_country": "JP",
        "lajiao_proxy_credential_protocol": "socks5",
        "mailat_protocol_use_local_bridge": False,
        "mailat_protocol_proxy_attempts": 2,
    }
    monkeypatch.setattr(runner, "ResourcePoolRepository", lambda _path=None: pool.repo)

    def fake_country(proxy_url, timeout_seconds, log):
        if "1000" in proxy_url:
            return "US", "198.51.100.1"
        return "JP", "203.0.113.10"

    monkeypatch.setattr(runner, "_proxy_exit_country", fake_country)

    selected, runtime, exit_ip, proxy_runtime = runner._select_proxy_url("socks5://bad:pass@proxy.local:1000", config, lambda _message: None, task_id="task-country")

    assert selected == "socks5://good:pass@proxy.local:1001"
    assert runtime == selected
    assert exit_ip == "203.0.113.10"
    assert proxy_runtime is not None
    proxy_runtime.cleanup()
    assert pool.repo.get("proxy", "lajiao_credentials", "bad:pass@proxy.local:1000")["status"] == "cooldown"
    assert pool.repo.get("proxy", "lajiao_credentials", "good:pass@proxy.local:1001")["status"] == "leased"
    assert any(item["key"] == "good:pass@proxy.local:1001" for item in config["resource_leases"])

def test_mailat_proxy_country_check_accepts_candidate_custom_zone_in_automatic_mode(monkeypatch) -> None:
    from services import mailat_email_protocol_runner as runner

    candidate = "http://customer-custom_zone_TR:secret@proxy.local:7878"
    monkeypatch.setattr(runner, "_proxy_exit_country", lambda _proxy_url, _timeout_seconds, _log: ("TR", "203.0.113.77"))
    logs: list[str] = []

    selected, runtime_url, exit_ip, proxy_runtime = runner._select_proxy_url(
        candidate,
        {
            "mailat_protocol_proxy_precheck_enabled": True,
            "mailat_protocol_use_local_bridge": False,
        },
        logs.append,
    )

    assert (selected, runtime_url, exit_ip) == (candidate, candidate, "203.0.113.77")
    assert proxy_runtime is not None
    assert any("expected=TR ok=True" in message for message in logs)
    proxy_runtime.cleanup()



def test_protocol_storage_session_converts_mailat_cookie_jar(tmp_path: Path) -> None:
    from services.mailat_email_protocol_task import _ProtocolStorageSession

    source = tmp_path / "mailat-session.json"
    target = tmp_path / "storage.json"
    source.write_text(json.dumps({
        "cookieJar": {
            "cookies": [{
                "key": "__Secure-next-auth.session-token",
                "value": "cookie-value",
                "domain": "chatgpt.com",
                "path": "/",
                "expires": "2030-01-01T00:00:00.000Z",
                "httpOnly": True,
                "secure": True,
                "sameSite": "lax",
            }]
        }
    }), encoding="utf-8")

    saved = _ProtocolStorageSession(str(source)).save_storage_state(str(target))
    payload = json.loads(target.read_text(encoding="utf-8"))

    assert saved == str(target)
    assert payload["origins"] == []
    assert payload["cookies"][0]["name"] == "__Secure-next-auth.session-token"
    assert payload["cookies"][0]["domain"] == "chatgpt.com"
    assert payload["cookies"][0]["sameSite"] == "Lax"


def test_cpa_bind_stages_sdk_in_task_directory_before_process_launch(monkeypatch, tmp_path: Path) -> None:
    from services import mailat_protocol_bind_runner as runner

    mailat_dir = tmp_path / "mailat-source"
    bin_dir = mailat_dir / "node_modules" / ".bin"
    src_dir = mailat_dir / "src"
    bin_dir.mkdir(parents=True)
    src_dir.mkdir(parents=True)
    (bin_dir / ("tsx.cmd" if os.name == "nt" else "tsx")).write_text("", encoding="utf-8")
    (src_dir / "index.ts").write_text("// bind entry\n", encoding="utf-8")
    sdk_content = "// sentinel sdk\nexport const bindSentinel = true;\n"
    (mailat_dir / "sdk.js").write_text(sdk_content, encoding="utf-8")
    _patch_mailat_runtime(monkeypatch, runner, mailat_dir)

    captured: dict[str, str] = {}

    class Stdin:
        def write(self, _value: str) -> None:
            pass

        def flush(self) -> None:
            pass

    class FakeProcess:
        def __init__(self, _command, *, cwd, **_kwargs) -> None:
            captured["sdk_content_at_launch"] = (Path(cwd) / "sdk.js").read_text(encoding="utf-8")
            self.stdin = Stdin()

            self.stdout = iter(())
            self.pid = 1

        def wait(self) -> int:
            return 0

    monkeypatch.setattr(runner, "TASK_TMP_ROOT", tmp_path / "bind-tasks")
    monkeypatch.setattr(runner.subprocess, "Popen", FakeProcess)

    runner.run_mailat_protocol_cpa_bind(
        {
            "cpa_base_url": "https://cpa.invalid",
            "cpa_management_key": "test-management-key",
        },
        email="bind-test@example.invalid",
        password="test-password",
        task_id="sdk-staging",
        log=lambda _message: None,
        otp_callback=lambda: "test-otp",
    )

    assert captured["sdk_content_at_launch"] == sdk_content


def test_cpa_bind_forwards_one_email_otp_after_recognized_prompt(monkeypatch, tmp_path: Path) -> None:
    from services import mailat_protocol_bind_runner as runner

    mailat_dir = tmp_path / "mailat-source"
    bin_dir = mailat_dir / "node_modules" / ".bin"
    src_dir = mailat_dir / "src"
    bin_dir.mkdir(parents=True)
    src_dir.mkdir(parents=True)
    (bin_dir / ("tsx.cmd" if os.name == "nt" else "tsx")).write_text("", encoding="utf-8")
    (src_dir / "index.ts").write_text("// bind entry\n", encoding="utf-8")
    (mailat_dir / "sdk.js").write_text("// sentinel sdk\n", encoding="utf-8")
    _patch_mailat_runtime(monkeypatch, runner, mailat_dir)

    callback_calls: list[None] = []
    captured: dict[str, object] = {}

    class Stdin:
        def __init__(self) -> None:
            self.values: list[str] = []

        def write(self, value: str) -> None:
            self.values.append(value)

        def flush(self) -> None:
            pass

    class FakeProcess:
        def __init__(self, command, **_kwargs) -> None:
            captured["command"] = command
            self.stdin = Stdin()
            self.stdout = iter(["manualEmailOtp: targetEmail=bind-test@example.invalid\n"])
            self.pid = 1

        def wait(self) -> int:
            captured["stdin_values"] = list(self.stdin.values)
            return 0

    def fake_otp_callback() -> str:
        callback_calls.append(None)
        return "654321"

    monkeypatch.setattr(runner, "TASK_TMP_ROOT", tmp_path / "bind-tasks")
    monkeypatch.setattr(runner.subprocess, "Popen", FakeProcess)

    runner.run_mailat_protocol_cpa_bind(
        {
            "cpa_base_url": "https://cpa.invalid",
            "cpa_management_key": "test-management-key",
        },
        email="bind-test@example.invalid",
        password="test-password",
        otp_callback=fake_otp_callback,
        task_id="otp-bridge",
        log=lambda _message: None,
    )

    assert "--otp" in captured["command"]
    assert "--cpa-bind-only" in captured["command"]
    assert callback_calls == [None]
    assert captured["stdin_values"] == ["654321\n"]


def test_local_protocol_bind_runs_auth_and_loads_tokens(monkeypatch, tmp_path: Path) -> None:
    from services import mailat_protocol_bind_runner as runner

    mailat_dir = tmp_path / "mailat-source"
    bin_dir = mailat_dir / "node_modules" / ".bin"
    src_dir = mailat_dir / "src"
    bin_dir.mkdir(parents=True)
    src_dir.mkdir(parents=True)
    (bin_dir / ("tsx.cmd" if os.name == "nt" else "tsx")).write_text("", encoding="utf-8")
    (src_dir / "index.ts").write_text("// bind entry\n", encoding="utf-8")
    (mailat_dir / "sdk.js").write_text("// sentinel sdk\n", encoding="utf-8")
    _patch_mailat_runtime(monkeypatch, runner, mailat_dir)

    captured: dict[str, object] = {}

    class Stdin:
        def __init__(self) -> None:
            self.values: list[str] = []

        def write(self, value: str) -> None:
            self.values.append(value)

        def flush(self) -> None:
            pass

    class FakeProcess:
        def __init__(self, command, *, cwd, **_kwargs) -> None:
            captured["command"] = command
            self.stdin = Stdin()
            self.stdout = iter(["manualEmailOtp: targetEmail=bind-test@example.invalid\n"])
            self.pid = 1
            auth_dir = Path(cwd) / "auth"
            auth_dir.mkdir()
            (auth_dir / "bind-test.json").write_text(json.dumps({
                "access_token": "local-access",
                "refresh_token": "local-refresh",
                "id_token": "local-id",
                "account_id": "auth0|local-account",
                "email": "bind-test@example.invalid",
                "expired": "2026-07-17T00:00:00Z",
            }), encoding="utf-8")

        def wait(self) -> int:
            captured["stdin_values"] = list(self.stdin.values)
            return 0

    monkeypatch.setattr(runner, "TASK_TMP_ROOT", tmp_path / "bind-tasks")
    monkeypatch.setattr(runner.subprocess, "Popen", FakeProcess)

    result = runner.run_mailat_protocol_cpa_bind(
        {
            "oauth_callback_mode": "local",
        },
        email="bind-test@example.invalid",
        password="test-password",
        otp_callback=lambda: "654321",
        task_id="local-auth",
        log=lambda _message: None,
    )

    command = captured["command"]
    assert "--auth" in command
    assert "--codex-cpa" not in command
    assert "--cpa-base" not in command
    assert captured["stdin_values"] == ["654321\n"]
    assert result["oauth_callback_mode"] == "local"
    assert result["oauth_result"]["access_token"] == "local-access"
    assert result["oauth_result"]["refresh_token"] == "local-refresh"
    assert result["oauth_result"]["id_token"] == "local-id"


def test_local_protocol_bind_persists_tokens_without_losing_credentials(monkeypatch) -> None:
    from services import mailat_protocol_bind_task as task

    stored: dict[str, object] = {}
    events: list[tuple[str, str, dict[str, object]]] = []

    monkeypatch.setattr(task.db, "upsert_account", lambda account: stored.update(account) or 1)
    monkeypatch.setattr(task.db, "get_account", lambda _key: dict(stored))
    monkeypatch.setattr(
        task.db,
        "add_account_event",
        lambda account_key, event_type, **kwargs: events.append((account_key, event_type, kwargs)),
    )

    updated = task._persist_local_oauth_result(
        {
            "account_key": "bind-test@example.invalid",
            "email": "bind-test@example.invalid",
            "password": "account-password",
            "outlook_refresh_token": "mailbox-refresh",
            "tokens": {
                "access_token": "old-access",
                "chatgpt_access_token_initial": "registration-access",
            },
        },
        {
            "oauth_auth_file": "auth/bind-test.json",
            "oauth_result": {
                "access_token": "local-access",
                "refresh_token": "local-refresh",
                "id_token": "local-id",
                "account_id": "auth0|local-account",
                "expired": "2026-07-17T00:00:00Z",
            },
        },
        task_id="task-local-bind",
    )

    assert updated["password"] == "account-password"
    assert updated["outlook_refresh_token"] == "mailbox-refresh"
    assert updated["raw_tokens"] == {
        "access_token": "local-access",
        "refresh_token": "local-refresh",
        "id_token": "local-id",
        "chatgpt_access_token_initial": "registration-access",
        "token_expires_at": "2026-07-17T00:00:00Z",
    }
    assert updated["account_id"] == "auth0|local-account"
    assert updated["oauth_callback_mode"] == "local"
    assert updated["binding_status"] == "bound"
    assert updated["cpa_submit_status"] == ""
    assert events[0][0:2] == ("bind-test@example.invalid", "protocol_local_tokens_persisted")
    assert events[0][2]["status"] == "bound"


def test_cpa_bind_reuses_used_outlook_token_for_existing_account(monkeypatch, tmp_path: Path) -> None:
    from services import mailat_protocol_bind_runner as runner

    email = "used-bind@example.com"
    state_file = tmp_path / "outlook-pool.jsonl"
    state_file.write_text(
        json.dumps({"email": email, "status": "registered", "updated_at": "2026-01-01T00:00:00"}) + "\n",
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def fake_wait_for_openai_code(self, account, *, timeout: int = 180) -> str:
        captured["account"] = account
        captured["timeout"] = timeout
        return "654321"

    monkeypatch.setattr(runner.OutlookTokenMailbox, "wait_for_openai_code", fake_wait_for_openai_code)

    callback = runner._existing_email_otp_callback(
        {
            "mailbox_provider": "outlook_token",
            "outlook_email": email,
            "outlook_password": "mail-password",
            "outlook_client_id": "client-id",
            "outlook_refresh_token": "refresh-token",
            "outlook_pool_state_file": str(state_file),
            "email_otp_timeout": 45,
        },
        email=email,
        log=lambda _message: None,
    )

    assert callback() == "654321"
    assert captured["account"].email == email
    assert captured["timeout"] == 45


def test_cpa_bind_restores_registration_icloud_order_text_for_target_account(monkeypatch, tmp_path: Path) -> None:
    from services import mailat_protocol_bind_task as task

    prior_task_id = "prior-registration"
    source_config_path = tmp_path / "data" / "tasks" / f"{prior_task_id}_config.yaml"
    source_config_path.parent.mkdir(parents=True)
    source_config_path.write_text("placeholder", encoding="utf-8")
    historical_order_text = "bind-test@icloud.invalid----test-order-token"
    captured: dict[str, object] = {}

    def fake_load_config(path: str) -> dict[str, object]:
        if path == "current-config.yaml":
            return {
                "mailbox_provider": "icloud_privacy",
                "icloud_privacy_order_file": "stale-order-file.txt",
            }
        assert Path(path) == source_config_path
        return {
            "mailbox_provider": "icloud_privacy",
            "icloud_privacy_order_text": historical_order_text,
        }

    def fake_run_bind(config, *, email, password, task_id, log):
        captured["config"] = config
        captured["email"] = email
        captured["task_id"] = task_id
        return {"protocol_work_dir": str(tmp_path / "bind-work")}

    monkeypatch.setattr(task, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(task, "load_config", fake_load_config)
    monkeypatch.setattr(
        task.account_store,
        "get_account",
        lambda _account_key: {
            "email": "bind-test@icloud.invalid",
            "password": "test-password",
            "registration_task_id": prior_task_id,
        },
    )
    monkeypatch.setattr(task, "run_mailat_protocol_cpa_bind", fake_run_bind)

    task.run("current-config.yaml", account_key="target-account", task_id="bind-task")

    assert captured["config"]["icloud_privacy_order_text"] == historical_order_text
    assert captured["email"] == "bind-test@icloud.invalid"
    assert captured["task_id"] == "bind-task"


def test_normalize_email_protocol_backend_aliases() -> None:
    from services.go_email_protocol_runner import normalize_email_protocol_backend

    assert normalize_email_protocol_backend("python") == "python"
    assert normalize_email_protocol_backend("mailat") == "python"
    assert normalize_email_protocol_backend("node") == "python"
    assert normalize_email_protocol_backend("go") == "go"
    assert normalize_email_protocol_backend("golang") == "go"
    assert normalize_email_protocol_backend(None) == "python"


def test_go_runner_polls_otp_and_completes(monkeypatch, tmp_path: Path) -> None:
    from services import go_email_protocol_runner as runner

    monkeypatch.setattr(runner, "TASK_TMP_ROOT", tmp_path / "tasks")
    monkeypatch.setattr(runner, "check_go_email_protocol_health", lambda config, timeout=5.0: {"ok": True, "service": "email-protocol-worker"})
    def bridge_used(*_args, **_kwargs):
        raise AssertionError("direct Go worker must not create an HTTP proxy bridge")

    monkeypatch.setattr(runner, "_select_proxy_url", bridge_used)
    monkeypatch.setattr(runner, "_proxy_url", lambda config: "socks5://user:pass@proxy.local:1080")

    calls: list[tuple[str, str]] = []
    states = {
        "n": 0,
    }

    def fake_http(method: str, url: str, payload=None, *, headers=None, timeout=30.0):
        calls.append((method, url))
        if method == "POST" and url.endswith("/v2/email-register"):
            assert payload["resource_grant"]["bridge"] == {
                "id": "direct-socks", "url": "socks5://user:pass@proxy.local:1080", "generation": 1, "capability": "direct"
            }
            return {"job_id": "jr_1", "job_capability": "cap_1", "status": "running"}
        if method == "POST" and url.endswith("/v2/email-register/jr_1/otp"):
            assert payload == {"challenge_id": "ch_1", "state_version": 2, "code": "654321"}
            assert headers == {"X-Job-Capability": "cap_1", "Authorization": "Bearer cap_1"}
            return {"job_id": "jr_1", "status": "running"}
        if method == "GET" and "/v2/email-register/jr_1" in url:
            states["n"] += 1
            if states["n"] == 1:
                return {
                    "job_id": "jr_1",
                    "status": "waiting_for_otp",
                    "state_version": 2,
                    "challenge": {"challenge_id": "ch_1", "state_version": 2},
                }
            return {
                "job_id": "jr_1",
                "status": "succeeded",
                "session": {
                    "email": "test@example.com",
                    "account_id": "acc_1",
                    "access_token": (
                        "eyJhbGciOiJub25lIn0."
                        "eyJodHRwczovL2FwaS5vcGVuYWkuY29tL2F1dGgiOnsiY2hhdGdwdF9hY2NvdW50X2lkIjoiYWNjXzEiLCJjaGF0Z3B0X3BsYW5fdHlwZSI6ImZyZWUifSwiaHR0cHM6Ly9hcGkub3BlbmFpLmNvbS9wcm9maWxlIjp7ImVtYWlsIjoidGVzdEBleGFtcGxlLmNvbSJ9fQ."
                    ),
                    "cookies": [{"name": "oai-did", "value": "did", "domain": ".openai.com", "path": "/"}],
                },
            }
        raise AssertionError(f"unexpected call {method} {url}")

    monkeypatch.setattr(runner, "_http_json", fake_http)

    result = runner.run_go_email_protocol(
        {
            "go_email_protocol_url": "http://127.0.0.1:18765",
            "go_email_protocol_transport": "direct",
            "go_email_protocol_poll_interval_ms": 1,
            "mailat_protocol_timeout_seconds": 120,
        },
        email="test@example.com",
        password="pw",
        otp_callback=lambda: "654321",
        task_id="task_go_test",
        log=lambda _msg: None,
    )

    assert result["protocol_backend"] == "go"
    assert result["protocol_runner"] == "go/email-protocol-worker"
    assert result["account_id"] == "acc_1"
    assert result["access_token"]
    assert Path(result["protocol_session_state_path"]).is_file()
    assert any(method == "POST" and url.endswith("/v2/email-register/jr_1/otp") for method, url in calls)



def test_go_runner_accepts_camel_access_token(monkeypatch, tmp_path: Path) -> None:
    from services import go_email_protocol_runner as runner

    monkeypatch.setattr(runner, "TASK_TMP_ROOT", tmp_path / "tasks")
    monkeypatch.setattr(runner, "check_go_email_protocol_health", lambda config, timeout=5.0: {"ok": True})
    monkeypatch.setattr(runner, "_proxy_url", lambda config: "socks5://user:pass@proxy.local:1080")

    token = (
        "eyJhbGciOiJub25lIn0."
        "eyJodHRwczovL2FwaS5vcGVuYWkuY29tL2F1dGgiOnsiY2hhdGdwdF9hY2NvdW50X2lkIjoiYWNjXzEiLCJjaGF0Z3B0X3BsYW5fdHlwZSI6ImZyZWUifSwiaHR0cHM6Ly9hcGkub3BlbmFpLmNvbS9wcm9maWxlIjp7ImVtYWlsIjoidGVzdEBleGFtcGxlLmNvbSJ9fQ."
    )

    def fake_http(method: str, url: str, payload=None, *, headers=None, timeout=30.0):
        if method == "POST" and url.endswith("/v2/email-register"):
            return {
                "status": "succeeded",
                "session": {
                    "email": "test@example.com",
                    "account_id": "acc_1",
                    "accessToken": token,
                    "cookies": [],
                    "origins": [],
                },
            }
        raise AssertionError(f"unexpected call {method} {url}")

    monkeypatch.setattr(runner, "_http_json", fake_http)

    result = runner.run_go_email_protocol(
        {
            "go_email_protocol_url": "http://127.0.0.1:18765",
            "go_email_protocol_transport": "direct",
            "mailat_protocol_timeout_seconds": 120,
        },
        email="test@example.com",
        password="pw",
        otp_callback=lambda: "654321",
        task_id="task_go_camel",
        log=lambda _msg: None,
    )

    assert result["access_token"] == token
    session_doc = json.loads(Path(result["protocol_session_state_path"]).read_text(encoding="utf-8"))
    assert session_doc["access_token"] == token

def test_go_runner_fails_fast_when_daemon_down(monkeypatch, tmp_path: Path) -> None:
    from services import go_email_protocol_runner as runner

    monkeypatch.setattr(runner, "TASK_TMP_ROOT", tmp_path / "tasks")

    def boom(config, timeout=5.0):
        raise RuntimeError("无法连接 Go 邮箱协议 worker")

    monkeypatch.setattr(runner, "check_go_email_protocol_health", boom)

    try:
        runner.run_go_email_protocol(
            {"go_email_protocol_url": "http://127.0.0.1:9"},
            email="test@example.com",
            password="pw",
            otp_callback=lambda: "1",
            task_id="task_down",
            log=lambda _msg: None,
        )
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "Go" in str(exc)


def test_go_runner_surfaces_worker_terminal_message(monkeypatch, tmp_path: Path) -> None:
    from services import go_email_protocol_runner as runner

    monkeypatch.setattr(runner, "TASK_TMP_ROOT", tmp_path / "tasks")
    monkeypatch.setattr(runner, "check_go_email_protocol_health", lambda config, timeout=5.0: {"ok": True})
    monkeypatch.setattr(runner, "_select_proxy_url", lambda proxy_url, config, log, *, task_id="": (proxy_url, "http://127.0.0.1:17890", "203.0.113.10", None))
    monkeypatch.setattr(runner, "_proxy_url", lambda config: "socks5://user:pass@proxy.local:1080")

    def fake_http(method: str, url: str, payload=None, *, headers=None, timeout=30.0):
        if method == "POST" and url.endswith("/v2/email-register"):
            return {"job_id": "jr_1", "job_capability": "cap_1", "status": "running"}
        if method == "GET" and "/v2/email-register/jr_1" in url:
            return {
                "job_id": "jr_1",
                "status": "failed",
                "failure_code": "email_already_used",
                "message": "protocol: S6 continue_url already email-verification",
            }
        raise AssertionError(f"unexpected call {method} {url}")

    monkeypatch.setattr(runner, "_http_json", fake_http)

    with pytest.raises(RuntimeError, match="S6 continue_url already email-verification"):
        runner.run_go_email_protocol(
            {
                "go_email_protocol_url": "http://127.0.0.1:18765",
                "go_email_protocol_poll_interval_ms": 1,
                "mailat_protocol_timeout_seconds": 120,
            },
            email="test@example.com",
            password="pw",
            otp_callback=lambda: "654321",
            task_id="task_failure_detail",
            log=lambda _msg: None,
        )



def test_go_direct_socks_proxy_admission_key_uses_concrete_session() -> None:
    from services import go_email_protocol_runner as runner

    config = {"go_email_protocol_transport": "direct", "proxy": "shared-user:secret@proxy.local:10000"}
    first = runner._resource_grant(
        config,
        email="a@example.com",
        runtime_proxy_url="socks5://shared-user-session-111:secret@proxy.local:10000",
        exit_ip="203.0.113.1",
    )
    second = runner._resource_grant(
        config,
        email="b@example.com",
        runtime_proxy_url="socks5://shared-user-session-222:secret@proxy.local:10000",
        exit_ip="203.0.113.2",
    )

    assert first["proxy_key"] != second["proxy_key"]
    assert str(first["proxy_key"]).startswith("direct-socks:")
    assert "shared-user" not in str(first["proxy_key"])
    assert "secret" not in str(first["proxy_key"])
    assert first["bridge"]["url"] == "socks5://shared-user-session-111:secret@proxy.local:10000"