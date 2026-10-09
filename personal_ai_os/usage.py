"""Real request accounting; prices are optional user-entered estimates."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from .custom_agent_store import CustomAgentStore
from .storage import Repository


def _price(value: str) -> str:
    try:
        amount = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("invalid_model_price") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError("invalid_model_price")
    return str(amount)


def set_model_prices(repository: Repository, model_id: str,
                     input_per_million: str, output_per_million: str) -> None:
    prices = repository.get_setting("model_prices", {})
    if not input_per_million.strip() and not output_per_million.strip():
        prices.pop(model_id, None)
    elif not input_per_million.strip() or not output_per_million.strip():
        raise ValueError("both_model_prices_required")
    else:
        prices[model_id] = {
            "input_per_million": _price(input_per_million),
            "output_per_million": _price(output_per_million),
        }
    repository.set_setting("model_prices", prices)


def usage_summary(repository: Repository, *, run_id: str | None = None) -> dict[str, Any]:
    rows = repository.list_model_usage(run_id) + CustomAgentStore(repository).list_usage(run_id)
    prices = repository.get_setting("model_prices", {})
    by_model: dict[str, dict[str, Any]] = {}
    estimate = Decimal("0")
    priced_count = 0
    for row in rows:
        model = by_model.setdefault(row["model_id"], {
            "model_id": row["model_id"], "request_count": 0,
            "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
            "missing_token_records": 0, "duration_ms": 0,
        })
        model["request_count"] += 1
        model["duration_ms"] += row["duration_ms"]
        if any(row[key] is None for key in ("input_tokens", "output_tokens", "total_tokens")):
            model["missing_token_records"] += 1
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            if row[key] is not None:
                model[key] += row[key]
        price = prices.get(row["model_id"])
        if price and row["input_tokens"] is not None and row["output_tokens"] is not None:
            estimate += (
                Decimal(row["input_tokens"]) * Decimal(price["input_per_million"])
                + Decimal(row["output_tokens"]) * Decimal(price["output_per_million"])
            ) / Decimal(1_000_000)
            priced_count += 1
    return {
        "request_count": len(rows),
        "duration_ms": sum(row["duration_ms"] for row in rows),
        "input_tokens": sum(row["input_tokens"] or 0 for row in rows),
        "output_tokens": sum(row["output_tokens"] or 0 for row in rows),
        "total_tokens": sum(row["total_tokens"] or 0 for row in rows),
        "missing_token_records": sum(item["missing_token_records"] for item in by_model.values()),
        "estimated_cost_usd": str(estimate) if priced_count else None,
        "estimated_request_count": priced_count,
        "by_model": list(by_model.values()),
    }
