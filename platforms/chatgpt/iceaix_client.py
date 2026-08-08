"""
PPXY / iceaix.com API 客户端 — ChatGPT Plus 试用开通。

API 文档: E:/Download/Telegram Desktop/iceapi.md

模式:
  - API 模式: 使用 API Key，全自动
  - CDK 模式: 使用 CDK 在 Web 前台手动激活 (一期)

本文件实现 API 模式的完整客户端。CDK 模式由用户手动操作。
"""

from __future__ import annotations

import sys
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional
from urllib.parse import urljoin

import requests


def configure_utf8_stdio() -> None:
    """Keep PPXY Chinese responses readable under Windows subprocess stdout."""
    for stream in (getattr(sys, "stdout", None), getattr(sys, "stderr", None)):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


configure_utf8_stdio()



# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

BASE_URL = "https://plus.iceaix.com"
API_PREFIX = "/api/v1"

DEFAULT_POLL_INTERVAL = 3.0  # 轮询间隔(秒)
DEFAULT_JOB_TIMEOUT = 300    # 任务超时(秒)
DEFAULT_OTP_TIMEOUT = 30     # OTP 等待超时(秒)


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    OTP_PENDING = "otp_pending"
    SUCCESS = "success"
    FAILED = "failed"


class TrialStatus(str, Enum):
    ELIGIBLE = "eligible"
    NO_TRIAL = "no_trial"
    BLOCKED = "blocked"


class BillingStatus(str, Enum):
    CHARGED = "charged"
    RELEASED = "released"
    PENDING = "pending"


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class AccountInfo:
    """PPXY 账户信息。"""
    client_id: str = ""
    name: str = ""
    status: str = ""
    quota_total: int = 0
    quota_used: int = 0
    quota_reserved: int = 0
    quota_remaining: int = 0
    concurrency_limit: int = 0
    job_cost_units: int = 1

    @property
    def can_create_job(self) -> bool:
        return self.quota_remaining >= self.job_cost_units


@dataclass
class TrialCheckResult:
    """试用资格检测结果。"""
    ok: bool = False
    eligible: bool = False
    blocked: bool = False
    status: str = ""
    result_code: str = ""
    message: str = ""
    amount_cents: int = 0
    currency: str = "JPY"
    resource_mode: str = ""


@dataclass
class JobInfo:
    """任务信息。"""
    job_id: str = ""
    status: JobStatus = JobStatus.QUEUED
    product: str = ""
    result_code: str = ""
    error_message: str = ""
    client_ref: str = ""
    done: bool = False
    otp_pending: bool = False
    billing_status: str = ""
    cost_units: int = 0
    callback_status: str = ""
    result: dict = field(default_factory=dict)
    resource_mode: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.status in (JobStatus.SUCCESS, JobStatus.FAILED)

    @property
    def is_success(self) -> bool:
        return self.status == JobStatus.SUCCESS


@dataclass
class CreateJobRequest:
    """创建开通任务的请求。"""
    input_token: str                      # ChatGPT access token
    phone: str                            # PayPal 手机号
    sms_api: str = ""                     # 接码 API URL (自动模式)
    otp: str = ""                         # 已知 OTP (手动模式)
    client_ref: str = ""                  # 订单号
    callback_url: str = ""                # 回调 URL
    proxy: str = ""                       # 自定义 US 代理
    proxy_jp: str = ""                    # 自定义 JP 代理
    email: str = ""                       # 指定注册邮箱
    pplink_retry: int = 3
    otp_timeout: int = 30


# ---------------------------------------------------------------------------
# PPXY API 客户端
# ---------------------------------------------------------------------------

