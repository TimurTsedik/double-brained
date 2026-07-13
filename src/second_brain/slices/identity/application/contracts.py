from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class AccessContext:
    user_id: UUID
    user_space_id: UUID


class UpdateTransaction(Protocol):
    """Published marker for work performed in an existing update transaction."""
