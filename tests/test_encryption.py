import base64
import json
from datetime import datetime, timezone
from typing import Any

import pytest

from guard_agent.encryption import (
    EncryptionError,
    PayloadEncryptor,
    _default_json_handler,
    create_encryptor,
)


class TestPayloadEncryptor:
    """Test suite for PayloadEncryptor class."""

    @pytest.fixture
    def valid_project_key(self) -> str:
        """Generate a valid 256-bit project key."""
        # Generate 32 bytes (256 bits) for AES-256
        key_bytes = b"0" * 32
        return base64.urlsafe_b64encode(key_bytes).decode()

    @pytest.fixture
    def encryptor(self, valid_project_key: str) -> PayloadEncryptor:
        """Create a PayloadEncryptor instance with valid key."""
        return PayloadEncryptor(valid_project_key)

    def test_init_with_valid_key(self, valid_project_key: str) -> None:
        """Test initialization with a valid key."""
        encryptor = PayloadEncryptor(valid_project_key)
        assert encryptor is not None
        assert encryptor._cipher is not None

    def test_init_with_empty_key(self) -> None:
        """Test that empty key raises EncryptionError."""
        with pytest.raises(EncryptionError, match="Project key cannot be empty"):
            PayloadEncryptor("")

    def test_init_with_invalid_base64(self) -> None:
        """Test that invalid base64 raises EncryptionError."""
        # Base64 decoding actually succeeds, but produces wrong size
        with pytest.raises(EncryptionError, match="Invalid key size"):
            PayloadEncryptor("not-valid-base64!!!")

    def test_init_with_wrong_key_size(self) -> None:
        """Test that wrong key size raises EncryptionError."""
        # 16 bytes instead of 32
        short_key = base64.urlsafe_b64encode(b"0" * 16).decode()
        with pytest.raises(EncryptionError, match="Invalid key size"):
            PayloadEncryptor(short_key)

    def test_encrypt_simple_payload(self, encryptor: PayloadEncryptor) -> None:
        """Test encryption of a simple payload."""
        data = {"test": "value", "number": 42}
        encrypted = encryptor.encrypt(data)

        # Verify it's a valid base64 string
        assert isinstance(encrypted, str)
        decoded = base64.urlsafe_b64decode(encrypted.encode())
        assert len(decoded) > 0

        # Verify nonce is included (first 12 bytes)
        assert len(decoded) >= 12

    def test_decrypt_simple_payload(self, encryptor: PayloadEncryptor) -> None:
        """Test decryption of a simple payload."""
        original_data = {"test": "value", "number": 42}
        encrypted = encryptor.encrypt(original_data)
        decrypted = encryptor.decrypt(encrypted)

        assert decrypted == original_data

    def test_encrypt_decrypt_round_trip(self, encryptor: PayloadEncryptor) -> None:
        """Test full encryption/decryption round trip."""
        test_cases: list[dict[str, Any]] = [
            {"events": [], "metrics": []},
            {
                "events": [
                    {"type": "rate_limit", "ip": "1.2.3.4", "timestamp": "2024-01-01"}
                ]
            },
            {"metrics": [{"type": "request_count", "value": 100}]},
            {
                "events": [{"type": "sql_injection", "payload": "' OR 1=1 --"}],
                "metrics": [{"cpu": 50.5, "memory": 1024}],
            },
        ]

        for data in test_cases:
            encrypted = encryptor.encrypt(data)
            decrypted = encryptor.decrypt(encrypted)
            assert decrypted == data

    def test_encrypt_with_associated_data(self, encryptor: PayloadEncryptor) -> None:
        """Test encryption with associated authenticated data."""
        data = {"test": "value"}
        aad = "project-123"

        encrypted = encryptor.encrypt(data, associated_data=aad)
        decrypted = encryptor.decrypt(encrypted, associated_data=aad)

        assert decrypted == data

    def test_decrypt_with_wrong_associated_data(
        self, encryptor: PayloadEncryptor
    ) -> None:
        """Test that wrong AAD causes decryption failure."""
        data = {"test": "value"}
        encrypted = encryptor.encrypt(data, associated_data="correct-aad")

        with pytest.raises(EncryptionError, match="Invalid or tampered payload"):
            encryptor.decrypt(encrypted, associated_data="wrong-aad")

    def test_decrypt_tampered_payload(self, encryptor: PayloadEncryptor) -> None:
        """Test that tampered payload is detected."""
        data = {"test": "value"}
        encrypted = encryptor.encrypt(data)

        # Tamper with the encrypted data
        encrypted_bytes = base64.urlsafe_b64decode(encrypted.encode())
        tampered = encrypted_bytes[:-1] + bytes([encrypted_bytes[-1] ^ 0xFF])
        tampered_str = base64.urlsafe_b64encode(tampered).decode()

        with pytest.raises(EncryptionError, match="Invalid or tampered payload"):
            encryptor.decrypt(tampered_str)

    def test_decrypt_invalid_base64(self, encryptor: PayloadEncryptor) -> None:
        """Test that invalid base64 is handled."""
        with pytest.raises(EncryptionError, match="Invalid or tampered payload"):
            encryptor.decrypt("not-valid-base64!!!")

    def test_decrypt_too_short_payload(self, encryptor: PayloadEncryptor) -> None:
        """Test that payload shorter than nonce is handled."""
        short_payload = base64.urlsafe_b64encode(b"short").decode()
        with pytest.raises(EncryptionError, match="Invalid or tampered payload"):
            encryptor.decrypt(short_payload)

    def test_encrypt_large_payload(self, encryptor: PayloadEncryptor) -> None:
        """Test encryption of a large payload."""
        # Create a large payload with many events
        large_data = {
            "events": [
                {"id": i, "type": "test", "data": "x" * 100} for i in range(1000)
            ]
        }

        encrypted = encryptor.encrypt(large_data)
        decrypted = encryptor.decrypt(encrypted)

        assert decrypted == large_data

    def test_encrypt_special_characters(self, encryptor: PayloadEncryptor) -> None:
        """Test encryption of special characters and unicode."""
        data = {
            "unicode": "Hello 世界 🔐",
            "special": "!@#$%^&*()_+-=[]{}|;':\",./<>?",
            "newlines": "line1\nline2\r\nline3",
        }

        encrypted = encryptor.encrypt(data)
        decrypted = encryptor.decrypt(encrypted)

        assert decrypted == data

    def test_verify_key_valid(self, encryptor: PayloadEncryptor) -> None:
        """Test key verification with valid key."""
        assert encryptor.verify_key() is True

    def test_verify_key_determinism(self, valid_project_key: str) -> None:
        """Test that encryption is non-deterministic (different nonces)."""
        encryptor = PayloadEncryptor(valid_project_key)
        data = {"test": "value"}

        # Encrypt same data twice
        encrypted1 = encryptor.encrypt(data)
        encrypted2 = encryptor.encrypt(data)

        # Should produce different ciphertexts (different nonces)
        assert encrypted1 != encrypted2

        # But both should decrypt to same value
        assert encryptor.decrypt(encrypted1) == data
        assert encryptor.decrypt(encrypted2) == data

    def test_json_serialization_determinism(self, encryptor: PayloadEncryptor) -> None:
        """Test that JSON serialization is deterministic."""
        data1 = {"b": 2, "a": 1}
        data2 = {"a": 1, "b": 2}

        encrypted1 = encryptor.encrypt(data1)
        encrypted2 = encryptor.encrypt(data2)

        # Decrypt both
        decrypted1 = encryptor.decrypt(encrypted1)
        decrypted2 = encryptor.decrypt(encrypted2)

        # Both should produce same result (sorted keys)
        assert decrypted1 == decrypted2


