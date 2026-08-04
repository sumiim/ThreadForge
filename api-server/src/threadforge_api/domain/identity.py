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
    subject: str = ""
    login: str = ""
    name: str = ""
    avatar_url: str = ""

    def public_dict(self) -> dict[str, str]:
        return {
            "owner_id": self.owner_id,
            "subject": self.subject,
            "login": self.login,
            "name": self.name,
            "avatar_url": self.avatar_url,
        }


def canonical_owner_id(value: str | UUID) -> str:
    """Return the canonical UUID spelling or raise ``ValueError``."""

    return str(UUID(str(value)))
