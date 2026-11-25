"""Authentication and User Profile Endpoints."""

import html
import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Cookie, Depends, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from app.api.deps import get_auth_service, get_current_user, get_user_repo, verify_csrf
from app.core.config import Settings, get_settings
from app.core.errors import ValidationError
from app.core.logging import get_logger
from app.db.client import get_supabase
from app.db.repos.telegram_repo import TelegramRepo
from app.db.repos.user_repo import UserRepo
from app.models.db import UserRow, UserUpdate
from app.services.auth_service import AuthService
from app.services.telegram_client import TelegramClient
from app.services.telegram_service import get_start_message_id

logger = get_logger("app.api.v1.auth")

router = APIRouter(tags=["Authentication & User"])


@router.get("/auth/google")
@router.get("/auth/google/start")
async def google_auth_start(
    response: Response,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    settings: Annotated[Settings, Depends(get_settings)],
    redirect: bool = False,
    chat_id: int | None = None,
) -> Any:
    """Initiate Google OAuth 2.0 PKCE flow (supports Telegram bot linking via chat_id)."""
    verifier, challenge = auth_service.generate_pkce_pair()
    msg_id = await get_start_message_id(chat_id) if chat_id else None
    if chat_id and msg_id:
        client_state = f"tg_{chat_id}_{msg_id}"
    elif chat_id:
        client_state = f"tg_{chat_id}"
    else:
        client_state = "web"
    # Embed encrypted verifier in signed state so callback works even if browser drops cookies
    state = auth_service.create_signed_state(code_verifier=verifier, client_state=client_state)
    auth_url = auth_service.build_authorization_url(code_challenge=challenge, state=state)

    # Store code_verifier in short-lived HttpOnly cookie (10 min)
    cookie_kwargs = {
        "httponly": True,
        "secure": settings.is_production,
        "samesite": "lax",
        "max_age": 600,
        "path": "/",
    }

    # If chat_id is present or redirect=True, immediately redirect user to Google consent page
    if redirect or chat_id is not None:
        red_resp = RedirectResponse(url=auth_url, status_code=status.HTTP_302_FOUND)
        red_resp.set_cookie("oauth_verifier", verifier, **cookie_kwargs)  # type: ignore[arg-type]
        return red_resp

    response.set_cookie("oauth_verifier", verifier, **cookie_kwargs)  # type: ignore[arg-type]
    return {
        "authorization_url": auth_url,
        "state": state,
    }


