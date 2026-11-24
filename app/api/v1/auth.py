"""Authentication and User Profile Endpoints."""

import html
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
                    f"🔑 <b>Final Step: Set your Groq API Key</b>\n"
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

    # 1. Telegram OAuth flow: Return HTML with 3..2..1 countdown and close the in-app tab
    if client_state and client_state.startswith("tg_"):
        esc_email = html.escape(user.email)
        tg_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Gmail Connected - Email Agent</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #0f172a;
      color: #f8fafc;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 1.5rem;
    }}
    .card {{
      background: #1e293b;
      border: 1px solid #334155;
      border-radius: 1.25rem;
      padding: 2.5rem 2rem;
      max-width: 440px;
      width: 100%;
      text-align: center;
      box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
    }}
    .icon {{
      font-size: 3.5rem;
      margin-bottom: 1rem;
    }}
    h1 {{
      font-size: 1.5rem;
      font-weight: 700;
      color: #38bdf8;
      margin-bottom: 0.5rem;
    }}
    p {{
      color: #94a3b8;
      font-size: 0.95rem;
      line-height: 1.6;
      margin-bottom: 1.25rem;
    }}
    .email-box {{
      background: #0f172a;
      border: 1px solid #334155;
      border-radius: 0.5rem;
      padding: 0.75rem 1rem;
      color: #f1f5f9;
      font-weight: 600;
      font-size: 0.9rem;
      margin-bottom: 1.5rem;
      word-break: break-all;
    }}
    .btn {{
      display: block;
      width: 100%;
      background: #2563eb;
      color: #ffffff;
      font-weight: 600;
      text-decoration: none;
      padding: 0.9rem 1.5rem;
      border: none;
      cursor: pointer;
      border-radius: 0.75rem;
      font-size: 1rem;
      transition: background 0.2s;
    }}
    .btn:hover {{
      background: #1d4ed8;
    }}
    .hint {{
      margin-top: 1rem;
      font-size: 0.85rem;
      color: #94a3b8;
    }}
    .countdown {{
      font-weight: 700;
      color: #38bdf8;
      font-size: 1.1rem;
    }}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">🎉</div>
    <h1>Gmail Connected!</h1>
    <p>Your Google account has been linked to your Telegram email assistant.</p>
    <div class="email-box">{esc_email}</div>
    <button onclick="closeTab()" class="btn" id="returnBtn">✓ Done — Return to Chat</button>
    <p class="hint" id="status-hint">
      Closing window in <span class="countdown" id="countdown">3</span>s...
    </p>
  </div>
  <script>
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
        h.innerHTML = "You're all set! Tap the <b>✕</b> at top left to return to chat.";
      }}
    }}

    var seconds = 3;
    var timer = setInterval(function() {{
      seconds--;
      var el = document.getElementById("countdown");
      if (el) el.innerText = seconds;
      if (seconds <= 0) {{
        clearInterval(timer);
        closeTab();
      }}
    }}, 1000);
  </script>
</body>
</html>"""
        tg_resp = HTMLResponse(content=tg_html, status_code=status.HTTP_200_OK)
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


@router.post("/auth/logout")
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
        "prefs": current_user.prefs,
        "feature_flags": current_user.feature_flags,
        "next_digest_at": (
            current_user.next_digest_at.isoformat() if current_user.next_digest_at else None
        ),
        "created_at": current_user.created_at.isoformat(),
    }


@router.patch("/me/preferences", dependencies=[Depends(verify_csrf)])
async def update_user_preferences(
    update_data: UserUpdate,
    current_user: Annotated[UserRow, Depends(get_current_user)],
    user_repo: Annotated[UserRepo, Depends(get_user_repo)],
) -> dict[str, Any]:
    """Update user preferences (tone, signature, timezone, digest time, etc.)."""
    updated_user = await user_repo.update_user(current_user.id, update_data)
    return {
        "status": "updated",
        "prefs": updated_user.prefs,
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