class TestCreateEncryptor:
    """Test suite for create_encryptor factory function."""

    def test_create_with_valid_key(self) -> None:
        """Test factory with valid key."""
        key = base64.urlsafe_b64encode(b"0" * 32).decode()
        encryptor = create_encryptor(key)

        assert encryptor is not None
        assert isinstance(encryptor, PayloadEncryptor)

    def test_create_with_none_key(self) -> None:
        """Test factory with None returns None."""
        encryptor = create_encryptor(None)
        assert encryptor is None

    def test_create_with_empty_key(self) -> None:
        """Test factory with empty string returns None."""
        encryptor = create_encryptor("")
        assert encryptor is None

    def test_create_with_invalid_key(self) -> None:
        """Test factory with invalid key raises EncryptionError."""
        with pytest.raises(EncryptionError):
            create_encryptor("invalid-key")


class TestEncryptionSecurity:
    """Test suite for security properties of encryption."""

    @pytest.fixture
    def encryptor(self) -> PayloadEncryptor:
        """Create encryptor with valid key."""
        key = base64.urlsafe_b64encode(b"1" * 32).decode()
        return PayloadEncryptor(key)

    def test_different_keys_produce_different_ciphertexts(self) -> None:
        """Test that different keys produce different ciphertexts."""
        key1 = base64.urlsafe_b64encode(b"1" * 32).decode()
        key2 = base64.urlsafe_b64encode(b"2" * 32).decode()

        encryptor1 = PayloadEncryptor(key1)
        encryptor2 = PayloadEncryptor(key2)

        data = {"test": "value"}
        encrypted1 = encryptor1.encrypt(data)
        encrypted2 = encryptor2.encrypt(data)

        # Different keys should produce different ciphertexts
        assert encrypted1 != encrypted2

        # Each can only decrypt its own
        assert encryptor1.decrypt(encrypted1) == data
        assert encryptor2.decrypt(encrypted2) == data

        with pytest.raises(EncryptionError):
            encryptor1.decrypt(encrypted2)

        with pytest.raises(EncryptionError):
            encryptor2.decrypt(encrypted1)

    def test_nonce_uniqueness(self, encryptor: PayloadEncryptor) -> None:
        """Test that nonces are unique across encryptions."""
        data = {"test": "value"}
        nonces = set()

        for _ in range(100):
            encrypted = encryptor.encrypt(data)
            encrypted_bytes = base64.urlsafe_b64decode(encrypted.encode())
            nonce = encrypted_bytes[:12]  # First 12 bytes
            nonces.add(nonce)

        # All nonces should be unique
        assert len(nonces) == 100

    def test_ciphertext_size(self, encryptor: PayloadEncryptor) -> None:
        """Test ciphertext size is reasonable."""
        data = {"test": "value"}
        plaintext = json.dumps(data, separators=(",", ":"), sort_keys=True)
        encrypted = encryptor.encrypt(data)
        encrypted_bytes = base64.urlsafe_b64decode(encrypted.encode())

        # Size should be: nonce (12) + plaintext + tag (16)
        expected_size = 12 + len(plaintext.encode()) + 16

        assert len(encrypted_bytes) == expected_size


