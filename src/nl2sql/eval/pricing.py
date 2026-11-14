"""Cost from ``config/pricing.yaml``. Missing prices give ``None``, never an invented number."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

DEFAULT_PRICING_PATH = Path(__file__).resolve().parents[3] / "config" / "pricing.yaml"


def load_pricing(path: str | Path | None = None) -> dict[str, Any]:
    p = Path(path) if path else DEFAULT_PRICING_PATH
    if not p.exists():
        return {"models": {}}
    data = yaml.safe_load(p.read_text()) or {}
    return data if isinstance(data, dict) else {"models": {}}


def compute_cost(
    usage_by_node: dict[str, dict[str, dict[str, int]]], pricing: dict[str, Any]
) -> tuple[float | None, list[str]]:
    """(total USD, models without a price). Total is None if any used model has no price."""
    models = pricing.get("models") or {}
    total = 0.0
    missing: set[str] = set()
    for per_model in usage_by_node.values():
        for model, u in per_model.items():
            price = models.get(model) or {}
            pin, pout = price.get("input_per_1m_usd"), price.get("output_per_1m_usd")
            if pin is None or pout is None:
                if u["input_tokens"] or u["output_tokens"]:
                    missing.add(model)
                continue
            total += (u["input_tokens"] * pin + u["output_tokens"] * pout) / 1_000_000
    return (None if missing else total), sorted(missing)
