"""ChatGPT phone registration — pure protocol. Based on heartmore/chatgpt-auto-register."""
from __future__ import annotations
import json, uuid, urllib3
from typing import Callable
from urllib.parse import urlencode
from curl_cffi import requests as curl_requests
from .sentinel import Sentinel
urllib3.disable_warnings()

CHATGPT, AUTH = "https://chatgpt.com", "https://auth.openai.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"

JSON_H = {
    "accept": "application/json", "accept-language": "en-US,en;q=0.9",
    "content-type": "application/json", "user-agent": UA,
    "sec-ch-ua": '"Google Chrome";v="145"', "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty", "sec-fetch-mode": "cors", "sec-fetch-site": "same-origin",
}
NAV_H = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9", "user-agent": UA,
    "sec-ch-ua": '"Google Chrome";v="145"', "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "document", "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none", "upgrade-insecure-requests": "1",
}


def _protocol_proxy(proxy: str) -> str:
    """Use proxy form compatible with curl_cffi while keeping browser config unchanged."""
    if proxy.startswith("socks5://"):
        return "socks5h://" + proxy[len("socks5://"):]
    return proxy

class ChatGPTProtocolRegister:
    def __init__(self, proxy="", verbose=True):
        self.s = curl_requests.Session(impersonate="chrome", verify=False)
        if proxy:
            protocol_proxy = _protocol_proxy(proxy)
            self.s.proxies = {"http": protocol_proxy, "https": protocol_proxy}
        self.verbose = verbose
        self.did = str(uuid.uuid4())
        self.sentinel = Sentinel(self.did)
        self._sc: dict = {}
    def _log(self, n, m):
        if self.verbose: print(f"  [{n:02d}] {m}")
    def _sen(self, flow):
        if flow not in self._sc:
            try:
                self._sc[flow] = self.sentinel.get(self.s, flow)
            except Exception as e:
                self._sc[flow] = {"token":"", "so_token":"", "error": str(e) or "Sentinel token acquisition failed"}
        return self._sc[flow]
    def _sentinel_failure(self, flow, step):
        err = self._sen(flow).get("error") or "Sentinel returned empty token"
        reason = f"Sentinel token missing for {flow}: {err}"
        return {
            "_status": "sentinel_token_missing",
            "_body": reason,
            "status": "failed",
            "failed_step": step,
            "failure_reason": reason,
            "retryable": True,
            "next_action": "retry_with_fresh_session",
        }
    def _add_sen(self, h, flow, step):
        st = self._sen(flow)
        if not st.get("token"):
            return self._sentinel_failure(flow, step)
        h["OpenAI-Sentinel-Token"] = st["token"]
        if st.get("so_token"): h["OpenAI-Sentinel-SO-Token"] = st["so_token"]
        return None

    def visit(self):
        self._log(1, "GET chatgpt.com/auth/login")
        self.s.get(f"{CHATGPT}/auth/login", headers=NAV_H, allow_redirects=True, timeout=30)

    def get_csrf(self):
        self._log(2, "GET /api/auth/csrf")
        r = self.s.get(f"{CHATGPT}/api/auth/csrf", headers={**JSON_H,"origin":CHATGPT,"referer":f"{CHATGPT}/auth/login"}, timeout=30)
        csrf = r.json().get("csrfToken")
        if not csrf: raise RuntimeError("csrf failed")
        return csrf

    def signin(self, phone, csrf):
        self._log(3, "POST /api/auth/signin/openai")
        encoded = phone.replace("+","%2B")
        qs = "&".join(f"{k}={v}" for k,v in {"prompt":"login","screen_hint":"login_or_signup","login_hint":encoded,"ext-oai-did":self.did,"auth_session_logging_id":str(uuid.uuid4())}.items())
        r = self.s.post(f"{CHATGPT}/api/auth/signin/openai?{qs}", data={"callbackUrl":"/","csrfToken":csrf,"json":"true"}, headers={**JSON_H,"content-type":"application/x-www-form-urlencoded","origin":CHATGPT,"referer":f"{CHATGPT}/auth/login"}, allow_redirects=False, timeout=30)
        return r.json().get("url","")

    def establish_session(self, url=""):
        target = url or f"{AUTH}/create-account/password"
        self._log(4, "GET authorize/create-account session")
        return self.s.get(target, headers={**NAV_H,"referer":CHATGPT}, allow_redirects=True, timeout=30)

    def register_user(self, phone, password):
        self._log(5, "POST /api/accounts/user/register")
        h = {**JSON_H,"referer":f"{AUTH}/create-account/password","oai-device-id":self.did}
        failure = self._add_sen(h, "username_password_create", "register_user")
        if failure: return failure
        r = self.s.post(f"{AUTH}/api/accounts/user/register", json={"username":phone,"password":password}, headers=h, timeout=30)
        d = r.json() if r.headers.get("content-type","").startswith("application/json") else {}
        d["_status"] = r.status_code
        d["_body"] = r.text[:500] if r.text else ""
        return d

    def send_otp(self, url):
        self._log(6, "GET /api/accounts/phone-otp/send")
        r = self.s.get(url, headers={**NAV_H,"referer":f"{AUTH}/create-account/password"}, allow_redirects=True, timeout=30)
        return {"_status": r.status_code, "_url": str(r.url), "_body": r.text[:500] if r.text else ""}

    def validate_otp(self, code):
        self._log(7, "POST /api/accounts/phone-otp/validate")
        h = {**JSON_H,"referer":f"{AUTH}/contact-verification","oai-device-id":self.did}
        failure = self._add_sen(h, "authorize_continue", "validate_otp")
        if failure: return failure
        r = self.s.post(f"{AUTH}/api/accounts/phone-otp/validate", json={"code":code}, headers=h, timeout=30)
        d = r.json() if r.ok else {}
        d["_status"] = r.status_code
        return d

    def create_account(self, name, birthdate):
        self._log(8, "POST /api/accounts/create_account")
        h = {**JSON_H,"referer":f"{AUTH}/about-you","oai-device-id":self.did}
        failure = self._add_sen(h, "oauth_create_account", "create_account")
        if failure: return failure
        r = self.s.post(f"{AUTH}/api/accounts/create_account", json={"name":name,"birthdate":birthdate}, headers=h, allow_redirects=False, timeout=30)
        d = r.json() if r.headers.get("content-type","").startswith("application/json") else {}
        d["_status"] = r.status_code
        d["_body"] = r.text[:500] if r.text else ""
        return d

    def visit_about_you(self, url):
        self._log(-1, "GET about-you page")
        url = url if url.startswith("http") else f"{AUTH}{url}"
        self.s.get(url, headers={**NAV_H,"referer":f"{AUTH}/contact-verification","sec-fetch-site":"same-origin"}, allow_redirects=True, timeout=30)

    def oauth_callback(self, url):
        self._log(9, "GET oauth callback")
        self.s.get(url, headers={**NAV_H,"referer":AUTH,"sec-fetch-site":"cross-site"}, allow_redirects=True, timeout=30)
        return self.s.cookies.get("__Secure-next-auth.session-token","")

    def get_access_token(self):
        r = self.s.get(f"{CHATGPT}/api/auth/session", headers=JSON_H, timeout=30)
        try: return r.json().get("accessToken","")
        except Exception: return ""


