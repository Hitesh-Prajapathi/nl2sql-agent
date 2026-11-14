"""BIRD Mini-Dev question loading."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

QUESTIONS_REL = Path("raw/minidev/MINIDEV/mini_dev_postgresql.json")
TABLES_REL = Path("raw/minidev/MINIDEV/dev_tables.json")


@dataclass(frozen=True)
class Question:
    question_id: int
    db_id: str
    question: str
    evidence: str
    gold_sql: str
    difficulty: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_questions(data_dir: str | Path, file: str | Path | None = None) -> list[Question]:
    """Load questions from the official Mini-Dev JSON (or a fixture in the same format)."""
    path = Path(file) if file else Path(data_dir) / QUESTIONS_REL
    raw = json.loads(path.read_text())
    return [
        Question(
            question_id=int(r["question_id"]),
            db_id=str(r["db_id"]),
            question=str(r["question"]),
            evidence=str(r.get("evidence") or ""),
            gold_sql=str(r["SQL"]),
            difficulty=str(r.get("difficulty") or "unknown"),
        )
        for r in raw
    ]


def load_table_columns(data_dir: str | Path, file: str | Path | None = None) -> dict[str, dict[str, list[str]]]:
    """db_id -> lower(table) -> [lower(column), ...] from dev_tables.json."""
    path = Path(file) if file else Path(data_dir) / TABLES_REL
    out: dict[str, dict[str, list[str]]] = {}
    for d in json.loads(path.read_text()):
        tables = [t.lower() for t in d["table_names_original"]]
        cols: dict[str, list[str]] = {t: [] for t in tables}
        for t_idx, name in d["column_names_original"]:
            if t_idx >= 0:
                cols[tables[t_idx]].append(str(name).lower())
        out[d["db_id"]] = cols
    return out


def dataset_hash(questions: list[Question]) -> str:
    """Stable hash of the evaluated question set (ids, text, evidence, gold SQL)."""
    h = hashlib.sha256()
    for q in sorted(questions, key=lambda x: x.question_id):
        h.update(json.dumps(q.to_dict(), sort_keys=True).encode())
    return h.hexdigest()