class IceAixClient:
    """PPXY / iceaix.com API 客户端。"""

    def __init__(
        self,
        api_key: str,
        base_url: str = BASE_URL,
        timeout: int = 30,
        verbose: bool = True,
    ):
        """
        Args:
            api_key: PPXY API Key (格式 api_xxx)
            base_url: API 基础 URL
            timeout: HTTP 请求超时
            verbose: 是否输出日志
        """
        if not api_key:
            raise ValueError("PPXY API Key 不能为空")
        if not api_key.startswith("api_"):
            raise ValueError(
                "PPXY API Key 应以 'api_' 开头。CDK 不能用于 API 模式，"
                "请在 iceaix 后台开通 API Key。"
            )

        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.verbose = verbose

        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )

    # ------------------------------------------------------------------
    # 1. 查询账户
    # ------------------------------------------------------------------

    def get_account(self) -> AccountInfo:
        """查询 PPXY 账户余额和配额。"""
        url = f"{self.base_url}{API_PREFIX}/account"
        self._log(f"GET {url}")
        resp = self.session.get(url, timeout=self.timeout)
        data = self._parse_response(resp)
        return AccountInfo(
            client_id=data.get("client_id", ""),
            name=data.get("name", ""),
            status=data.get("status", ""),
            quota_total=data.get("quota_total", 0),
            quota_used=data.get("quota_used", 0),
            quota_reserved=data.get("quota_reserved", 0),
            quota_remaining=data.get("quota_remaining", 0),
            concurrency_limit=data.get("concurrency_limit", 2),
            job_cost_units=data.get("job_cost_units", 1),
        )

    # ------------------------------------------------------------------
    # 2. 试用检测
    # ------------------------------------------------------------------

    def check_trial(
        self,
        token: str,
        proxy_jp: str = "",
    ) -> TrialCheckResult:
        """
        检测 ChatGPT token 是否有试用资格。

        Args:
            token: ChatGPT access token
            proxy_jp: 自定义 JP 代理 (可选)

        Returns:
            TrialCheckResult
        """
        url = f"{self.base_url}{API_PREFIX}/trial/check"
        body: dict[str, Any] = {"token": token}
        if proxy_jp:
            body["proxy_jp"] = proxy_jp

        self._log(f"POST {url} — 检测试用资格")
        resp = self.session.post(url, json=body, timeout=self.timeout)
        data = self._parse_response(resp)

        return TrialCheckResult(
            ok=data.get("ok", False),
            eligible=data.get("eligible", False),
            blocked=data.get("blocked", False),
            status=data.get("status", ""),
            result_code=data.get("result_code", ""),
            message=data.get("message", ""),
            amount_cents=data.get("amount_cents", 0),
            currency=data.get("currency", "JPY"),
            resource_mode=data.get("resource_mode", ""),
        )

    # ------------------------------------------------------------------
    # 3. 创建开通任务
    # ------------------------------------------------------------------

    def create_job(
        self,
        request: CreateJobRequest,
        idempotency_key: str = "",
    ) -> JobInfo:
        """
        创建 Plus 开通任务。

        取码方式:
          - 传 sms_api: 自动轮询接码
          - 不传 sms_api: 任务进入 otp_pending，手动提交
          - 传 otp: 直接使用该验证码

        Args:
            request: CreateJobRequest
            idempotency_key: 幂等 Key (建议传入)

        Returns:
            JobInfo with job_id
        """
        url = f"{self.base_url}{API_PREFIX}/jobs"
        body: dict[str, Any] = {
            "input": request.input_token,
            "phone": request.phone,
        }

        if request.sms_api:
            body["sms_api"] = request.sms_api
        if request.otp:
            body["otp"] = request.otp
        if request.client_ref:
            body["client_ref"] = request.client_ref
        if request.callback_url:
            body["callback_url"] = request.callback_url
        if request.proxy:
            body["proxy"] = request.proxy
        if request.proxy_jp:
            body["proxy_jp"] = request.proxy_jp
        if request.email:
            body["email"] = request.email
        if request.pplink_retry != 3:
            body["pplink_retry"] = request.pplink_retry
        if request.otp_timeout != 30:
            body["otp_timeout"] = request.otp_timeout

        headers = {}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        self._log(f"POST {url} — 创建开通任务")
        resp = self.session.post(url, json=body, headers=headers, timeout=self.timeout)
        data = self._parse_response(resp)

        return JobInfo(
            job_id=data.get("job_id", ""),
            status=JobStatus(data.get("status", "queued")),
            client_ref=data.get("client_ref", ""),
            cost_units=data.get("cost_units", 1),
            resource_mode=data.get("resource_mode", ""),
        )

    # ------------------------------------------------------------------
    # 4. 查询任务
    # ------------------------------------------------------------------

    def get_job(self, job_id: str) -> JobInfo:
        """查询任务状态。"""
        url = f"{self.base_url}{API_PREFIX}/jobs/{job_id}"
        resp = self.session.get(url, timeout=self.timeout)
        data = self._parse_response(resp)

        return JobInfo(
            job_id=data.get("job_id", job_id),
            status=JobStatus(data.get("status", "queued")),
            product=data.get("product", ""),
            result_code=data.get("result_code", ""),
            error_message=data.get("error_message", ""),
            client_ref=data.get("client_ref", ""),
            done=data.get("done", False),
            otp_pending=data.get("otp_pending", False),
            billing_status=data.get("billing_status", ""),
            cost_units=data.get("cost_units", 0),
            callback_status=data.get("callback_status", ""),
            result=data.get("result", {}),
            resource_mode=data.get("resource_mode", ""),
        )

    # ------------------------------------------------------------------
    # 5. 提交 OTP
    # ------------------------------------------------------------------

    def submit_otp(self, job_id: str, otp: str) -> JobInfo:
        """
        手动提交 PayPal OTP 验证码。

        用于手动 OTP 模式: 创建任务时不传 sms_api，
        任务进入 otp_pending 后调用此接口提交验证码。
        """
        url = f"{self.base_url}{API_PREFIX}/jobs/{job_id}/otp"
        body = {"otp": otp}

        self._log(f"POST {url} — 提交 OTP")
        resp = self.session.post(url, json=body, timeout=self.timeout)
        data = self._parse_response(resp)

        return JobInfo(
            job_id=data.get("job_id", job_id),
            status=JobStatus(data.get("status", "queued")),
            done=data.get("done", False),
            otp_pending=data.get("otp_pending", False),
        )

    # ------------------------------------------------------------------
    # 高级: 等待任务完成
    # ------------------------------------------------------------------

    def wait_for_job(
        self,
        job_id: str,
        *,
        timeout: float = DEFAULT_JOB_TIMEOUT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> JobInfo:
        """
        轮询等待任务完成 (带超时)。

        Returns:
            JobInfo with terminal status
        """
        self._log(f"等待任务 {job_id} 完成 (最多 {timeout}s)...")
        deadline = time.time() + timeout

        while time.time() < deadline:
            job = self.get_job(job_id)
            self._log(f"  状态: {job.status.value} | done={job.done} | otp_pending={job.otp_pending}")

            if job.is_terminal:
                return job

            time.sleep(poll_interval)

        # 超时
        return self.get_job(job_id)

    def run_manual_otp_job(
        self,
        request: CreateJobRequest,
        *,
        idempotency_key: str = "",
        job_timeout: float = DEFAULT_JOB_TIMEOUT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> JobInfo:
        """
        手动 OTP 模式全流程:
          1. 创建任务 (不传 sms_api)
          2. 等待到 otp_pending
          3. 返回 job_id，调用方通过 submit_otp() 提交验证码
          4. 等待最终结果

        Returns:
            JobInfo with final status
        """
        # 确保不传 sms_api (手动模式)
        request.sms_api = ""
        request.otp = ""

        # 1. 创建任务
        job = self.create_job(request, idempotency_key=idempotency_key)
        if not job.job_id:
            return job

        self._log(f"  任务已创建: {job.job_id}")

        # 2. 等待进入 otp_pending
        deadline = time.time() + job_timeout
        while time.time() < deadline:
            job = self.get_job(job.job_id)
            if job.otp_pending:
                self._log("  任务进入 OTP 待处理状态，等待外部提供验证码")
                return job
            if job.is_terminal:
                return job
            time.sleep(poll_interval)

        return job

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _parse_response(self, resp: requests.Response) -> dict:
        """解析 HTTP 响应。"""
        if resp.status_code == 429:
            raise RuntimeError("PPXY: 超过并发限制 (429)")
        if resp.status_code == 402:
            raise RuntimeError("PPXY: 额度不足 (402)")
        if resp.status_code == 401:
            raise RuntimeError("PPXY: API Key 无效 (401)")

        resp.raise_for_status()
        try:
            return resp.json()
        except json.JSONDecodeError:
            raise RuntimeError(f"PPXY: 非 JSON 响应: {resp.text[:200]}")

    def _log(self, msg: str):
        if self.verbose:
            print(f"  [IceAix] {msg}")


# ---------------------------------------------------------------------------
# 便捷函数 (无 API Key 时的手动模式标记)
# ---------------------------------------------------------------------------

def manual_cdk_activation_required(access_token: str) -> str:
    """
    当没有 API Key 时，返回 CDK 手动激活的提示信息。
    用户复制 access_token 后在 iceaix.com 网页手动输入 CDK 完成激活。

    Returns:
        提示字符串
    """
    divider = "=" * 60
    return (
        f"\n{divider}\n"
        "  [!] 手动激活步骤 (CDK 模式)\n"
        f"{divider}\n"
        f"  1. 复制 access token (前50字符): {access_token[:50]}...\n"
        "  2. 打开 https://plus.iceaix.com\n"
        "  3. 选择 CDK 模式，粘贴 access token\n"
        "  4. 输入 CDK 激活码\n"
        "  5. 选择 PayPal 日区手机号\n"
        "  6. 等待激活完成\n"
        "  7. 回到脚本继续 OAuth 邮箱绑定\n"
        f"{divider}\n"
    )
