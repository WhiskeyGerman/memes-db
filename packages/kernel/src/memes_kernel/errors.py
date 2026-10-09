"""Общие ошибки нарушения доменных инвариантов."""

from enum import StrEnum


class DomainError(ValueError):
    """Нарушение доменного правила с машинно-читаемым кодом."""

    def __init__(self, code: StrEnum) -> None:
        """Создать ошибку.

        Args:
            code: Код нарушенного правила.
        """
        self.code = code
        super().__init__(code.value)
