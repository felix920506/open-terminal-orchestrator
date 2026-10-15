"""At-rest encryption for secrets stored in the database."""

from __future__ import annotations

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger(__name__)

_PLAIN_PREFIX = "plain:"
_ENC_PREFIX = "fernet:"


class SecretBox:
    """Encrypts with Fernet when a secret key is configured, else stores tagged plaintext."""

    def __init__(self, secret_key: str):
        self._fernet = None
        if secret_key:
            derived = base64.urlsafe_b64encode(hashlib.sha256(secret_key.encode()).digest())
            self._fernet = Fernet(derived)
        else:
            log.warning("ORCH_SECRET_KEY is not set; secrets are stored unencrypted")

    def seal(self, value: str) -> str:
        if self._fernet is None:
            return _PLAIN_PREFIX + value
        return _ENC_PREFIX + self._fernet.encrypt(value.encode()).decode()

    def open(self, value: str) -> str:
        if value.startswith(_PLAIN_PREFIX):
            return value[len(_PLAIN_PREFIX) :]
        if value.startswith(_ENC_PREFIX):
            if self._fernet is None:
                raise RuntimeError("Encrypted secret found but ORCH_SECRET_KEY is not set")
            try:
                return self._fernet.decrypt(value[len(_ENC_PREFIX) :].encode()).decode()
            except InvalidToken as e:
                raise RuntimeError("Cannot decrypt secret; ORCH_SECRET_KEY changed?") from e
        return value
