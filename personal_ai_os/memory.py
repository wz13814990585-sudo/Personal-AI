"""Reviewable memory values and deterministic study-time policy."""

from __future__ import annotations

import json
import re
from datetime import time
from typing import Any

from .contracts import MemoryProposal


def search_approved(
    rows: list[dict[str, Any]], query: str, limit: int = 5
) -> list[dict[str, Any]]:
    if limit < 1:
        raise ValueError("memory_limit_must_be_positive")
    approved = [row for row in rows if row["status"] == "approved"]
    query_terms = _terms(query)
    ranked: list[tuple[int, int, str, dict[str, Any]]] = []
    for row in approved:
        policy = row["kind"] == "study_time_avoid"
        score = len(query_terms & _terms(row["value_json"]))
        if policy or not query_terms or score:
            ranked.append((1 if policy else 0, score, row["updated_at_utc"], row))
    ranked.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    return [item[3] for item in ranked[:limit]]


def _terms(value: str) -> set[str]:
    words = set(re.findall(r"[a-zA-Z0-9]+", value.casefold()))
    chinese = re.findall(r"[\u4e00-\u9fff]+", value)
    return words | {
        part[index:index + 2]
        for part in chinese for index in range(max(0, len(part) - 1))
    }


def validate_memory_value(kind: str, value: dict[str, Any]) -> dict[str, Any]:
    if kind == "study_time_avoid":
        if set(value) != {"start", "end"}:
            raise ValueError("study_time_avoid requires start and end")
        try:
            start = time.fromisoformat(value["start"])
            end = time.fromisoformat(value["end"])
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid_study_time_range") from exc
        if start >= end:
            raise ValueError("invalid_study_time_range")
        return {"start": start.strftime("%H:%M"), "end": end.strftime("%H:%M")}
    if kind == "text_preference":
        if set(value) != {"text"} or not isinstance(value["text"], str) or not value["text"].strip():
            raise ValueError("text_preference requires nonempty text")
        return {"text": value["text"].strip()}
    raise ValueError("unsupported_memory_kind")


def normalize_proposal(proposal: MemoryProposal, feedback_id: str, body: str) -> MemoryProposal:
    if proposal.feedback_id != feedback_id:
        raise ValueError("proposal_feedback_mismatch")
    if not proposal.source_excerpt.strip() or proposal.source_excerpt not in body:
        raise ValueError("proposal_source_not_in_feedback")
    value = proposal.value
    if proposal.kind == "study_time_avoid" and "早上" in body and any(
        word in body for word in ("不要", "不喜欢", "避免", "别")
    ):
        value = {"start": "00:00", "end": "12:00"}
    return proposal.model_copy(update={"value": validate_memory_value(proposal.kind, value)})


def approved_study_avoid_ranges(
    rows: list[dict[str, Any]],
) -> tuple[list[tuple[time, time]], list[str]]:
    ranges: list[tuple[time, time]] = []
    ids: list[str] = []
    for row in rows:
        if row["status"] != "approved" or row["kind"] != "study_time_avoid":
            continue
        value = validate_memory_value(row["kind"], json.loads(row["value_json"]))
        ranges.append((time.fromisoformat(value["start"]), time.fromisoformat(value["end"])))
        ids.append(row["id"])
    return ranges, ids
