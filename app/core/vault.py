"""Token Vault: AES-256-GCM authenticated encryption for OAuth refresh tokens."""

import base64
import os
from typing import ClassVar

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import Settings, get_settings
from app.core.errors import AppError


class TokenVaultError(AppError):
    """Exception raised for cryptographic or key errors in the token vault."""

    def __init__(self, message: str) -> None:
        super().__init__(message=message, code="TOKEN_VAULT_ERROR", status_code=500)


class TokenVault:
    """Manages AES-256-GCM encryption/decryption of OAuth refresh tokens with key rotation."""

    NONCE_BYTES: ClassVar[int] = 12

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.active_version = self.settings.TOKEN_KEY_VERSION
        self._keys: dict[int, bytes] = {}
        self._load_keys()

    def _load_keys(self) -> None:
        primary_key_raw = self.settings.TOKEN_ENCRYPTION_KEY.get_secret_value().encode("utf-8")
        if len(primary_key_raw) != 32:
            raise TokenVaultError(
                f"TOKEN_ENCRYPTION_KEY must be exactly 32 bytes (got {len(primary_key_raw)})"
            )
        self._keys[self.active_version] = primary_key_raw

        for ver, sec in self.settings.TOKEN_SECONDARY_KEYS.items():
            sec_bytes = sec.get_secret_value().encode("utf-8")
            if len(sec_bytes) != 32:
                raise TokenVaultError(f"Secondary key version {ver} must be exactly 32 bytes")
            self._keys[ver] = sec_bytes

    def encrypt(self, plaintext: str, key_version: int | None = None) -> tuple[str, int]:
        """Encrypt plaintext with AES-256-GCM.

        Returns (base64_encoded_ciphertext, key_version).
        """
        ver = key_version or self.active_version
        key = self._keys.get(ver)
        if not key:
            raise TokenVaultError(f"Encryption key for version {ver} is not loaded")

        nonce = os.urandom(self.NONCE_BYTES)
        aesgcm = AESGCM(key)
        # AESGCM automatically appends 16-byte authentication tag
        ciphertext_and_tag = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
        encrypted_payload = nonce + ciphertext_and_tag
        return base64.b64encode(encrypted_payload).decode("ascii"), ver

    def decrypt(self, ciphertext_b64: str, key_version: int | None = None) -> str:
        """Decrypt base64 payload with specified key version (defaults to active_version)."""
        ver = key_version or self.active_version
        key = self._keys.get(ver)
        if not key:
            raise TokenVaultError(f"Decryption key for version {ver} is not configured")

        try:
            encrypted_payload = base64.b64decode(ciphertext_b64.encode("ascii"))
            if len(encrypted_payload) < self.NONCE_BYTES + 16:
                raise TokenVaultError("Invalid ciphertext length")

            nonce = encrypted_payload[: self.NONCE_BYTES]
            ciphertext = encrypted_payload[self.NONCE_BYTES :]
            aesgcm = AESGCM(key)
            decrypted_bytes = aesgcm.decrypt(nonce, ciphertext, None)
            return decrypted_bytes.decode("utf-8")
        except Exception as exc:
            if isinstance(exc, TokenVaultError):
                raise
            raise TokenVaultError(f"Failed to decrypt token: {exc}") from exc


_vault_instance: TokenVault | None = None


def get_token_vault() -> TokenVault:
    global _vault_instance
    if _vault_instance is None:
        _vault_instance = TokenVault()
    return _vault_instance