class TestDefaultJsonHandler:
    """Test suite for _default_json_handler function."""

    def test_datetime_serialization(self) -> None:
        """Test that datetime objects are serialized to ISO format."""
        dt = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        result = _default_json_handler(dt)
        assert result == "2024-01-01T12:00:00+00:00"

    def test_uuid_serialization(self) -> None:
        from uuid import UUID

        value = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
        result = _default_json_handler(value)
        assert result == "cccccccc-cccc-cccc-cccc-cccccccccccc"

    def test_unsupported_type_raises_type_error(self) -> None:
        """Test that unsupported types raise TypeError (line 20)."""

        class CustomObject:
            pass

        obj = CustomObject()
        with pytest.raises(
            TypeError, match="Object of type CustomObject is not JSON serializable"
        ):
            _default_json_handler(obj)


class TestEncryptionEdgeCases:
    """Test suite for edge cases."""

    @pytest.fixture
    def encryptor(self) -> PayloadEncryptor:
        """Create encryptor with valid key."""
        key = base64.urlsafe_b64encode(b"0" * 32).decode()
        return PayloadEncryptor(key)

    def test_encrypt_empty_dict(self, encryptor: PayloadEncryptor) -> None:
        """Test encryption of empty dictionary."""
        data: dict[str, Any] = {}
        encrypted = encryptor.encrypt(data)
        decrypted = encryptor.decrypt(encrypted)

        assert decrypted == data

    def test_encrypt_nested_data(self, encryptor: PayloadEncryptor) -> None:
        """Test encryption of deeply nested data."""
        data = {
            "level1": {
                "level2": {"level3": {"level4": {"value": "deep"}, "array": [1, 2, 3]}}
            }
        }

        encrypted = encryptor.encrypt(data)
        decrypted = encryptor.decrypt(encrypted)

        assert decrypted == data

    def test_encrypt_with_null_values(self, encryptor: PayloadEncryptor) -> None:
        """Test encryption with null/None values."""
        data = {"null_value": None, "empty_string": "", "zero": 0}

        encrypted = encryptor.encrypt(data)
        decrypted = encryptor.decrypt(encrypted)

        assert decrypted == data

    def test_encrypt_json_serialization_error(
        self, encryptor: PayloadEncryptor
    ) -> None:
        """Test that non-serializable data raises EncryptionError (line 118-119)."""
        # Mock json.dumps to raise an error
        import json
        from unittest.mock import patch

        with patch.object(json, "dumps", side_effect=TypeError("not serializable")):
            with pytest.raises(EncryptionError, match="Failed to encrypt payload"):
                encryptor.encrypt({"test": "value"})

    def test_verify_key_with_encryption_error(self) -> None:
        """Test verify_key returns False when encryption fails."""
        key = base64.urlsafe_b64encode(b"0" * 32).decode()
        encryptor = PayloadEncryptor(key)

        # Mock encrypt to raise EncryptionError
        from unittest.mock import patch

        with patch.object(
            encryptor, "encrypt", side_effect=EncryptionError("test error")
        ):
            assert encryptor.verify_key() is False
