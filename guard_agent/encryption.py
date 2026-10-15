import base64
import json
import os
from datetime import datetime
from typing import Any
from uuid import UUID

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class EncryptionError(Exception):
    """Base exception for encryption-related errors."""

    pass


class EncryptionConfigError(RuntimeError):
    """Raised when encryption initialization fails; plaintext fallback is forbidden."""


def _default_json_handler(obj: Any) -> str:
    """Handle non-serializable objects during JSON encoding."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, UUID):
        return str(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


class PayloadEncryptor:
    """
    Encrypts telemetry payloads using project-specific encryption keys with AES-256-GCM.

    This class provides symmetric encryption using AES-256-GCM (Authenticated
    Encryption with Associated Data) to protect telemetry data in transit
    between the agent and the core backend.

    Security features:
    - 256-bit keys for strong security
    - Authenticated encryption (prevents tampering)
    - Project-specific keys for isolation
    - Automatic JSON serialization
    - Base64 encoding for safe transmission
    - 2-3x faster than CBC mode
    - No padding oracle vulnerabilities

    Example:
        >>> encryptor = PayloadEncryptor(project_key="your_key_here")
        >>> data = {"events": [...], "metrics": [...]}
        >>> encrypted = encryptor.encrypt(data)
        >>> # Send encrypted to core
    """

    NONCE_SIZE = 12
    TAG_SIZE = 16
    KEY_SIZE = 32

    def __init__(self, project_key: str) -> None:
        """
        Initialize the encryptor with a project encryption key.

        Args:
            project_key: Base64-encoded 256-bit key from core backend

        Raises:
            EncryptionError: If the project key is invalid
        """
        if not project_key:
            raise EncryptionError("Project key cannot be empty")

        try:
            padded = project_key.strip() + "=" * (-len(project_key.strip()) % 4)
            key_bytes = base64.urlsafe_b64decode(padded.encode())

            if len(key_bytes) != self.KEY_SIZE:
                raise EncryptionError(
                    f"Invalid key size: {len(key_bytes)} bytes, "
                    f"expected {self.KEY_SIZE}"
                )

            self._cipher = AESGCM(key_bytes)

        except EncryptionError:
            raise
        except Exception as e:
            raise EncryptionError(f"Invalid project key format: {e}") from e

    def encrypt(self, data: dict[str, Any], associated_data: str | None = None) -> str:
        """
        Encrypt a telemetry payload with AES-256-GCM.

        The data is serialized to JSON (with deterministic key ordering),
        encrypted using AES-256-GCM, and base64-encoded for transmission.

        Args:
            data: Dictionary containing events and/or metrics
            associated_data: Optional authenticated data (not encrypted)

        Returns:
            Base64-encoded encrypted payload string

        Raises:
            EncryptionError: If encryption fails

        Example:
            >>> data = {
            ...     "events": [{"type": "rate_limit", "ip": "1.2.3.4"}],
            ...     "metrics": [{"type": "request_count", "value": 100}]
            ... }
            >>> encrypted = encryptor.encrypt(data)
        """
        try:
            json_data = json.dumps(
                data,
                separators=(",", ":"),
                sort_keys=True,
                default=_default_json_handler,
            )

            nonce = os.urandom(self.NONCE_SIZE)
            aad = associated_data.encode() if associated_data else None
            encrypted = self._cipher.encrypt(nonce, json_data.encode(), aad)
            combined = nonce + encrypted

            return base64.urlsafe_b64encode(combined).decode()

        except Exception as e:
            raise EncryptionError(f"Failed to encrypt payload: {e}") from e

    def decrypt(
        self, encrypted_data: str, associated_data: str | None = None
    ) -> dict[str, Any]:
        """
        Decrypt an encrypted payload (primarily for testing).

        In normal operation, only the core backend decrypts payloads.
        This method is provided for testing and validation purposes.

        Args:
            encrypted_data: Base64-encoded encrypted payload
            associated_data: Optional authenticated data

        Returns:
            Decrypted dictionary

        Raises:
            EncryptionError: If decryption fails or data is tampered

        Example:
            >>> decrypted = encryptor.decrypt(encrypted_payload)
        """
        try:
            combined = base64.urlsafe_b64decode(encrypted_data.encode())

            nonce = combined[: self.NONCE_SIZE]
            ciphertext = combined[self.NONCE_SIZE :]

            aad = associated_data.encode() if associated_data else None
            decrypted = self._cipher.decrypt(nonce, ciphertext, aad)

            result: dict[str, Any] = json.loads(decrypted.decode())
            return result

        except Exception as e:
            raise EncryptionError("Invalid or tampered payload") from e

    def verify_key(self) -> bool:
        """
        Verify that the encryption key is valid.

        Performs a quick encryption/decryption round-trip test.

        Returns:
            True if key is valid, False otherwise
        """
        try:
            test_data = {"test": "verification"}
            encrypted = self.encrypt(test_data)
            decrypted = self.decrypt(encrypted)
            return decrypted == test_data
        except EncryptionError:
            return False


def create_encryptor(project_key: str | None) -> PayloadEncryptor | None:
    """
    Factory function to create an encryptor with AES-256-GCM.

    Args:
        project_key: Optional project encryption key (256-bit, base64-encoded)

    Returns:
        PayloadEncryptor instance if key is provided, None otherwise

    Raises:
        EncryptionError: If key is provided but invalid
    """
    if not project_key:
        return None

    return PayloadEncryptor(project_key)
