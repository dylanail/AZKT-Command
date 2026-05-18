from __future__ import annotations

import json
from pathlib import Path

from ..core.config import CONFIG_DIR

_PRICING = CONFIG_DIR / "pricing.json"


def _load() -> dict:
    return json.loads(_PRICING.read_text())


def cost_usd(model: str, input_tokens: int, output_tokens: int,
             cache_write: int = 0, cache_read: int = 0) -> tuple[float, bool]:
    """Return (cost, fell_back). fell_back=True when model id was unknown."""
    p = _load()
    table = p["per_mtok"]
    fell_back = model not in table
    rates = table.get(model) or table[p["default_model"]]
    c = (
        input_tokens / 1e6 * rates["input"]
        + output_tokens / 1e6 * rates["output"]
        + cache_write / 1e6 * rates.get("cache_write", rates["input"])
        + cache_read / 1e6 * rates.get("cache_read", rates["input"])
    )
    return round(c, 6), fell_back


def pricing_table() -> dict:
    return _load()
