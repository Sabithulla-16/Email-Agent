"""Authentication and User Profile Endpoints."""

from typing import Annotated, Any

from fastapi import APIRouter, Cookie, Depends, Request, Response, status
from fastapi.responses import RedirectResponse

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
    client_state = f"tg_{chat_id}" if chat_id else "web"
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
            tg_chat_id = int(client_state[3:])
            telegram_repo = TelegramRepo(get_supabase())
            await telegram_repo.link_chat(user_id=user.id, chat_id=tg_chat_id)
            tg_client = TelegramClient(telegram_repo=telegram_repo, settings=settings)
            await tg_client.send_message(
                chat_id=tg_chat_id,
                text=(
                    f"✦ <b>Gmail Connected!</b>\n\n"
                    f"Account: <code>{user.email}</code>\n"
                    f"User: <b>{user.name}</b>\n\n"
                    f"You're all set! Use /pending to see any email drafts awaiting approval."
                ),
            )
            logger.info("telegram_chat_linked_via_oauth", user_id=str(user.id), chat_id=tg_chat_id)
        except Exception as exc:
            logger.warning("failed_to_link_telegram_chat", error=str(exc))

    csrf_token = auth_service.generate_csrf_token()

    # Deliver HttpOnly session cookie
    response.set_cookie(
        key="session_token",
        value=raw_session_token,
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
        max_age=14 * 86400,
        path="/",
    )
    # Deliver double-submit CSRF cookie (readable by frontend client)
    response.set_cookie(
        key="csrf_token",
        value=csrf_token,
        httponly=False,
        secure=settings.is_production,
        samesite="lax",
        max_age=14 * 86400,
        path="/",
    )
    # Clear temporary oauth_verifier cookie
    response.delete_cookie("oauth_verifier", path="/")

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
