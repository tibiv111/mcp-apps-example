"""
Mock OAuth 2.1 with Dynamic Client Registration.

Claude's connector flow expects all four endpoints (discovery, register,
authorize, token). This implementation auto-accepts everything — it exists
so the handshake completes, not to actually authenticate anybody.
"""

from __future__ import annotations

import secrets
import time
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

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


def _esc(value: str) -> str:
    """Minimal HTML attribute escape for OAuth params re-embedded in the form."""
    return (
        value.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


@router.get("/oauth/authorize")
async def oauth_authorize_form(
    response_type: str = "code",
    client_id: str = "",
    redirect_uri: str = "",
    state: str = "",
    code_challenge: str = "",
    code_challenge_method: str = "",
    scope: str = "",
) -> HTMLResponse:
    """
    Render the mock sign-in page. Claude opens this in a real browser window,
    so we render a form and let the user submit it — the POST handler does
    the redirect back with an auth code.

    The OAuth params are echoed back as hidden inputs so the POST handler can
    complete the redirect without needing session state.
    """
    fields = {
        "response_type": response_type,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
        "scope": scope,
    }
    hidden = "\n".join(
        f'        <input type="hidden" name="{k}" value="{_esc(v)}" />'
        for k, v in fields.items()
    )
    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>NAV AI · Sign in</title>
  <style>
    :root {{
      --bg:#0a0d12; --bg-elev:#11151c; --border:#1f2630; --border-2:#2a3340;
      --text:#e8e6e0; --text-2:#8a8f9c; --text-3:#4d535f;
      --accent:#d4a85a; --accent-d:#8a7547;
      --font-display:ui-serif,'Iowan Old Style','Apple Garamond','Hoefler Text',Baskerville,'Palatino Linotype',Georgia,'Times New Roman',serif;
      --font-sans:ui-sans-serif,-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,sans-serif;
      --font-mono:ui-monospace,'SF Mono','JetBrains Mono','Cascadia Mono',Menlo,Consolas,'Courier New',monospace;
    }}
    * {{ box-sizing:border-box; }}
    html,body {{ margin:0; padding:0; background:var(--bg); color:var(--text);
                 font-family:var(--font-sans); font-size:13px; line-height:1.55;
                 -webkit-font-smoothing:antialiased; min-height:100vh; }}
    body {{
      display:grid; place-items:center; padding:24px;
      background-image:
        radial-gradient(ellipse 80% 50% at 50% -10%, rgba(212,168,90,0.06), transparent 60%),
        url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='160' height='160'><filter id='n'><feTurbulence baseFrequency='0.9' numOctaves='2' stitchTiles='stitch'/><feColorMatrix values='0 0 0 0 0.05  0 0 0 0 0.05  0 0 0 0 0.07  0 0 0 0.5 0'/></filter><rect width='100%25' height='100%25' filter='url(%23n)' opacity='0.5'/></svg>");
    }}
    .card {{
      width:100%; max-width:380px;
      background:var(--bg-elev); border:1px solid var(--border);
      padding:36px 36px 28px;
    }}
    .brand {{ display:flex; align-items:baseline; gap:10px; margin-bottom:28px; }}
    .brand .mark {{
      font-family:var(--font-display); font-style:italic; font-size:28px;
      letter-spacing:0.01em; line-height:1;
    }}
    .brand .mark::after {{
      content:"·"; color:var(--accent); margin:0 6px; font-style:normal;
    }}
    .brand .sub {{
      font-family:var(--font-mono); font-size:10px; letter-spacing:0.18em;
      color:var(--text-3); text-transform:uppercase;
    }}
    h1 {{
      font-family:var(--font-display); font-style:italic; font-weight:400;
      font-size:26px; line-height:1.1; margin:0 0 6px;
    }}
    .lede {{ color:var(--text-2); margin:0 0 24px; font-size:13px; }}
    label {{
      display:block; font-family:var(--font-mono); font-size:10px;
      letter-spacing:0.14em; text-transform:uppercase; color:var(--text-3);
      margin-bottom:6px;
    }}
    input[type=email], input[type=password] {{
      width:100%; padding:10px 12px; background:#0d1117;
      border:1px solid var(--border-2); color:var(--text);
      font-family:var(--font-sans); font-size:13px;
      outline:none; transition:border-color 120ms;
    }}
    input[type=email]:focus, input[type=password]:focus {{
      border-color:var(--accent-d);
    }}
    .field {{ margin-bottom:16px; }}
    button.primary {{
      width:100%; padding:11px 14px; margin-top:8px;
      background:var(--accent); color:#1a1308; border:none;
      font-family:var(--font-mono); font-size:11px; letter-spacing:0.16em;
      text-transform:uppercase; cursor:pointer;
      transition:background 120ms;
    }}
    button.primary:hover {{ background:#e0b768; }}
    .footnote {{
      margin-top:22px; padding-top:18px; border-top:1px dashed var(--border);
      font-family:var(--font-mono); font-size:10px; letter-spacing:0.1em;
      color:var(--text-3); text-transform:uppercase; text-align:center;
    }}
    .footnote .accent {{ color:var(--accent); }}
  </style>
</head>
<body>
  <form class="card" method="POST" action="/oauth/authorize">
    <div class="brand">
      <span class="mark">NAV<i style="font-style:normal">AI</i></span>
      <span class="sub">SIGN IN</span>
    </div>
    <h1>Welcome back.</h1>
    <p class="lede">Sign in to continue to NAV AI.</p>

    <div class="field">
      <label for="email">Email</label>
      <input type="email" id="email" name="email" value="{_esc(DEMO_USER)}" autocomplete="username" />
    </div>
    <div class="field">
      <label for="password">Password</label>
      <input type="password" id="password" name="password" value="demo" autocomplete="current-password" />
    </div>

{hidden}

    <button type="submit" class="primary">Sign in →</button>

    <div class="footnote">
      <span class="accent">●</span> Mock auth · accepts any credentials
    </div>
  </form>
</body>
</html>"""
    return HTMLResponse(page)


@router.post("/oauth/authorize")
async def oauth_authorize_submit(
    email: str = Form(""),
    password: str = Form(""),
    response_type: str = Form("code"),
    client_id: str = Form(""),
    redirect_uri: str = Form(""),
    state: str = Form(""),
    code_challenge: str = Form(""),
    code_challenge_method: str = Form(""),
    scope: str = Form(""),
) -> RedirectResponse:
    """
    Accept any submitted credentials and complete the OAuth redirect with a
    fresh auth code. Email/password are intentionally unused — this is a mock.
    """
    _ = (email, password, response_type, client_id, code_challenge, code_challenge_method, scope)
    auth_code = secrets.token_urlsafe(16)
    safe_redirect = redirect_uri or "/"
    sep = "&" if "?" in safe_redirect else "?"
    final_url = f"{safe_redirect}{sep}code={auth_code}&state={state}"
    return RedirectResponse(url=final_url, status_code=302)


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
