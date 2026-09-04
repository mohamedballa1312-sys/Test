"""PII masking, column encryption (AES-256-GCM) and keyed hashing."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# Any 10-digit run (ASCII or Arabic-Indic) — Iqama, national ID, unified establishment number.
_TEN_DIGITS = re.compile(r"(?<![0-9٠-٩])([0-9٠-٩]{10})(?![0-9٠-٩])")


def mask_id(value: str | None, keep: int = 4) -> str | None:
    """2401246992 -> 2401******. Never log an unmasked ID."""
    if not value:
        return value
    s = str(value)
    if len(s) <= keep:
        return "*" * len(s)
    return s[:keep] + "*" * (len(s) - keep)


def mask_pii_text(text: str) -> str:
    """Mask every 10-digit identifier inside free text (used by the logging processor)."""
    if not isinstance(text, str):
        return text
    return _TEN_DIGITS.sub(lambda m: mask_id(m.group(1)), text)


def _load_or_create_key(explicit_b64: str | None, data_dir: Path) -> bytes:
    if explicit_b64:
        key = base64.b64decode(explicit_b64)
        if len(key) != 32:
            raise ValueError("IQAMA_ENC_KEY must decode to 32 bytes")
        return key
    key_file = data_dir / ".enc_key"
    if key_file.exists():
        return base64.b64decode(key_file.read_text().strip())
    key = AESGCM.generate_key(bit_length=256)
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_text(base64.b64encode(key).decode())
    try:
        os.chmod(key_file, 0o600)
    except OSError:
        pass
    return key


class Encryptor:
    """AES-256-GCM. Ciphertext format: base64(nonce || ct||tag). Same key also used for HMAC lookups."""

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("key must be 32 bytes")
        self._key = key
        self._aead = AESGCM(key)

    @classmethod
    def from_settings(cls, enc_key_b64: str | None, data_dir: Path) -> "Encryptor":
        return cls(_load_or_create_key(enc_key_b64, data_dir))

    def encrypt(self, plaintext: str | None) -> str | None:
        if plaintext is None:
            return None
        nonce = secrets.token_bytes(12)
        ct = self._aead.encrypt(nonce, plaintext.encode("utf-8"), None)
        return base64.b64encode(nonce + ct).decode("ascii")

    def decrypt(self, token: str | None) -> str | None:
        if token is None:
            return None
        raw = base64.b64decode(token)
        return self._aead.decrypt(raw[:12], raw[12:], None).decode("utf-8")

    def encrypt_bytes(self, data: bytes) -> bytes:
        nonce = secrets.token_bytes(12)
        return nonce + self._aead.encrypt(nonce, data, None)

    def decrypt_bytes(self, blob: bytes) -> bytes:
        return self._aead.decrypt(blob[:12], blob[12:], None)

    def keyed_hash(self, value: str) -> str:
        """Deterministic HMAC for equality lookups (duplicates) without decrypting."""
        return hmac.new(self._key, value.encode("utf-8"), hashlib.sha256).hexdigest()


_encryptor: Encryptor | None = None


def get_encryptor() -> Encryptor:
    global _encryptor
    if _encryptor is None:
        from app.core.config import get_settings

        s = get_settings()
        _encryptor = Encryptor.from_settings(s.enc_key, s.data_dir)
    return _encryptor


def reset_encryptor() -> None:
    global _encryptor
    _encryptor = None
