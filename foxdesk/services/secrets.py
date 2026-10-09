"""Encrypting secrets entered in the app (directory passwords, API keys).

The key is derived from SECRET_KEY plus a purpose, so each kind of secret
has its own key. If SECRET_KEY changes, saved secrets can't be read and the
page that owns them asks for them again.
"""
import base64
import hashlib

from foxdesk.core import app


def _fernet(purpose):
    from cryptography.fernet import Fernet
    key = hashlib.sha256(f'foxdesk-{purpose}:{app.secret_key}'.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_secret(value, purpose):
    return _fernet(purpose).encrypt(value.encode()).decode()


def decrypt_secret(token, purpose):
    """None if it can't be read (usually SECRET_KEY changed since it was saved)."""
    from cryptography.fernet import InvalidToken
    try:
        return _fernet(purpose).decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return None
