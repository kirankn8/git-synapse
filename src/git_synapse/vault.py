"""The one secret this system has to be able to read back.

Everything else it stores is a hash: a session cookie, an API token, a
password. That works because those are only ever *checked*, never replayed --
a database dump yields the fact that a credential existed and nothing usable.

An access token for a private repository breaks that, unavoidably. `git clone`
has to be handed the actual characters, so the deployment must be able to
recover them. The next best guarantee is that the database alone is not
enough: the ciphertext lives in Postgres and the key lives in the environment,
so a dump, a backup, or a replica leaks nothing without the process's own
configuration.

That is a real reduction in the strength of the promise, and it is why storing
one is opt-in. With no key configured this refuses to store anything and the
deployment-level credentials in the environment remain the only way in.

Fernet rather than anything hand-rolled: AES-128-CBC with an HMAC over the
ciphertext, a random IV per message and a timestamp, from a library that is
already installed here. Encryption written for the occasion is the one kind of
code that fails silently and completely.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os

log = logging.getLogger(__name__)

#: The key, as either a Fernet key (44 urlsafe-base64 characters) or any
#: passphrase, which is stretched into one. A passphrase is accepted because
#: the alternative is people pasting `openssl rand` output into a compose file
#: and losing it -- and a stretched passphrase is far better than the feature
#: going unused.
ENV_KEY = "GS_SECRET_KEY"

#: Fixed salt. A per-deployment salt would have to be stored somewhere, and the
#: only place available is the database this is protecting the contents of. The
#: work factor is what defends a weak passphrase here, not the salt's secrecy.
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
    """Decrypt a stored token, or return empty on anything unreadable.

    Empty rather than raising, in every failure: a rotated key, a truncated
    column, a row written by another deployment. The caller's fallback is the
    environment credential, which is a working outcome -- while an exception
    here would take down a discovery run over one unreadable source.
    """
    if not sealed:
        return ""
    try:
        return _fernet().decrypt(sealed.encode()).decode()
    except Exception:  # noqa: BLE001 - see above; unreadable is not fatal
        log.warning("a stored access token could not be decrypted; "
                    "falling back to the environment credential")
        return ""


def hint(secret: str) -> str:
    """A few characters a person can recognise their own token by.

    The prefix and the last four, which is how every host prints them. Never
    enough to use, always enough to answer "is that the one I pasted?".
    """
    text = (secret or "").strip()
    if len(text) < 12:
        return "••••"
    return f"{text[:4]}…{text[-4:]}"
