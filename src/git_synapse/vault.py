"""The one secret this system has to be able to read back."""
from __future__ import annotations

import base64
import hashlib
import logging
import os

log = logging.getLogger(__name__)

ENV_KEY = "GS_SECRET_KEY"

_SALT = b"git-synapse-vault-v1"


class VaultError(RuntimeError):
    """No key is configured, so nothing can be stored."""


def _fernet():
    from cryptography.fernet import Fernet

    raw = (os.environ.get(ENV_KEY) or "").strip()
    if not raw:
        raise VaultError(
            f"{ENV_KEY} is not set, so an access token cannot be stored. Set it "
            "to any passphrase and restart, or use the deployment-wide token "
            "environment variables instead."
        )
    try:
        return Fernet(raw.encode())
    except (ValueError, TypeError):
        # Not a Fernet key, so treat it as a passphrase and stretch it.
        digest = hashlib.scrypt(raw.encode(), salt=_SALT, n=2**14, r=8, p=1,
                                dklen=32, maxmem=64 * 1024 * 1024)
        return Fernet(base64.urlsafe_b64encode(digest))


def available() -> bool:
    """Whether a token could be stored right now. Asked before offering to."""
    try:
        _fernet()
    except VaultError:
        return False
    return True


def seal(secret: str) -> str:
    """Encrypt a token for storage. Raises when no key is configured."""
    if not secret:
        raise VaultError("nothing to store")
    return _fernet().encrypt(secret.encode()).decode()


def open_(sealed: str | None) -> str:
    """Decrypt a stored token, or return empty on anything unreadable."""
    if not sealed:
        return ""
    try:
        return _fernet().decrypt(sealed.encode()).decode()
    except Exception:  # noqa: BLE001 - see above; unreadable is not fatal
        log.warning("a stored access token could not be decrypted; "
                    "falling back to the environment credential")
        return ""


def hint(secret: str) -> str:
    """A few characters a person can recognise their own token by."""
    text = (secret or "").strip()
    if len(text) < 12:
        return "••••"
    return f"{text[:4]}…{text[-4:]}"
