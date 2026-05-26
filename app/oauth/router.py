"""
Mock OAuth 2.1 with Dynamic Client Registration.

Claude's connector flow expects all four endpoints (discovery, register,
authorize, token). This implementation auto-accepts everything — it exists
so the handshake completes, not to actually authenticate anybody.
"""

from __future__ import annotations

import json
import secrets
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from .. import state as app_state
from ..config import BASE_URL, DEMO_USER

router = APIRouter()


@router.get("/.well-known/oauth-authorization-server")
async def oauth_discovery() -> dict[str, Any]:
    return {
        "issuer": BASE_URL,
        "authorization_endpoint": f"{BASE_URL}/oauth/authorize",
        "token_endpoint": f"{BASE_URL}/oauth/token",
        "registration_endpoint": f"{BASE_URL}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "none"],
        "scopes_supported": ["mcp"],
    }


async def _safe_json(request: Request) -> Any:
    try:
        return await request.json()
    except Exception:
        return None


@router.post("/oauth/register")
async def oauth_register(request: Request) -> dict[str, Any]:
    body = await _safe_json(request)
    client_id = app_state.new_id("client")
    client_secret = secrets.token_urlsafe(24)
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "client_id_issued_at": int(time.time()),
        "client_secret_expires_at": 0,
        "redirect_uris": body.get("redirect_uris", []) if isinstance(body, dict) else [],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "client_secret_post",
    }


@router.get("/oauth/authorize")
async def oauth_authorize(
    response_type: str = "code",
    client_id: str = "",
    redirect_uri: str = "",
    state: str = "",
    code_challenge: str = "",
    code_challenge_method: str = "",
    scope: str = "",
) -> HTMLResponse:
    """
    Issue a fake auth code and redirect back via HTML — Claude opens this in
    a real browser window, so we need a page that auto-redirects (not a JSON
    response).
    """
    auth_code = secrets.token_urlsafe(16)
    safe_redirect = redirect_uri or "/"
    sep = "&" if "?" in safe_redirect else "?"
    final_url = f"{safe_redirect}{sep}code={auth_code}&state={state}"
    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>NAV AI · Sign in</title>
  <meta http-equiv="refresh" content="1;url={final_url}" />
  <style>
    body {{ background:#0a0d12; color:#e8e6e0; font:14px/1.6 ui-sans-serif,system-ui,-apple-system,sans-serif;
            display:grid; place-items:center; height:100vh; margin:0; }}
    .card {{ border:1px solid #1f2630; padding:32px 40px; text-align:center; max-width:380px; }}
    h1 {{ font:italic 28px/1.1 ui-serif,Georgia,serif; margin:0 0 8px; letter-spacing:.02em; }}
    .sub {{ color:#7a8090; font-size:13px; margin-bottom:24px; }}
    .dot {{ display:inline-block; width:6px; height:6px; background:#d4a85a; margin-right:6px;
            vertical-align:middle; animation:p 1.2s ease-in-out infinite; }}
    @keyframes p {{ 0%,100%{{opacity:.3}} 50%{{opacity:1}} }}
  </style>
</head>
<body>
  <div class="card">
    <h1>NAV AI</h1>
    <div class="sub">Authenticating as {DEMO_USER}</div>
    <div><span class="dot"></span>completing sign-in…</div>
  </div>
  <script>setTimeout(function(){{ window.location = {json.dumps(final_url)}; }}, 1000);</script>
</body>
</html>"""
    return HTMLResponse(page)


@router.post("/oauth/token")
async def oauth_token(request: Request) -> dict[str, Any]:
    """Accept anything; return a fake bearer token."""
    # Body intentionally unread — mock accepts all grant flows.
    _ = await _safe_json(request)
    token = secrets.token_urlsafe(32)
    app_state.issued_tokens.add(token)
    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": 3600,
        "refresh_token": secrets.token_urlsafe(32),
        "scope": "mcp",
    }
