"""
Mailbox providers — ForwardedDomainMailbox and CFWorkerMailbox.

Canonical ForwardedDomainMailbox location: core.mailbox.forwarded_domain
This file retained for backward compatibility.
"""
from __future__ import annotations
import json
import os
from contextlib import contextmanager
import random
import string
from typing import Any

import requests

from core.mailbox.forwarded_domain import ForwardedDomainMailbox, MailboxAccount

import hashlib
import re
import time
from pathlib import Path

@contextmanager
def _email_pool_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+", encoding="utf-8")
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


from core.mailbox.forwarded_domain import extract_verification_code

class LinkApiMailbox:
    """Mailbox adapter for purchased iCloud API rows.

    Supported row formats:
      email----https://host/show/token/email
      email----https://host/show/token/email----code:https://host/api/code/token/email----mail:https://host/api/mail/token/email
      email----code:https://host/api/code/token/email----mail:https://host/api/mail/token/email
      email----http://host/api/mails?recipient=email&top=1
      email----http://host/api/imap/mails?recipient=email&top=5
    """

    def __init__(self, order_file: str = "", order_text: str = "", proxy: str | None = None):
        self.order_file = str(order_file or "").strip()
        self.order_text = str(order_text or "")
        self.proxies = {"http": proxy, "https": proxy} if proxy else None
        self.session = requests.Session()
        self.session.trust_env = False

    def _state_path(self) -> Path:
        path = Path("data/outlook_pool_state.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _record_state(self, email: str, status: str, *, reason: str = "") -> None:
        row = {
            "email": str(email or "").strip().lower(),
            "status": status,
            "job_id": "",
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "last_error": reason,
        }
        with self._state_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "LinkApiMailbox":
        return cls(
            order_file=str(config.get("icloud_api_order_file") or config.get("email_link_api_order_file") or ""),
            order_text=str(config.get("icloud_api_order_text") or config.get("email_link_api_order_text") or ""),
            proxy=str(config.get("mailbox_proxy") or "") or None,
        )

    @staticmethod
    def _classify_api_link(value: str, label: str = "") -> str:
        lowered = str(value or "").strip().lower()
        label = str(label or "").strip().lower()
        if not lowered.startswith(("http://", "https://")):
            return ""
        if label == "code" or "/api/code/" in lowered:
            return "code_url"
        # poualiis / recipient mailbox list APIs (must beat bare "/api/mail" substring checks)
        if (
            label == "mail"
            or "/api/mails" in lowered
            or "/api/imap/mails" in lowered
            or ("recipient=" in lowered and "/mail" in lowered)
            or "/api/mail/" in lowered
        ):
            return "mail_url"
        if label in {"show", "inbox"}:
            return "inbox_url"
        return "inbox_url"

    def _parse_row(self, text: str) -> dict[str, str] | None:
        parts = [part.strip() for part in str(text or "").strip().lstrip("\ufeff").split("----")]
        if len(parts) < 2 or "@" not in parts[0]:
            return None
        row = {"email": parts[0], "inbox_url": "", "code_url": "", "mail_url": ""}
        for part in parts[1:]:
            if not part:
                continue
            label = ""
            value = part
            if ":" in part:
                prefix, suffix = part.split(":", 1)
                if prefix.strip().lower() in {"show", "inbox", "mail", "code"}:
                    label = prefix.strip().lower()
                    value = suffix.strip()
            kind = self._classify_api_link(value, label)
            if not kind:
                continue
            if kind == "code_url":
                row["code_url"] = value
            elif kind == "mail_url":
                row["mail_url"] = value
            elif not row["inbox_url"]:
                row["inbox_url"] = value
        if not (row["inbox_url"] or row["code_url"] or row["mail_url"]):
            return None
        return row

    def _rows(self) -> list[dict[str, str]]:
        raw = self.order_text
        if self.order_file:
            raw += "\n" + Path(self.order_file).read_text(encoding="utf-8-sig")
        rows: list[dict[str, str]] = []
        for line in raw.splitlines():
            row = self._parse_row(line)
            if row:
                rows.append(row)
        if not rows:
            raise RuntimeError("iCloud API 邮箱池为空，格式应为 email----收信URL，可选追加 ----code:验证码API ----mail:邮件API")
        return rows

    def _blocked_emails(self) -> set[str]:
        path = self._state_path()
        latest: dict[str, str] = {}
        if not path.exists():
            return set()
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            email = str(row.get("email") or "").strip().lower()
            status = str(row.get("status") or "").strip()
            if email:
                latest[email] = status
        # "reserved" is a soft in-flight mark and must not permanently block re-lease.
        # Exclusive ownership is enforced by resource_pool leases.
        return {email for email, status in latest.items() if status in {"consumed", "dirty_email_already_used", "cooldown"}}

    def _account_from_row(self, row: dict[str, str]) -> MailboxAccount:
        return MailboxAccount(email=row["email"], account_id=row["email"], extra={"provider_name": "icloud_api", **row})

    def create_account(self) -> MailboxAccount:
        state_path = self._state_path()
        with _email_pool_lock(state_path.with_suffix(state_path.suffix + ".lock")):
            blocked = self._blocked_emails()
            for row in self._rows():
                normalized = row["email"].strip().lower()
                if normalized not in blocked:
                    self._record_state(normalized, "reserved", reason="icloud email registration lease")
                    return self._account_from_row(row)
        raise RuntimeError("iCloud API 邮箱池没有可用邮箱；所有邮箱都已失败或被消费")

    def account_for_email(self, email: str) -> MailboxAccount:
        target = str(email or "").strip().lower()
        for row in self._rows():
            if row["email"].strip().lower() == target:
                return self._account_from_row(row)
        raise RuntimeError(f"iCloud API 邮箱池未找到邮箱: {email}")

    def _fetch_text(self, account: MailboxAccount) -> str:
        url = str((account.extra or {}).get("inbox_url") or "").strip()
        if not url:
            raise RuntimeError("iCloud API 邮箱缺少收信 URL")
        response = self.session.get(url, params={"n": 1, "_": int(time.time())}, proxies=self.proxies, timeout=15)
        if response.status_code >= 400:
            raise RuntimeError(f"iCloud API 收信失败: status={response.status_code}")
        return response.text or ""

    def _fetch_json_url(self, url: str, label: str) -> dict[str, Any]:
        response = self.session.get(url, params={"_": int(time.time())}, proxies=self.proxies, timeout=15)
        if response.status_code >= 400:
            raise RuntimeError(f"iCloud API {label} 失败: status={response.status_code}")
        try:
            data = response.json()
        except Exception as exc:
            raise RuntimeError(f"iCloud API {label} 未返回 JSON: {response.text[:200]}") from exc
        return data if isinstance(data, dict) else {"data": data}

    def _payload_data(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = payload.get("data") if isinstance(payload, dict) else None
        return data if isinstance(data, dict) else payload

    def _code_marker(self, data: dict[str, Any]) -> str:
        code = str(data.get("code") or data.get("verification_code") or data.get("latest_verification_code") or "").strip()
        if not code:
            return ""
        parts = [
            str(data.get("message_id") or ""),
            str(data.get("mail_time") or data.get("latest_verification_message_date") or ""),
            str(data.get("updated_at") or ""),
            code,
        ]
        return "code-api:" + hashlib.sha1("|".join(parts).encode("utf-8", "ignore")).hexdigest()

    def _extract_code_from_payload(self, payload: dict[str, Any], code_pattern: str | None = None) -> tuple[str, str, bool]:
        data = self._payload_data(payload)
        code = str(data.get("code") or data.get("verification_code") or data.get("latest_verification_code") or "").strip()
        marker = self._code_marker(data)
        stale = bool(data.get("stale_code"))
        found = data.get("found")
        if code_pattern and code:
            match = re.search(code_pattern, code)
            code = (match.group(1) if match and match.groups() else match.group(0)) if match else ""
        if code and found is not False and not stale:
            return code, marker, False
        return "", marker, stale

    def _message_marker(self, mail: dict[str, Any]) -> str:
        raw_id = str(mail.get("message_id") or mail.get("id") or mail.get("emailId") or "").strip()
        if raw_id:
            return f"mail-api:{raw_id}"
        raw = json.dumps(mail, ensure_ascii=False, sort_keys=True)
        return "mail-api:" + hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()

    def _is_poualiis_payload(self, data: dict[str, Any]) -> bool:
        return isinstance(data, dict) and "msg" in data and ("status" in data or "mailbox" in data or "time" in data)

    def _extract_code_from_mail_payload(self, payload: dict[str, Any], code_pattern: str | None = None) -> tuple[str, set[str]]:
        data = self._payload_data(payload)
        markers: set[str] = set()

        # poualiis-style single latest mail:
        # {"mailbox":"INBOX","msg":"...code...","status":true,"time":"..."}
        if self._is_poualiis_payload(data):
            status = data.get("status")
            msg = str(data.get("msg") or "").strip()
            # Empty / not-found responses must not create durable markers, otherwise a later
            # real OTP can be blocked if the marker set is reused as baseline.
            if status is False or not msg:
                return "", set()
            marker_seed = "|".join(
                [
                    str(data.get("mailbox") or ""),
                    str(data.get("time") or ""),
                    msg[:240],
                ]
            )
            marker = "mail-api:poualiis:" + hashlib.sha1(marker_seed.encode("utf-8", "ignore")).hexdigest()
            markers.add(marker)
            if code_pattern:
                match = re.search(code_pattern, msg)
                if match:
                    return (match.group(1) if match.groups() else match.group(0)), markers
            candidate = extract_verification_code(msg, expected_lengths=(6,))
            if candidate:
                return candidate, markers
            for pattern in (
                r"verification code\D{0,80}(\d{6})",
                r"temporary verification code\D{0,80}(\d{6})",
                r"临时验证码\D{0,80}(\d{6})",
                r"code is\D{0,40}(\d{6})",
                r"enter the code below[^\d]{0,80}(\d{6})",
                r"\b(\d{6})\b",
            ):
                match = re.search(pattern, msg, flags=re.IGNORECASE)
                if match:
                    return str(match.group(1)), markers
            return "", markers

        code = str(data.get("latest_verification_code") or "").strip()
        if code:
            markers.add(self._code_marker(data))
            if code_pattern:
                match = re.search(code_pattern, code)
                code = (match.group(1) if match and match.groups() else match.group(0)) if match else ""
            if code:
                return code, markers
        messages = []
        for key in ("messages", "archive_messages"):
            value = data.get(key)
            if isinstance(value, list):
                messages.extend(item for item in value if isinstance(item, dict))
        # poualiis /api/mails/all style: results[].messages[]
        results = data.get("results")
        if isinstance(results, list):
            for item in results:
                if not isinstance(item, dict):
                    continue
                value = item.get("messages")
                if isinstance(value, list):
                    messages.extend(msg for msg in value if isinstance(msg, dict))
        for mail in messages:
            marker = self._message_marker(mail)
            markers.add(marker)
            raw = " ".join(
                str(mail.get(field) or "")
                for field in ("subject", "text", "content", "html", "body", "body_text", "body_preview", "snippet", "msg")
            )
            if code_pattern:
                match = re.search(code_pattern, raw)
                if match:
                    return match.group(1) if match.groups() else match.group(0), markers
            candidate = extract_verification_code(raw, expected_lengths=(6,))
            if candidate:
                return candidate, markers
        return "", markers

    def _extract_code_from_html(self, html: str, code_pattern: str | None = None) -> str:
        import html as html_lib
        newest_card = re.search(r'(?is)<div class="card"[^>]*>(.*?)(?=<div class="card"|</body>|</html>|$)', html)
        source = newest_card.group(1) if newest_card else html
        text = re.sub(r"(?is)<script[^>]*>.*?</script>|<style[^>]*>.*?</style>", " ", source)
        text = re.sub(r"(?is)<[^>]+>", " ", text)
        text = html_lib.unescape(text)
        text = re.sub(r"https?://\S+", " ", text)
        text = re.sub(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        if code_pattern:
            match = re.search(code_pattern, text)
            if match:
                return match.group(1) if match.groups() else match.group(0)
        phrase_patterns = (
            r"verification code\D{0,80}(\d{6})",
            r"temporary verification code\D{0,80}(\d{6})",
            r"認証コード\D{0,80}(\d{6})",
            r"検証コード\D{0,80}(\d{6})",
            r"一時的な認証コード\D{0,120}(\d{6})",
            r"一時検証コード\D{0,120}(\d{6})",
            r"临时验证码\D{0,80}(\d{6})",
            r"code is\D{0,40}(\d{6})",
        )
        for pattern in phrase_patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return str(match.group(1))
        return extract_verification_code(text, expected_lengths=(6,))

    def get_current_ids(self, account: MailboxAccount) -> set[str]:
        markers: set[str] = set()
        code_url = str((account.extra or {}).get("code_url") or "").strip()
        if code_url:
            try:
                _code, marker, _stale = self._extract_code_from_payload(self._fetch_json_url(code_url, "code"))
                if marker:
                    markers.add(marker)
            except Exception:
                pass
        mail_url = str((account.extra or {}).get("mail_url") or "").strip()
        if mail_url:
            try:
                payload = self._fetch_json_url(mail_url, "mail")
                data = self._payload_data(payload)
                # poualiis top=1 is a single latest-mail slot, not a message list.
                # Never put its OTP marker into the baseline, or the very code we need
                # will be treated as "already seen" when otp_callback finally runs.
                if self._is_poualiis_payload(data):
                    return set()
                _code, mail_markers = self._extract_code_from_mail_payload(payload)
                markers.update(mail_markers)
            except Exception:
                pass
        if markers:
            return markers
        try:
            text = self._fetch_text(account)
        except Exception:
            return set()
        if "0 封邮件" in text or "等待接收邮件" in text:
            return set()
        return {hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()}

    def wait_for_code(self, account: MailboxAccount, *, timeout: int = 180, before_ids: set[str] | None = None, code_pattern: str | None = None) -> str:
        seen = set(before_ids or set())
        deadline = time.time() + timeout
        code_url = str((account.extra or {}).get("code_url") or "").strip()
        mail_url = str((account.extra or {}).get("mail_url") or "").strip()
        inbox_url = str((account.extra or {}).get("inbox_url") or "").strip()
        last_error = ""
        # For poualiis-style single-latest APIs, track the last accepted OTP marker so
        # we still accept a brand-new code even if an older baseline already exists.
        last_poualiis_marker = ""
        last_poualiis_code = ""
        while time.time() < deadline:
            if code_url:
                try:
                    payload = self._fetch_json_url(code_url, "code")
                    code, marker, stale = self._extract_code_from_payload(payload, code_pattern=code_pattern)
                    if marker and marker not in seen and code:
                        return code
                    if marker and (stale or not code):
                        seen.add(marker)
                except Exception as exc:
                    last_error = str(exc).splitlines()[0][:160]
            if mail_url:
                try:
                    payload = self._fetch_json_url(mail_url, "mail")
                    data = self._payload_data(payload)
                    code, markers = self._extract_code_from_mail_payload(payload, code_pattern=code_pattern)
                    if self._is_poualiis_payload(data):
                        marker = next(iter(markers), "")
                        # Empty not-found: keep polling.
                        if not code:
                            pass
                        # First code, or a different code/marker than last accepted.
                        elif (not last_poualiis_marker and not last_poualiis_code) or (
                            marker and marker != last_poualiis_marker
                        ) or (code and code != last_poualiis_code):
                            # Also honor explicit before_ids only when the same code was already consumed.
                            if not (marker and marker in seen and code == last_poualiis_code):
                                last_poualiis_marker = marker
                                last_poualiis_code = code
                                return code
                    else:
                        fresh_markers = markers - seen
                        if code and fresh_markers:
                            return code
                        seen.update(markers)
                except Exception as exc:
                    last_error = str(exc).splitlines()[0][:160]
            if inbox_url:
                try:
                    text = self._fetch_text(account)
                    marker = hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()
                    if marker not in seen:
                        code = self._extract_code_from_html(text, code_pattern=code_pattern)
                        if code:
                            return code
                        seen.add(marker)
                except Exception as exc:
                    last_error = str(exc).splitlines()[0][:160]
            time.sleep(3)
        detail = f"; last_error={last_error}" if last_error else ""
        raise TimeoutError(f"等待 iCloud API 邮箱验证码超时 ({timeout}s){detail}")

class ICloudPrivacyMailbox(ForwardedDomainMailbox):
    """Imported iCloud Hide My Email aliases, with OTP delivered to an IMAP inbox such as 163."""

    def __init__(self, order_file: str = "", order_text: str = "", **kwargs: Any):
        super().__init__(domain="icloud.com", **kwargs)
        self.order_file = str(order_file or "").strip()
        self.order_text = str(order_text or "")

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "ICloudPrivacyMailbox":
        return cls(
            order_file=str(config.get("icloud_privacy_order_file") or ""),
            order_text=str(config.get("icloud_privacy_order_text") or ""),
            imap_user=str(config.get("mailbox_imap_user") or ""),
            imap_pass=str(config.get("mailbox_imap_pass") or ""),
            imap_host=str(config.get("mailbox_imap_host") or ""),
            imap_port=int(config.get("mailbox_imap_port") or 993),
        )

    def _state_path(self) -> Path:
        path = Path("data/icloud_privacy_pool_state.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _rows(self) -> list[str]:
        raw = self.order_text
        if self.order_file:
            raw += "\n" + Path(self.order_file).read_text(encoding="utf-8-sig")
        rows: list[str] = []
        for line in raw.splitlines():
            text = line.strip().lstrip("\ufeff")
            if not text:
                continue
            email = text.split("----", 1)[0].strip().lower()
            if "@" not in email:
                raise RuntimeError(f"iCloud 隐私邮箱行格式错误: {text[:80]}")
            rows.append(email)
        if not rows:
            raise RuntimeError("iCloud 隐私邮箱池为空，格式应为每行一个 iCloud 隐私邮箱账号")
        return rows

    def _record_state(self, email: str, status: str, *, reason: str = "") -> None:
        row = {"email": str(email or "").strip().lower(), "status": status, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "last_error": reason}
        with self._state_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _latest_status_by_email(self) -> dict[str, str]:
        path = self._state_path()
        latest: dict[str, str] = {}
        if not path.exists():
            return latest
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            email = str(row.get("email") or "").strip().lower()
            status = str(row.get("status") or "").strip()
            if email:
                latest[email] = status
        return latest

    def _blocked_emails(self, *, block_reserved: bool = True) -> set[str]:
        statuses = {"consumed", "dirty_email_already_used", "cooldown"}
        if block_reserved:
            statuses.add("reserved")
        return {email for email, status in self._latest_status_by_email().items() if status in statuses}

    def create_account(self) -> MailboxAccount:
        state_path = self._state_path()
        with _email_pool_lock(state_path.with_suffix(state_path.suffix + ".lock")):
            rows = self._rows()
            # Resource-pool tasks inject exactly one leased email into order_text.
            # The SQLite resource lease already provides concurrency control, so a stale
            # JSONL "reserved" marker must not make the leased singleton unusable.
            blocked = self._blocked_emails(block_reserved=not (len(rows) == 1 and not self.order_file))
            for email in rows:
                if email not in blocked:
                    self._record_state(email, "reserved", reason="icloud privacy email registration lease")
                    return MailboxAccount(email=email, account_id=email, extra={"provider_name": "icloud_privacy", "imap_user": self.imap_user})
        raise RuntimeError("iCloud 隐私邮箱池没有可用邮箱；所有邮箱都已失败或被消费")

    def account_for_email(self, email: str) -> MailboxAccount:
        target = str(email or "").strip().lower()
        for row_email in self._rows():
            if row_email == target:
                return MailboxAccount(email=row_email, account_id=row_email, extra={"provider_name": "icloud_privacy", "imap_user": self.imap_user})
        raise RuntimeError(f"iCloud 隐私邮箱池未找到邮箱: {email}")

class CFWorkerMailbox:
    """Cloudflare Worker / cloud-mail mailbox adapter used for OAuth binding OTP."""

    def __init__(self, api_url: str, admin_token: str = "", domain: str = "", fingerprint: str = "", proxy: str | None = None):
        self.api = str(api_url or "").rstrip("/")
        self.admin_token = str(admin_token or "")
        self.domain = str(domain or "").strip().lstrip("@")
        self.fingerprint = str(fingerprint or "")
        self.proxies = {"http": proxy, "https": proxy} if proxy else None
        self._api_mode = "auto"

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "CFWorkerMailbox":
        return cls(
            api_url=str(config.get("cfworker_api_url") or config.get("mailbox_api_url") or ""),
            admin_token=str(config.get("cfworker_admin_token") or config.get("mailbox_admin_token") or ""),
            domain=str(config.get("cfworker_domain") or config.get("mailbox_domain") or ""),
            fingerprint=str(config.get("cfworker_fingerprint") or ""),
            proxy=str(config.get("mailbox_proxy") or config.get("proxy") or "") or None,
        )

    def _headers(self) -> dict[str, str]:
        headers = {"accept": "application/json, text/plain, */*", "content-type": "application/json", "x-admin-auth": self.admin_token}
        if self.fingerprint:
            headers["x-fingerprint"] = self.fingerprint
        return headers

    def _cloud_headers(self) -> dict[str, str]:
        return {"accept": "application/json, text/plain, */*", "content-type": "application/json", "Authorization": self.admin_token}

    def _json(self, response, label: str) -> dict[str, Any]:
        try:
            data = response.json()
        except Exception as exc:
            raise RuntimeError(f"{label} 未返回 JSON: status={getattr(response, 'status_code', '?')} body={str(getattr(response, 'text', '') or '')[:200]}") from exc
        return data if isinstance(data, dict) else {"data": data}

    def _detect_api_mode(self) -> str:
        if self._api_mode in {"cfworker", "cloud_mail"}:
            return self._api_mode
        try:
            response = requests.get(f"{self.api}/api/setting/websiteConfig", headers={"accept": "application/json, text/plain, */*"}, proxies=self.proxies, timeout=5)
            data = self._json(response, "Cloud Mail websiteConfig")
            config = data.get("data") if isinstance(data, dict) else None
            if data.get("code") == 200 and isinstance(config, dict):
                domains = {str(item or "").strip().lstrip("@") for item in (config.get("domainList") or []) if str(item or "").strip()}
                if self.domain and domains and self.domain not in domains:
                    raise RuntimeError(f"Cloud Mail 未启用邮箱域名 {self.domain}，当前可用域名: {', '.join(sorted(domains))}")
                self._api_mode = "cloud_mail"
                return self._api_mode
        except Exception:
            pass
        self._api_mode = "cfworker"
        return self._api_mode

    def create_account(self) -> MailboxAccount:
        if not self.api:
            raise RuntimeError("未配置 CFWorker/Cloud Mail API 地址")
        name = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
        if self._detect_api_mode() == "cloud_mail":
            return self._create_cloud_mail_account(name)
        return self._create_cfworker_account(name)

    def _create_cloud_mail_account(self, name: str) -> MailboxAccount:
        if not self.domain:
            raise RuntimeError("Cloud Mail 未配置邮箱域名")
        if not self.admin_token:
            raise RuntimeError("Cloud Mail 需要开放 API Token")
        email = f"{name}@{self.domain}"
        response = requests.post(f"{self.api}/api/public/addUser", json={"list": [{"email": email}]}, headers=self._cloud_headers(), proxies=self.proxies, timeout=15)
        data = self._json(response, "Cloud Mail addUser")
        if getattr(response, "status_code", 200) >= 400 or data.get("code") not in (None, 200):
            raise RuntimeError(f"Cloud Mail addUser 失败: status={getattr(response, 'status_code', '?')} resp={str(data)[:200]}")
        self._api_mode = "cloud_mail"
        return MailboxAccount(email=email, account_id=email, extra={"provider_name": "cloud_mail", "api_url": self.api, "domain": self.domain})

    def _create_cfworker_account(self, name: str) -> MailboxAccount:
        payload: dict[str, Any] = {"enablePrefix": True, "name": name}
        if self.domain:
            payload["domain"] = self.domain
        response = requests.post(f"{self.api}/admin/new_address", json=payload, headers=self._headers(), proxies=self.proxies, timeout=15)
        data = self._json(response, "CFWorker new_address")
        email = str(data.get("email") or data.get("address") or "")
        token = str(data.get("token") or data.get("jwt") or "")
        if not email:
            raise RuntimeError(f"CFWorker new_address 未返回邮箱: {str(data)[:200]}")
        self._api_mode = "cfworker"
        return MailboxAccount(email=email, account_id=token or email, extra={"provider_name": "cfworker", "api_url": self.api, "domain": self.domain, "token": token})

    def _get_cloud_mail_mails(self, email: str) -> list[dict[str, Any]]:
        queries = [email, f"%{email}%"]
        for query in queries:
            response = requests.post(f"{self.api}/api/public/emailList", json={"toEmail": query, "timeSort": "desc", "num": 1, "size": 50}, headers=self._cloud_headers(), proxies=self.proxies, timeout=10)
            data = self._json(response, "Cloud Mail emailList")
            if getattr(response, "status_code", 200) >= 400 or data.get("code") not in (None, 200):
                raise RuntimeError(f"Cloud Mail emailList 失败: status={getattr(response, 'status_code', '?')} resp={str(data)[:200]}")
            items = data.get("data", data)
            if isinstance(items, dict):
                items = items.get("list", [])
            mails = []
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    raw = " ".join(str(item.get(field) or "") for field in ("subject", "sendEmail", "sendName", "content", "text"))
                    mails.append({**item, "id": item.get("emailId", item.get("id", "")), "raw": raw})
            if mails:
                return mails
        return []

    def _get_cfworker_mails(self, account: MailboxAccount) -> list[dict[str, Any]]:
        response = requests.get(f"{self.api}/admin/mails", params={"limit": 20, "offset": 0, "address": account.email}, headers=self._headers(), proxies=self.proxies, timeout=10)
        data = self._json(response, "CFWorker mails")
        items = data.get("results", data)
        return items if isinstance(items, list) else []

    def _get_mails(self, account: MailboxAccount) -> list[dict[str, Any]]:
        if self._detect_api_mode() == "cloud_mail":
            return self._get_cloud_mail_mails(account.email)
        try:
            return self._get_cfworker_mails(account)
        except Exception:
            return self._get_cloud_mail_mails(account.email)

    def get_current_ids(self, account: MailboxAccount) -> set[str]:
        try:
            return {str(mail.get("id") or "") for mail in self._get_mails(account) if str(mail.get("id") or "")}
        except Exception:
            return set()

    def wait_for_code(self, account: MailboxAccount, *, timeout: int = 180, before_ids: set[str] | None = None, code_pattern: str | None = None) -> str:
        seen = set(before_ids or set())
        deadline = time.time() + timeout
        while time.time() < deadline:
            for mail in self._get_mails(account):
                mid = str(mail.get("id") or "")
                if mid and mid in seen:
                    continue
                if mid:
                    seen.add(mid)
                raw = str(mail.get("raw") or mail.get("content") or mail.get("text") or mail.get("subject") or "")
                raw = re.sub(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", "", raw)
                raw = re.sub(r"m=\+\d+\.\d+", "", raw)
                raw = re.sub(r"\bt=\d+\b", "", raw)
                if code_pattern:
                    match = re.search(code_pattern, raw)
                    if match:
                        return match.group(1) if match.groups() else match.group(0)
                code = extract_verification_code(raw, expected_lengths=(6,))
                if code:
                    return code
            time.sleep(3)
        raise TimeoutError(f"等待邮箱验证码超时 ({timeout}s)")
