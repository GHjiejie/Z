"""Password hashing, local password screening and opaque session credentials."""

import hashlib
import secrets
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Passwords:
    def __init__(self, additional_blocklist: Path | None = None) -> None:
        self.hasher = PasswordHasher(time_cost=2, memory_cost=19_456, parallelism=1)
        paths = [Path(__file__).with_name("weak-passwords.txt")]
        if additional_blocklist is not None:
            paths.append(additional_blocklist)
        self.blocked = {
            line.strip().casefold()
            for path in paths
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        }
        self.dummy_hash = self.hash(secrets.token_urlsafe(32))

    def is_weak(self, password: str) -> bool:
        folded = password.casefold()
        if folded.strip() in self.blocked or len(set(folded)) <= 1:
            return True
        # Catch short repeated patterns without imposing character-class rules.
        return any(
            len(folded) % size == 0 and folded == folded[:size] * (len(folded) // size)
            for size in range(2, 5)
        )

    def hash(self, password: str) -> str:
        return self.hasher.hash(password)

    def verify(self, encoded: str, password: str) -> bool:
        try:
            return self.hasher.verify(encoded, password)
        except (VerificationError, InvalidHashError):
            return False