@router.get("/auth/google/callback")
async def google_auth_callback(
    request: Request,
    response: Response,
    code: str,
    state: str,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    settings: Annotated[Settings, Depends(get_settings)],
    oauth_verifier: Annotated[str | None, Cookie()] = None,
) -> Any:
    """Complete Google OAuth callback, establish session, and link Telegram if requested."""
    # Validate signed HMAC state and recover verifier fallback if cookie is missing
    client_state, state_verifier = auth_service.verify_signed_state(state)
    verifier_to_use = oauth_verifier or state_verifier
    if not verifier_to_use:
        raise ValidationError("Missing OAuth PKCE verifier. Flow may have expired.")

    user_agent = request.headers.get("user-agent")
    ip = request.client.host if request.client else None

    user, raw_session_token, _ = await auth_service.handle_oauth_callback(
        code=code,
        code_verifier=verifier_to_use,
        user_agent=user_agent,
        ip=ip,
    )

    # If linked via Telegram chat_id
    if client_state and client_state.startswith("tg_"):
        try:
            raw_parts = client_state[3:].split("_")
            tg_chat_id = int(raw_parts[0])
            msg_to_delete = (
                int(raw_parts[1]) if len(raw_parts) > 1 and raw_parts[1].isdigit() else None
            )
            if not msg_to_delete:
                msg_to_delete = await get_start_message_id(tg_chat_id)

            telegram_repo = TelegramRepo(get_supabase())
            existing_link = await telegram_repo.get_link_by_chat_id(tg_chat_id)
            if (
                existing_link
                and existing_link.is_active
                and str(existing_link.user_id) != str(user.id)
            ):
                logger.warning(
                    "telegram_chat_hijack_prevented",
                    chat_id=tg_chat_id,
                    existing_user=str(existing_link.user_id),
                    attempted_user=str(user.id),
                )
            else:
                await telegram_repo.link_chat(user_id=user.id, chat_id=tg_chat_id)
                tg_client = TelegramClient(telegram_repo=telegram_repo, settings=settings)

            # Delete the previous "Connect Gmail" message to keep chat pristine
            if msg_to_delete:
                await tg_client.delete_message(chat_id=tg_chat_id, message_id=msg_to_delete)

            has_groq_key = bool(user.prefs and user.prefs.get("groq_api_key_encrypted"))
            if not has_groq_key:
                welcome_text = (
                    f"✦ <b>Gmail Connected Successfully!</b>\n\n"
                    f"Account: <code>{user.email}</code>\n"
                    f"User: <b>{user.name}</b>\n\n"
                    f"▸ <b>Final Step: Set your Groq API Key</b>\n"
                    f"To enable AI email triage, summaries, and reply drafts on your own quota, "
                    f"enter your Groq API key:\n\n"
                    f"<code>/setkey gsk_your_key_here</code>\n\n"
                    f"<i>(Don't have one? Get a free key at https://console.groq.com/keys)</i>\n"
                    f"<i>Your key is encrypted with AES-256 and your message is deleted "
                    f"immediately for security.</i>"
                )
            else:
                welcome_text = (
                    f"✦ <b>Gmail Connected!</b>\n\n"
                    f"Account: <code>{user.email}</code>\n"
                    f"User: <b>{user.name}</b>\n\n"
                    f"You're all set! Use /pending to see any email drafts awaiting approval."
                )

            await tg_client.send_message(
                chat_id=tg_chat_id,
                text=welcome_text,
            )
            logger.info("telegram_chat_linked_via_oauth", user_id=str(user.id), chat_id=tg_chat_id)
        except Exception as exc:
            logger.warning("failed_to_link_telegram_chat", error=str(exc))

    csrf_token = auth_service.generate_csrf_token()

    def _set_auth_cookies(resp: Response) -> None:
        resp.set_cookie(
            key="session_token",
            value=raw_session_token,
            httponly=True,
            secure=settings.is_production,
            samesite="lax",
            max_age=14 * 86400,
            path="/",
        )
        resp.set_cookie(
            key="csrf_token",
            value=csrf_token,
            httponly=False,
            secure=settings.is_production,
            samesite="lax",
            max_age=14 * 86400,
            path="/",
        )
        resp.delete_cookie("oauth_verifier", path="/")

    # 1. Telegram OAuth flow: Return hardened, Impeccable-crafted HTML success screen
    if client_state and client_state.startswith("tg_"):
        esc_email = html.escape(user.email)
        csp_nonce = secrets.token_urlsafe(16)
        tg_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Gmail Connected — Email Agent</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <style>
    :root {{
      --bg: #0b0f17;
      --panel: #121826;
      --border: rgba(148, 163, 184, 0.18);
      --fg: #f8fafc;
      --muted: #94a3b8;
      --accent: #38bdf8;
      --btn-bg: #0284c7;
      --btn-hover: #0369a1;
      --ok: #34d399;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    ::selection {{ background: rgba(56, 189, 248, 0.25); color: var(--fg); }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--fg);
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 1.5rem;
      -webkit-font-smoothing: antialiased;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 2.25rem 2rem;
      max-width: 420px;
      width: 100%;
      text-align: center;
      animation: panelIn 320ms cubic-bezier(0.16, 1, 0.3, 1) both;
    }}
    @keyframes panelIn {{
      from {{ opacity: 0; transform: translateY(8px); }}
      to {{ opacity: 1; transform: translateY(0); }}
    }}
    @media (prefers-reduced-motion: reduce) {{
      .panel {{ animation: none; }}
    }}
    .mark {{
      width: 48px;
      height: 48px;
      margin: 0 auto 1.25rem;
      border-radius: 12px;
      background: rgba(52, 211, 153, 0.1);
      border: 1px solid rgba(52, 211, 153, 0.28);
      display: flex;
      align-items: center;
      justify-content: center;
      color: var(--ok);
    }}
    h1 {{
      font-size: 1.375rem;
      font-weight: 650;
      letter-spacing: -0.02em;
      color: var(--fg);
      margin-bottom: 0.5rem;
    }}
    .lead {{
      color: var(--muted);
      font-size: 0.9375rem;
      line-height: 1.55;
      margin-bottom: 1.5rem;
    }}
    .account-row {{
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 0.5rem;
      background: var(--bg);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 0.75rem 1rem;
      color: var(--fg);
      font-size: 0.875rem;
      font-weight: 500;
      margin-bottom: 1rem;
      word-break: break-all;
    }}
    .account-row svg {{
      flex-shrink: 0;
      color: var(--accent);
    }}
    .trust-note {{
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 0.4rem;
      font-size: 0.75rem;
      color: var(--muted);
      margin-bottom: 1.5rem;
    }}
    .trust-note svg {{
      color: var(--ok);
      flex-shrink: 0;
    }}
    .btn {{
      display: block;
      width: 100%;
      background: var(--btn-bg);
      color: #ffffff;
      font-weight: 600;
      padding: 0.85rem 1.25rem;
      border: 1px solid rgba(255, 255, 255, 0.08);
      cursor: pointer;
      border-radius: 10px;
      font-size: 0.9375rem;
      transition: background 150ms ease;
    }}
    .btn:hover {{ background: var(--btn-hover); }}
    .btn:focus-visible {{
      outline: 2px solid var(--accent);
      outline-offset: 2px;
    }}
    .hint {{
      margin-top: 1rem;
      font-size: 0.8125rem;
      color: var(--muted);
      font-variant-numeric: tabular-nums;
    }}
    .countdown {{
      font-weight: 600;
      color: var(--fg);
    }}
  </style>
