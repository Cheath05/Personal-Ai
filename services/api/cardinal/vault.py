"""Encrypts account tokens before they touch the database.

The key lives in its own file next to the database (data/secret.key, readable only by you), not in the
database. Backups copy the database only, so a stolen backup can't sign in to your Google account.
Lose the key and you just reconnect your accounts.
"""

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class VaultError(Exception):
    pass


class Vault:
    def __init__(self, key_file: Path):
        self.key_file = key_file
        self._fernet: Fernet | None = None

    def _get(self) -> Fernet:
        if self._fernet is None:
            if not self.key_file.exists():
                self.key_file.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(Fernet.generate_key())
            self._fernet = Fernet(self.key_file.read_bytes().strip())
        return self._fernet

    def encrypt(self, value: str) -> str:
        return self._get().encrypt(value.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._get().decrypt(token.encode()).decode()
        except InvalidToken as e:
            raise VaultError("Stored token can't be decrypted (the key changed). Reconnect the account.") from e
