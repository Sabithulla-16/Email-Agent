"""FastAPI request dependencies: database repositories, auth, CSRF, and user resolution."""

from typing import Annotated

import structlog
from fastapi import Cookie, Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.errors import ForbiddenError, UnauthorizedError
from app.db.client import get_supabase
from app.db.repos.draft_repo import DraftRepo
from app.db.repos.email_repo import EmailRepo
from app.db.repos.rule_repo import RuleRepo
from app.db.repos.telegram_repo import TelegramRepo
from app.db.repos.user_repo import UserRepo
from app.models.db import UserRow
from app.services.auth_service import AuthService

logger = structlog.get_logger("app.api.deps")
bearer_scheme = HTTPBearer(auto_error=False)


def get_user_repo() -> UserRepo:
    return UserRepo(get_supabase())


def get_email_repo() -> EmailRepo:
    return EmailRepo(get_supabase())


def get_draft_repo() -> DraftRepo:
    return DraftRepo(get_supabase())


def get_telegram_repo() -> TelegramRepo:
    return TelegramRepo(get_supabase())


def get_rule_repo() -> RuleRepo:
    return RuleRepo(get_supabase())


def get_auth_service(user_repo: Annotated[UserRepo, Depends(get_user_repo)]) -> AuthService:
    return AuthService(user_repo=user_repo)


async def get_current_user(
    request: Request,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    bearer_creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
    session_token: Annotated[str | None, Cookie()] = None,
    authorization: Annotated[str | None, Header()] = None,
    x_session_token: Annotated[str | None, Header()] = None,
) -> UserRow:
    """Resolve current user from HttpOnly session cookie, Bearer token, or X-Session-Token."""
    raw_token: str | None = session_token or request.cookies.get("session_token") or x_session_token

    if not raw_token and bearer_creds:
        raw_token = bearer_creds.credentials.strip()

    if not raw_token and authorization and authorization.startswith("Bearer "):
        raw_token = authorization.split("Bearer ", 1)[1].strip()

    if not raw_token:
        logger.warning(
            "unauthorized_missing_session",
            path=str(request.url.path),
            cookies=list(request.cookies.keys()),
            has_auth_header=bool(authorization or bearer_creds),
        )
        raise UnauthorizedError("Authentication required. Please sign in.")

    return await auth_service.authenticate_session(raw_token)


def verify_csrf(
    request: Request,
    csrf_token: Annotated[str | None, Cookie()] = None,
    x_csrf_token: Annotated[str | None, Header()] = None,
) -> None:
    """Enforce Double-Submit CSRF cookie pattern on state-changing requests."""
    # Safe methods do not require CSRF check
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return

    # Skip cookie CSRF for requests authenticated via Bearer header or X-Session-Token
    auth_header = request.headers.get("Authorization")
    has_bearer = bool(auth_header and auth_header.startswith("Bearer "))
    if has_bearer or bool(request.headers.get("X-Session-Token")):
        return

    cookie_csrf = csrf_token or request.cookies.get("csrf_token")
    header_csrf = x_csrf_token or request.headers.get("X-CSRF-Token")

    if not cookie_csrf or not header_csrf or cookie_csrf != header_csrf:
        raise ForbiddenError("CSRF validation failed. Missing or invalid X-CSRF-Token header.")
        raise ForbiddenError("CSRF validation failed. Missing or mismatched CSRF token.")
