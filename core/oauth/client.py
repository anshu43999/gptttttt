"""
OAuth PKCE Client — minimal PKCE implementation for Codex CLI OAuth flow.

Reuses core logic from platforms/chatgpt/oauth.py for PKCE generation and
token exchange, without any browser fingerprinting (that's BrowserSession's domain).
"""

import urllib.parse

from platforms.chatgpt.oauth import (
    _pkce_verifier,
    _post_form,
    _random_state,
    _sha256_b64url_no_pad,
)

OPENAI_AUTH = "https://auth.openai.com"


class OAuthClient:
    """Minimal OAuth PKCE client for the Codex CLI public client."""

    CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
    REDIRECT_URI = "http://localhost:1455/auth/callback"
    SCOPE = "openid email profile offline_access"

    # ------------------------------------------------------------------
    # PKCE
    # ------------------------------------------------------------------

    @staticmethod
    def generate_pkce():
        """Return (code_verifier: str, code_challenge: str)."""
        verifier = _pkce_verifier()
        challenge = _sha256_b64url_no_pad(verifier)
        return verifier, challenge

    # ------------------------------------------------------------------
    # Authorize URL
    # ------------------------------------------------------------------

    @classmethod
    def build_authorize_url(cls, code_challenge):
        """Build the full OAuth authorize URL for the given PKCE challenge."""
        state = _random_state()
        params = {
            "client_id": cls.CLIENT_ID,
            "response_type": "code",
            "redirect_uri": cls.REDIRECT_URI,
            "scope": cls.SCOPE,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "prompt": "login",
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
        }
        base_url = f"{OPENAI_AUTH}/oauth/authorize"
        return f"{base_url}?{urllib.parse.urlencode(params)}"

    # ------------------------------------------------------------------
    # Token exchange
    # ------------------------------------------------------------------

    @classmethod
    def exchange_code(cls, code, code_verifier):
        """Exchange an authorization code for tokens.

        Returns a dict with keys: access_token, refresh_token, id_token.
        """
        token_resp = _post_form(
            f"{OPENAI_AUTH}/oauth/token",
            {
                "grant_type": "authorization_code",
                "client_id": cls.CLIENT_ID,
                "code": code,
                "redirect_uri": cls.REDIRECT_URI,
                "code_verifier": code_verifier,
            },
        )
        return {
            "access_token": (token_resp.get("access_token") or "").strip(),
            "refresh_token": (token_resp.get("refresh_token") or "").strip(),
            "id_token": (token_resp.get("id_token") or "").strip(),
        }
