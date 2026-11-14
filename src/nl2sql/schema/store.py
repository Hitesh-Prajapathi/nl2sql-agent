"""Offline profile store: versioned JSON files built once and loaded at request time."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nl2sql.config import get_settings
from nl2sql.schema.introspect import PROFILE_FORMAT_VERSION, render_context
from nl2sql.schema.profile import DatabaseProfile
from nl2sql.schema.provider import SchemaProvider


def profile_path(db_id: str, directory: str | Path | None = None) -> Path:
    return Path(directory or get_settings().profile_dir) / f"{db_id}.json"


def content_hash(data: dict[str, Any]) -> str:
    """Stable hash of the profile content (excluding bookkeeping fields)."""
    body = {k: v for k, v in data.items() if k not in ("built_at", "content_sha256")}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def save_profile(data: dict[str, Any], directory: str | Path | None = None) -> Path:
    data = dict(data)
    data["built_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    data["content_sha256"] = content_hash(data)
    path = profile_path(data["db_id"], directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    return path


def load_profile(path: str | Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(Path(path).read_text())
    if data.get("format_version") != PROFILE_FORMAT_VERSION:
        raise ValueError(
            f"{path}: profile format {data.get('format_version')} != {PROFILE_FORMAT_VERSION}; "
            "rebuild it with `make profile`"
        )
    return data


class StoredProfileSchemaProvider(SchemaProvider):
    """Serves a pre-built profile: no database access, no per-request catalog hash query."""

    def __init__(self, data: dict[str, Any], table_token_budget: int | None = None) -> None:
        self._data = data
        budget = table_token_budget or get_settings().profile_table_token_budget
        self.profile = DatabaseProfile(
            db_id=data["db_id"],
            display_name=data.get("display_name") or data["db_id"],
            domain_description=data.get("domain_description"),
            date_ranges={k: (v[0], v[1]) for k, v in data.get("date_ranges", {}).items()},
        )
        self._context = render_context(data, budget, self.profile)

    @classmethod
    def from_file(
        cls, path: str | Path, table_token_budget: int | None = None
    ) -> StoredProfileSchemaProvider:
        return cls(load_profile(path), table_token_budget)

    @property
    def content_sha256(self) -> str:
        return str(self._data.get("content_sha256") or content_hash(self._data))

    @property
    def data(self) -> dict[str, Any]:
        return self._data

    async def get_context(self, question: str = "") -> str:
        return self._context