</head>
<body>
  <main class="panel">
    <div class="mark" aria-hidden="true">
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor"
           stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M20 6 9 17l-5-5"/>
      </svg>
    </div>
    <h1>Gmail Connected</h1>
    <p class="lead">Your Google account is verified and linked to your Telegram assistant.</p>
    <div class="account-row">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
           stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <rect width="20" height="16" x="2" y="4" rx="2"/>
        <path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"/>
      </svg>
      <span>{esc_email}</span>
    </div>
    <div class="trust-note">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
           stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10"/>
      </svg>
      <span>AES-256-GCM encrypted &middot; OAuth PKCE verified</span>
    </div>
    <button type="button" class="btn" id="returnBtn">Return to Chat</button>
    <p class="hint" id="status-hint">
      Closing window in <span class="countdown" id="countdown">3</span>s
    </p>
  </main>
  <script nonce="{csp_nonce}">
    try {{
      if (window.history && window.history.replaceState) {{
        window.history.replaceState(null, "", window.location.pathname);
      }}
    }} catch (e) {{}}

    function closeTab() {{
      try {{
        if (window.Telegram && window.Telegram.WebApp) {{
          window.Telegram.WebApp.ready();
          window.Telegram.WebApp.close();
        }}
      }} catch (e) {{}}
      try {{
        window.close();
      }} catch (e) {{}}
      var h = document.getElementById("status-hint");
      if (h) {{
        h.textContent = "All set. Close this window to return to Telegram.";
      }}
    }}

    var btn = document.getElementById("returnBtn");
    if (btn) btn.addEventListener("click", closeTab);

    var seconds = 3;
    var timer = setInterval(function() {{
      seconds--;
      var el = document.getElementById("countdown");
      if (el) el.textContent = String(seconds);
      if (seconds <= 0) {{
        clearInterval(timer);
        closeTab();
      }}
    }}, 1000);
  </script>
