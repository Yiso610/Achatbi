"""Argon2id password hashing and local password policy."""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type

from auth.models import ValidationError


class PasswordService:
    """Hash and verify passwords without ever storing plaintext."""

    def __init__(
        self,
        *,
        time_cost: int = 3,
        memory_cost: int = 65536,
        parallelism: int = 4,
    ) -> None:
        self._hasher = PasswordHasher(
            time_cost=time_cost,
            memory_cost=memory_cost,
            parallelism=parallelism,
            hash_len=32,
            salt_len=16,
            type=Type.ID,
        )

    @staticmethod
    def validate(password: str) -> None:
        """Apply the same policy to initial, reset, and changed passwords."""
        if not isinstance(password, str):
            raise ValidationError("密码格式无效。")
        if len(password) < 10:
            raise ValidationError("密码至少需要 10 个字符。")
        if len(password) > 128:
            raise ValidationError("密码不能超过 128 个字符。")
        if not any(character.isalpha() for character in password):
            raise ValidationError("密码至少需要包含一个字母。")
        if not any(character.isdigit() for character in password):
            raise ValidationError("密码至少需要包含一个数字。")

    def hash(self, password: str) -> str:
        self.validate(password)
        return self._hasher.hash(password)

    def verify(self, encoded_hash: str, password: str) -> bool:
        try:
            return bool(self._hasher.verify(encoded_hash, password))
        except (InvalidHashError, VerificationError, VerifyMismatchError):
            return False

    def needs_rehash(self, encoded_hash: str) -> bool:
        try:
            return self._hasher.check_needs_rehash(encoded_hash)
        except InvalidHashError:
            return True
