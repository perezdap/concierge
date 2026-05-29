"""
Profiles — named bundles of catalog selectors that can be activated to publish
a curated subset of capabilities to a session.

A profile is a list of *selectors* (not literal canonical names). At apply-time
we resolve selectors against the current catalog so a profile remains valid
even if upstream servers are reconfigured.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..core.catalog import Catalog
from ..core.types import CatalogEntry, PrimitiveType


@dataclass
class ProfileSelector:
    server: str | None = None
    tags: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    primitive_type: PrimitiveType | None = None
    names: list[str] = field(default_factory=list)  # exact canonical names

    def matches(self, entry: CatalogEntry) -> bool:
        if self.server and entry.server_id != self.server:
            return False
        if self.primitive_type and entry.primitive_type != self.primitive_type:
            return False
        if self.tags and not set(t.lower() for t in self.tags).issubset({t.lower() for t in entry.tags}):
            return False
        if self.categories and not set(c.lower() for c in self.categories).issubset({c.lower() for c in entry.categories}):
            return False
        if self.names and entry.canonical_name not in self.names:
            return False
        return True


@dataclass
class Profile:
    name: str
    description: str = ""
    selectors: list[ProfileSelector] = field(default_factory=list)
    # Apply automatically at session init (see GatewayService.initialize).
    auto_apply: bool = False

    def resolve(self, catalog: Catalog) -> list[str]:
        """Return matching canonical names."""
        out: list[str] = []
        for e in catalog.store.all():
            if any(s.matches(e) for s in self.selectors):
                out.append(e.canonical_name)
        return sorted(set(out))


class ProfileRegistry:
    def __init__(self) -> None:
        self._profiles: dict[str, Profile] = {}

    def register(self, profile: Profile) -> None:
        self._profiles[profile.name] = profile

    def get(self, name: str) -> Profile | None:
        return self._profiles.get(name)

    def all(self) -> list[Profile]:
        return list(self._profiles.values())

    def auto_apply_profiles(self) -> list[Profile]:
        """Profiles flagged to publish at session init."""
        return [p for p in self._profiles.values() if p.auto_apply]
