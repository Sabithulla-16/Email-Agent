"""Google OAuth 2.0 PKCE, Token Refresh, and Session Management Service."""

import base64
import hashlib
import hmac
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import httpx
import structlog
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from groq import AsyncGroq

from app.core.config import Settings, get_settings
from app.core.errors import NotFoundError, UnauthorizedError, ValidationError
from app.core.vault import TokenVault, get_token_vault
from app.db.repos.user_repo import UserRepo
from app.models.db import (
    OAuthStatus,
    OAuthTokenUpsert,
    SessionCreate,
    SessionRow,
    UserCreate,
    UserRow,
    UserUpdate,
)
from app.queue.redis import get_redis

logger = structlog.get_logger("app.services.auth")

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

INITIAL_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]


class AuthService:
    """Handles Google OAuth flows, token encryption, and session lifecycle."""

    def __init__(
        self,
        user_repo: UserRepo,
        settings: Settings | None = None,
        vault: TokenVault | None = None,
    ) -> None:
        self.user_repo = user_repo
        self.settings = settings or get_settings()
        self.vault = vault or get_token_vault()

    # -------------------------------------------------------------------------
    # PKCE & State Generation
    # -------------------------------------------------------------------------
    def generate_pkce_pair(self) -> tuple[str, str]:
        """Generate (code_verifier, code_challenge)."""
        verifier = secrets.token_urlsafe(64)
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        return verifier, challenge

    def create_signed_state(self, client_state: str = "", code_verifier: str = "") -> str:
        """Create HMAC signed state to prevent CSRF and carry encrypted verifier."""
        timestamp = str(int(time.time()))
        nonce = secrets.token_hex(16)
        enc_verifier = ""
        if code_verifier:
            vault_ciphertext, _ = self.vault.encrypt(code_verifier)
            enc_verifier = (
                base64.urlsafe_b64encode(vault_ciphertext.encode("ascii"))
                .decode("ascii")
                .rstrip("=")
            )
        raw = f"{timestamp}:{nonce}:{client_state}:{enc_verifier}"
        secret = self.settings.APP_SECRET_KEY.get_secret_value().encode("utf-8")
        sig = hmac.new(secret, raw.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{raw}:{sig}"

    def verify_signed_state(self, state: str, max_age_seconds: int = 600) -> tuple[str, str | None]:
        """Verify state signature and return (client_state, recovered_code_verifier)."""
        parts = state.split(":")
        if len(parts) == 4:
            timestamp_str, nonce, client_state, received_sig = parts
            enc_verifier = ""
            raw = f"{timestamp_str}:{nonce}:{client_state}"
        elif len(parts) == 5:
            timestamp_str, nonce, client_state, enc_verifier, received_sig = parts
            raw = f"{timestamp_str}:{nonce}:{client_state}:{enc_verifier}"
        else:
            raise ValidationError("Invalid OAuth state parameter structure")

        try:
            ts = int(timestamp_str)
        except ValueError:
            raise ValidationError("Invalid timestamp in OAuth state") from None

        if time.time() - ts > max_age_seconds:
            raise ValidationError("OAuth state has expired. Please try logging in again.")

        secret = self.settings.APP_SECRET_KEY.get_secret_value().encode("utf-8")
        expected_sig = hmac.new(secret, raw.encode("utf-8"), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(received_sig, expected_sig):
            raise ValidationError("Forged or invalid OAuth state parameter")

        recovered_verifier: str | None = None
        if enc_verifier:
            try:
                pad = len(enc_verifier) % 4
                if pad:
                    enc_verifier += "=" * (4 - pad)
                vault_ciphertext = base64.urlsafe_b64decode(enc_verifier.encode("ascii")).decode(
                    "ascii"
                )
                recovered_verifier = self.vault.decrypt(vault_ciphertext)
            except Exception as e:
                logger.warning("failed_to_decrypt_state_verifier", error=str(e))
                recovered_verifier = None

        return client_state, recovered_verifier

    def build_authorization_url(
        self,
        code_challenge: str,
        state: str,
        prompt_consent: bool = False,
        extra_scopes: list[str] | None = None,
    ) -> str:
        """Build Google OAuth 2.0 authorization URL with PKCE."""
        scopes = list(INITIAL_SCOPES)
        if extra_scopes:
            for s in extra_scopes:
                if s not in scopes:
                    scopes.append(s)

        params = {
            "client_id": self.settings.GOOGLE_CLIENT_ID,
            "redirect_uri": self.settings.GOOGLE_REDIRECT_URI,
            "response_type": "code",
            "scope": " ".join(scopes),
            "access_type": "offline",
            "include_granted_scopes": "true",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "prompt": "consent" if prompt_consent else "select_account",
        }

        return f"{GOOGLE_AUTH_URL}?{httpx.QueryParams(params)}"

    # -------------------------------------------------------------------------
    # Token Exchange & User Provisioning
    # -------------------------------------------------------------------------
    async def exchange_code_for_tokens(self, code: str, code_verifier: str) -> dict[str, Any]:
        """Exchange authorization code with Google for tokens."""
        data = {
            "client_id": self.settings.GOOGLE_CLIENT_ID,
            "client_secret": self.settings.GOOGLE_CLIENT_SECRET.get_secret_value(),
            "code": code,
            "code_verifier": code_verifier,
            "grant_type": "authorization_code",
            "redirect_uri": self.settings.GOOGLE_REDIRECT_URI,
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(GOOGLE_TOKEN_URL, data=data)
            if resp.status_code != 200:
                logger.error(
                    "google_token_exchange_failed",
                    status=resp.status_code,
                    body=resp.text,
                )
                raise ValidationError(f"Google token exchange failed: {resp.text}")
            return cast(dict[str, Any], resp.json())

    def verify_google_id_token(self, raw_id_token: str) -> dict[str, Any]:
        """Verify the OpenID Connect ID token signature, issuer, and audience."""
        try:
            # google-auth ID token verification
            payload = id_token.verify_oauth2_token(  # type: ignore[no-untyped-call]
                raw_id_token,
                google_requests.Request(),
                audience=self.settings.GOOGLE_CLIENT_ID,
            )
            return cast(dict[str, Any], payload)
        except Exception as exc:
            logger.warning("google_id_token_verification_failed", error=str(exc))
            raise UnauthorizedError(f"Invalid Google ID token: {exc}") from exc

    async def handle_oauth_callback(
        self,
        code: str,
        code_verifier: str,
        user_agent: str | None = None,
        ip: str | None = None,
    ) -> tuple[UserRow, str, SessionRow]:
        """Complete Google OAuth callback: verify ID token, upsert user, create session."""
        tokens = await self.exchange_code_for_tokens(code, code_verifier)
        id_info = self.verify_google_id_token(tokens["id_token"])

        google_sub = id_info["sub"]
        email = id_info["email"]
        name = id_info.get("name")
        locale = id_info.get("locale", "en")

        # 1. Upsert user by google_sub (NOT by email alone per requirements)
        user = await self.user_repo.upsert_user(
            UserCreate(
                google_sub=google_sub,
                email=email,
                name=name,
                locale=locale,
            )
        )

        # 2. Store encrypted refresh token in vault (if Google returned one)
        refresh_token = tokens.get("refresh_token")
        granted_scopes = tokens.get("scope", "").split()
        if refresh_token:
            enc_refresh, key_ver = self.vault.encrypt(refresh_token)
            await self.user_repo.upsert_oauth_token(
                OAuthTokenUpsert(
                    user_id=str(user.id),
                    refresh_token_enc=enc_refresh,
                    key_version=key_ver,
                    scopes=granted_scopes,
                    status=OAuthStatus.ACTIVE.value,
                    last_refreshed_at=datetime.now(UTC).isoformat(),
                )
            )

        # 3. Cache short-lived access token in Redis
        access_token = tokens.get("access_token")
        expires_in = tokens.get("expires_in", 3600)
        if access_token:
            await self._cache_access_token(user.id, access_token, int(expires_in))

        # 4. Create browser session
        raw_session_token, session = await self.create_session(
            user.id, user_agent=user_agent, ip=ip
        )

        return user, raw_session_token, session

    # -------------------------------------------------------------------------
    # Access Token Cache & Refresh Logic
    # -------------------------------------------------------------------------
    async def _cache_access_token(self, user_id: UUID | str, token: str, expires_in: int) -> None:
        try:
            redis = get_redis()
            if not redis:
                return
            ttl = max(60, expires_in - 60)
            await redis.setex(f"gmail_access_token:{user_id}", ttl, token)
        except Exception as exc:
            logger.warning("failed_to_cache_access_token_in_redis", error=str(exc))

    async def get_valid_access_token(self, user_id: UUID | str) -> str:
        """Retrieve valid access token from Redis cache or refresh it using the vault."""
        # 1. Check Redis cache
        try:
            redis = get_redis()
            if redis:
                cached = await redis.get(f"gmail_access_token:{user_id}")
                if cached:
                    return cached
        except Exception:
            pass

        # 2. Load stored refresh token
        oauth_row = await self.user_repo.get_oauth_token(user_id)
        if not oauth_row:
            raise UnauthorizedError(
                "No OAuth credentials found for user. Please sign in with Google."
            )

        if oauth_row.status == OAuthStatus.NEEDS_REAUTH:
            raise UnauthorizedError(
                "User credentials expired or revoked. Re-authentication required."
            )

        # 3. Decrypt refresh token
        refresh_token = self.vault.decrypt(oauth_row.refresh_token_enc, oauth_row.key_version)

        # 4. Exchange with Google
        data = {
            "client_id": self.settings.GOOGLE_CLIENT_ID,
            "client_secret": self.settings.GOOGLE_CLIENT_SECRET.get_secret_value(),
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(GOOGLE_TOKEN_URL, data=data)
            if resp.status_code == 200:
                tokens = resp.json()
                new_access_token = tokens["access_token"]
                expires_in = tokens.get("expires_in", 3600)
                await self._cache_access_token(user_id, new_access_token, int(expires_in))
                await self.user_repo.update_token_status(user_id, OAuthStatus.ACTIVE.value)
                return new_access_token

            # Handle invalid_grant per requirement
            body = resp.text
            if "invalid_grant" in body:
                logger.warning("google_refresh_token_invalid_grant", user_id=str(user_id))
                await self.user_repo.update_token_status(user_id, OAuthStatus.NEEDS_REAUTH.value)
                raise UnauthorizedError(
                    "Google refresh token invalid or revoked. Please sign in again."
                )

            logger.error("google_token_refresh_failed", status=resp.status_code, body=body)
            raise UnauthorizedError(f"Failed to refresh access token: {body}")

    # -------------------------------------------------------------------------
    # Sessions & CSRF
    # -------------------------------------------------------------------------
    @staticmethod
    def hash_token(raw_token: str) -> str:
        """Deterministic SHA-256 hash for session tokens."""
        return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    async def create_session(
        self,
        user_id: UUID | str,
        user_agent: str | None = None,
        ip: str | None = None,
        duration_days: int = 14,
    ) -> tuple[str, SessionRow]:
        """Generate high-entropy random session token and insert hashed record."""
        raw_token = secrets.token_urlsafe(32)
        token_hash = self.hash_token(raw_token)
        expires_at = datetime.now(UTC) + timedelta(days=duration_days)

        session = await self.user_repo.create_session(
            SessionCreate(
                user_id=str(user_id),
                token_hash=token_hash,
                expires_at=expires_at.isoformat(),
                user_agent=user_agent,
                ip=ip,
            )
        )
        return raw_token, session

    async def authenticate_session(self, raw_session_token: str) -> UserRow:
        """Verify session token against DB and load user."""
        token_hash = self.hash_token(raw_session_token)
        session = await self.user_repo.get_valid_session(token_hash)
        if not session:
            raise UnauthorizedError("Invalid or expired session. Please log in.")

        user = await self.user_repo.get_by_id(session.user_id)
        if not user or user.deleted_at:
            raise UnauthorizedError("User account no longer active")

        return user

    async def logout_session(self, raw_session_token: str) -> None:
        """Revoke active session token."""
        token_hash = self.hash_token(raw_session_token)
        await self.user_repo.revoke_session(token_hash)

    def generate_csrf_token(self) -> str:
        """Generate a random cryptographic CSRF token for double-submit cookie."""
        return secrets.token_hex(32)

    # -------------------------------------------------------------------------
    # User-Supplied Groq API Key (BYOK) Management
    # -------------------------------------------------------------------------
    async def get_user_groq_api_key(self, user_id: UUID | str) -> str | None:
        """Retrieve and decrypt user's personal Groq API key if configured."""
        user = await self.user_repo.get_by_id(user_id)
        if not user or not user.prefs:
            return None
        enc_key = user.prefs.get("groq_api_key_encrypted")
        if not enc_key:
            return None
        try:
            return self.vault.decrypt(enc_key)
        except Exception as exc:
            logger.warning("failed_to_decrypt_user_groq_key", user_id=str(user_id), error=str(exc))
            return None

    async def get_user_groq_key_status(self, user_id: UUID | str) -> dict[str, Any]:
        """Return masked key info and verification status for UI / Telegram."""
        user = await self.user_repo.get_by_id(user_id)
        if not user or not user.prefs:
            return {"has_key": False, "masked_key": None}
        enc_key = user.prefs.get("groq_api_key_encrypted")
        masked = user.prefs.get("groq_api_key_masked")
        return {
            "has_key": bool(enc_key),
            "masked_key": masked if enc_key else None,
        }

    async def set_user_groq_api_key(self, user_id: UUID | str, raw_api_key: str) -> str:
        """Validate, encrypt with AES-256-GCM, and save the user's Groq API key."""
        cleaned = raw_api_key.strip()
        if not cleaned.startswith("gsk_") or len(cleaned) < 20:
            raise ValidationError("Invalid Groq API key format. Key should start with 'gsk_'")

        # Validate with Groq API
        try:
            test_client = AsyncGroq(api_key=cleaned, max_retries=0, timeout=10.0)
            await test_client.models.list()
        except Exception as exc:
            logger.warning("groq_api_key_validation_failed", error=str(exc))
            raise ValidationError(f"Invalid Groq API key: {exc}") from exc

        user = await self.user_repo.get_by_id(user_id)
        if not user:
            raise NotFoundError(f"User {user_id} not found")

        enc_key, _ = self.vault.encrypt(cleaned)
        masked_key = f"{cleaned[:7]}...{cleaned[-4:]}"
        new_prefs = dict(user.prefs)
        new_prefs["groq_api_key_encrypted"] = enc_key
        new_prefs["groq_api_key_masked"] = masked_key

        await self.user_repo.update_user(user.id, UserUpdate(prefs=new_prefs))
        logger.info(
            "user_groq_key_configured_successfully",
            user_id=str(user.id),
            masked=masked_key,
        )
        return masked_key

    async def remove_user_groq_api_key(self, user_id: UUID | str) -> bool:
        """Remove user's stored Groq API key."""
        user = await self.user_repo.get_by_id(user_id)
        if not user:
            return False
        new_prefs = dict(user.prefs)
        new_prefs.pop("groq_api_key_encrypted", None)
        new_prefs.pop("groq_api_key_masked", None)
        await self.user_repo.update_user(user.id, UserUpdate(prefs=new_prefs))
        logger.info("user_groq_key_removed", user_id=str(user.id))
        return True
