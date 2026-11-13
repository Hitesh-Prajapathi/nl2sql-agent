from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DatabaseProfile:
    """Database-agnostic metadata used for headers, refusals and date notes."""

    db_id: str
    display_name: str
    domain_description: str | None = None
    # Keyed "table.column" -> (min_iso, max_iso); filled during introspection.
    date_ranges: dict[str, tuple[str, str]] = field(default_factory=dict)

    @classmethod
    def from_db_name(cls, db_name: str) -> DatabaseProfile:
        return cls(db_id=db_name, display_name=db_name)

    def header(self) -> str:
        text = f"=== DATABASE SCHEMA ({self.display_name}) ===\n"
        if self.domain_description:
            text += f"Domain: {self.domain_description}\n"
        return text