def register_phone_account(phone, password, proxy="", sms_wait_fn=None, name="A", birthdate="2000-01-01", verbose=True):
    r = ChatGPTProtocolRegister(proxy=proxy, verbose=verbose)
    try:
        r.visit(); csrf = r.get_csrf(); redir = r.signin(phone, csrf)
        if not redir: return {"ok":False,"error":"signin failed"}
        r.establish_session(redir)
        ret = r.register_user(phone, password)
        curl = ret.get("continue_url","")
        if not curl:
            e = ret.get("_body","") or f"status={ret.get('_status')}"
            if "already registered" in str(e).lower(): return {"ok":False,"error":"PHONE_ALREADY_REGISTERED"}
            return {"ok":False,"error":f"register failed: {e[:200]}"}
        r.send_otp(curl)
        if not sms_wait_fn: return {"ok":False,"error":"SMS wait fn required"}
        code = sms_wait_fn()
        if not code: return {"ok":False,"error":"SMS timeout"}
        ret = r.validate_otp(str(code))
        curl = ret.get("continue_url","")
        if not curl: return {"ok":False,"error":f"OTP validation failed (status={ret.get('_status')})"}
        r.visit_about_you(curl)
        ret = r.create_account(name, birthdate)
        curl = ret.get("continue_url","")
        if not curl: return {"ok":False,"error":f"create_account failed: {ret.get('_body','')[:200]}"}
        r.oauth_callback(curl)
        at = r.get_access_token()
        if not at:
            return {"ok":False,"phone":phone,"password":password,"error":"access token missing"}
        return {"ok":True,"phone":phone,"password":password,"access_token":at}
    except Exception as e:
        return {"ok":False,"error":str(e)}
