"""Identity primitives for the single-owner V1.5 compatibility boundary."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class Actor:
    """Authenticated actor supplied by the API boundary.

    V1.5 deliberately resolves this actor from server configuration. A future
    OIDC adapter can replace that dependency without changing service methods.
    """

    owner_id: str


def canonical_owner_id(value: str | UUID) -> str:
    """Return the canonical UUID spelling or raise ``ValueError``."""

    return str(UUID(str(value)))