</body>
</html>"""
        tg_resp = HTMLResponse(content=tg_html, status_code=status.HTTP_200_OK)
        tg_resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
        tg_resp.headers["Pragma"] = "no-cache"
        tg_resp.headers["Referrer-Policy"] = "no-referrer"
        tg_resp.headers["Content-Security-Policy"] = (
            f"default-src 'none'; script-src 'nonce-{csp_nonce}' https://telegram.org; "
            "style-src 'unsafe-inline'; object-src 'none'; base-uri 'none'; frame-ancestors 'none';"
        )
        _set_auth_cookies(tg_resp)
        return tg_resp

    # 2. Browser Web OAuth flow: redirect back to frontend login/app screen
    accept_header = request.headers.get("accept", "")
    if "text/html" in accept_header:
        frontend_url = settings.FRONTEND_BASE_URL.rstrip("/")
        red_resp = RedirectResponse(url=f"{frontend_url}/", status_code=status.HTTP_302_FOUND)
        _set_auth_cookies(red_resp)
        return red_resp

    # 3. API / Automated tests: return JSON payload
    _set_auth_cookies(response)
    return {
        "status": "authenticated",
        "user": {
            "id": str(user.id),
            "email": user.email,
            "name": user.name,
            "locale": user.locale,
            "timezone": user.timezone,
        },
        "session_token": raw_session_token,
        "csrf_token": csrf_token,
    }


def _sanitize_prefs_for_client(prefs: dict[str, Any] | None) -> dict[str, Any]:
    """Strip internal/encrypted secrets from user.prefs before returning to API clients."""
    if not prefs:
        return {}
    return {k: v for k, v in prefs.items() if k != "groq_api_key_encrypted"}


class UserPrefsPayload(BaseModel):
    tone: str | None = Field(default=None, max_length=32)
    signature: str | None = Field(default=None, max_length=1000)
    language: str | None = Field(default=None, max_length=16)
    voice_briefing: bool | None = None
    voice_name: str | None = Field(default=None, max_length=64)


class UserPreferencesUpdate(BaseModel):
    """Strict allowlist of client-editable profile & preference fields."""

    name: str | None = Field(default=None, max_length=120)
    timezone: str | None = Field(default=None, max_length=64)
    digest_time: str | None = Field(default=None, max_length=16)
    locale: str | None = Field(default=None, max_length=16)
    prefs: UserPrefsPayload | None = None


@router.post("/auth/logout", dependencies=[Depends(verify_csrf)])
async def logout(
    response: Response,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    session_token: Annotated[str | None, Cookie()] = None,
) -> dict[str, str]:
    """Revoke active user session and clear session cookies."""
    if session_token:
        await auth_service.logout_session(session_token)

    response.delete_cookie("session_token", path="/")
    response.delete_cookie("csrf_token", path="/")
    return {"status": "logged_out"}


@router.get("/me")
async def get_current_user_profile(
    current_user: Annotated[UserRow, Depends(get_current_user)],
) -> dict[str, Any]:
    """Get current authenticated user profile and preferences."""
    return {
        "id": str(current_user.id),
        "email": current_user.email,
        "name": current_user.name,
        "timezone": current_user.timezone,
        "digest_time": str(current_user.digest_time),
        "locale": current_user.locale,
        "prefs": _sanitize_prefs_for_client(current_user.prefs),
        "feature_flags": current_user.feature_flags,
        "next_digest_at": (
            current_user.next_digest_at.isoformat() if current_user.next_digest_at else None
        ),
        "created_at": current_user.created_at.isoformat(),
    }


@router.patch("/me/preferences", dependencies=[Depends(verify_csrf)])
async def update_user_preferences(
    update_data: UserPreferencesUpdate,
    current_user: Annotated[UserRow, Depends(get_current_user)],
    user_repo: Annotated[UserRepo, Depends(get_user_repo)],
) -> dict[str, Any]:
    """Update user preferences (tone, signature, timezone, digest time, etc.)."""
    merged_prefs: dict[str, Any] | None = None
    if update_data.prefs is not None:
        merged_prefs = dict(current_user.prefs or {})
        merged_prefs.update(update_data.prefs.model_dump(exclude_none=True))

    safe_update = UserUpdate(
        name=update_data.name,
        timezone=update_data.timezone,
        digest_time=update_data.digest_time,
        locale=update_data.locale,
        prefs=merged_prefs,
    )
    updated_user = await user_repo.update_user(current_user.id, safe_update)
    return {
        "status": "updated",
        "prefs": _sanitize_prefs_for_client(updated_user.prefs),
        "timezone": updated_user.timezone,
        "digest_time": str(updated_user.digest_time),
    }


class GroqKeyPayload(BaseModel):
    api_key: str = Field(..., min_length=20, max_length=256)


@router.get("/me/groq-key")
async def get_groq_key_status(
    current_user: Annotated[UserRow, Depends(get_current_user)],
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> dict[str, Any]:
    """Check whether the current user has configured a personal Groq API key."""
    return await auth_service.get_user_groq_key_status(current_user.id)


@router.post("/me/groq-key", dependencies=[Depends(verify_csrf)])
async def set_groq_key(
    payload: GroqKeyPayload,
    current_user: Annotated[UserRow, Depends(get_current_user)],
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> dict[str, Any]:
    """Validate, encrypt with AES-256-GCM, and save the user's Groq API key."""
    masked = await auth_service.set_user_groq_api_key(current_user.id, payload.api_key)
    return {
        "status": "configured",
        "masked_key": masked,
    }


@router.delete("/me/groq-key", dependencies=[Depends(verify_csrf)])
async def delete_groq_key(
    current_user: Annotated[UserRow, Depends(get_current_user)],
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> dict[str, Any]:
    """Remove user's stored personal Groq API key."""
    await auth_service.remove_user_groq_api_key(current_user.id)
    return {"status": "removed"}


@router.delete("/me", dependencies=[Depends(verify_csrf)])
async def delete_me(
    response: Response,
    current_user: Annotated[UserRow, Depends(get_current_user)],
    user_repo: Annotated[UserRepo, Depends(get_user_repo)],
) -> dict[str, str]:
    """Complete data deletion ('Delete my data'): deletes all user data and audit logs deletion."""
    await user_repo.delete_user_data(current_user.id)
    response.delete_cookie("session_token", path="/")
    response.delete_cookie("csrf_token", path="/")
    return {"status": "user_data_deleted"}
