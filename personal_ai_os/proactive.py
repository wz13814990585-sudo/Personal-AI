"""Grounded, reviewable suggestions with immutable strategy versions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from .contracts import SuggestionStrategy
from .memory import search_approved
from .storage import Repository, _rows


_POLICY_FIELD = {
    "overdue_task": "overdue_days",
    "habit_gap": "habit_gap_min",
    "daily_review": "review_unfinished_min",
}
_MINIMUM = {"overdue_task": 0, "habit_gap": 1, "daily_review": 1}


def _stamp(now: datetime) -> str:
    if now.tzinfo is None:
        raise ValueError("timezone_aware_datetime_required")
    return now.astimezone(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _audit(connection: sqlite3.Connection, event: str, summary: dict[str, Any], actor: str) -> None:
    connection.execute(
        "INSERT INTO trace_events(id,actor,event_type,summary_json,created_at_utc) "
        "VALUES (?,?,?,?,?)",
        (uuid4().hex, actor, event, _json(summary), _stamp(datetime.now(timezone.utc))),
    )


def _active_strategy(connection: sqlite3.Connection) -> tuple[int, SuggestionStrategy]:
    row = connection.execute(
        "SELECT v.version,v.policy_json FROM suggestion_strategy_versions v "
        "JOIN suggestion_strategy_state s ON s.active_version=v.version WHERE s.singleton=1"
    ).fetchone()
    if row is None:
        raise RuntimeError("suggestion_strategy_missing")
    return row["version"], SuggestionStrategy.model_validate_json(row["policy_json"])


def current_strategy(repository: Repository) -> dict[str, Any]:
    with repository.connection() as connection:
        version, policy = _active_strategy(connection)
    return {"version": version, "policy": policy.model_dump()}


def list_strategy_versions(repository: Repository) -> list[dict[str, Any]]:
    with repository.connection() as connection:
        active, _ = _active_strategy(connection)
        rows = _rows(connection.execute(
            "SELECT * FROM suggestion_strategy_versions ORDER BY version DESC"
        ))
    return [{**row, "policy": SuggestionStrategy.model_validate_json(
        row["policy_json"]).model_dump(), "active": row["version"] == active} for row in rows]


def _suggestion_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["evidence"] = json.loads(result["evidence_json"])
    result["source_ids"] = json.loads(result["source_ids_json"])
    return result


def list_suggestions(repository: Repository, status: str | None = None) -> list[dict[str, Any]]:
    if status not in (None, "pending", "accepted", "rejected"):
        raise ValueError("invalid_suggestion_status")
    with repository.connection() as connection:
        rows = connection.execute(
            "SELECT * FROM proactive_suggestions" + (" WHERE status=?" if status else "") +
            " ORDER BY generated_at_utc DESC,rowid DESC",
            (status,) if status else (),
        ).fetchall()
    return [_suggestion_row(row) for row in rows]


def _candidate(
    kind: str, subject_id: str, state: dict[str, Any], title: str,
    explanation: str, evidence: dict[str, Any], source_ids: list[str],
    policy: SuggestionStrategy,
) -> dict[str, Any]:
    key = hashlib.sha256(_json({"kind": kind, "subject_id": subject_id,
                                "state": state}).encode()).hexdigest()
    field = _POLICY_FIELD[kind]
    return {
        "dedupe_key": key, "kind": kind, "subject_id": subject_id,
        "title": title[:160], "explanation": explanation[:1000],
        "evidence": evidence, "source_ids": list(dict.fromkeys(source_ids)),
        "proposed_threshold": max(_MINIMUM[kind], getattr(policy, field) - 1),
    }


def _collect(
    connection: sqlite3.Connection, now: datetime, policy: SuggestionStrategy,
) -> list[dict[str, Any]]:
    memories = _rows(connection.execute(
        "SELECT id,kind,value_json,status,updated_at_utc FROM memories "
        "WHERE status='approved' ORDER BY updated_at_utc DESC"
    ))
    def sources(query: str, primary: list[str], evidence: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
        study_context = any(token in query.casefold() for token in
                            ("study", "learn", "interview", "学", "复习", "面试"))
        selected = (search_approved(memories, query, limit=5) if query.strip() else [])
        selected = [item for item in selected
                    if item["kind"] != "study_time_avoid" or study_context][:2]
        selected = [{"id": item["id"], "kind": item["kind"],
                     "value": json.loads(item["value_json"]),
                     "updated_at_utc": item["updated_at_utc"]} for item in selected]
        return primary + [item["id"] for item in selected], {
            **evidence, "approved_memories": selected,
        }

    candidates: list[dict[str, Any]] = []
    now_utc = now.astimezone(timezone.utc)
    for task in _rows(connection.execute(
        "SELECT id,title,status,due_at_utc FROM tasks "
        "WHERE status!='completed' AND due_at_utc IS NOT NULL ORDER BY rowid"
    )):
        due = datetime.fromisoformat(task["due_at_utc"]).astimezone(timezone.utc)
        if due + timedelta(days=policy.overdue_days) > now_utc:
            continue
        source_ids, evidence = sources(task["title"], [task["id"]], {
            "task": {"id": task["id"], "title": task["title"],
                     "status": task["status"], "due_at_utc": task["due_at_utc"]},
        })
        candidates.append(_candidate(
            "overdue_task", task["id"],
            {"status": task["status"], "due_at_utc": task["due_at_utc"]},
            f"检查逾期任务：{task['title']}",
            "这项任务仍未完成且已超过策略规定的期限；建议先检查是否需要重新安排。"
            "接受只调整以后发现逾期信号的阈值，不会修改该任务。",
            evidence, source_ids, policy,
        ))

    for habit in _rows(connection.execute(
        "SELECT id,title,timezone,target_per_week,status FROM habits WHERE status='active' ORDER BY rowid"
    )):
        local_today = now.astimezone(ZoneInfo(habit["timezone"])).date()
        first_day = local_today - timedelta(days=6)
        checkins = _rows(connection.execute(
            "SELECT id,local_date,completed FROM habit_checkins WHERE habit_id=? "
            "AND local_date>=? AND local_date<=? ORDER BY local_date",
            (habit["id"], first_day.isoformat(), local_today.isoformat()),
        ))
        if not checkins:
            continue  # No real progress record means no trend claim.
        completed = sum(bool(item["completed"]) for item in checkins)
        gap = max(0, habit["target_per_week"] - completed)
        if gap < policy.habit_gap_min:
            continue
        source_ids, evidence = sources(habit["title"],
            [habit["id"], *[item["id"] for item in checkins]], {
                "habit": {"id": habit["id"], "title": habit["title"],
                          "timezone": habit["timezone"],
                          "target_per_week": habit["target_per_week"],
                          "completed_last_seven_days": completed,
                          "window_start": first_day.isoformat(),
                          "window_end": local_today.isoformat(),
                          "checkins": checkins},
            })
        candidates.append(_candidate(
            "habit_gap", habit["id"],
            {"target": habit["target_per_week"],
             "checkins": [(item["local_date"], item["completed"]) for item in checkins]},
            f"回顾习惯进度：{habit['title']}",
            f"近七个当地日期记录完成 {completed}/{habit['target_per_week']} 次；"
            "建议检查目标是否适合当前节奏。接受只调整以后发现习惯落差的阈值。",
            evidence, source_ids, policy,
        ))

    for review in _rows(connection.execute(
        "SELECT id,local_date,timezone,snapshot_json,note,revision FROM daily_reviews "
        "ORDER BY local_date DESC LIMIT 14"
    )):
        local_today = now.astimezone(ZoneInfo(review["timezone"])).date()
        review_day = date.fromisoformat(review["local_date"])
        if not 0 <= (local_today - review_day).days <= 6:
            continue
        snapshot = json.loads(review["snapshot_json"])
        unfinished = int(snapshot.get("unfinished_tasks", 0))
        conflicts = int(snapshot.get("plan_conflicts", 0))
        if unfinished < policy.review_unfinished_min and conflicts == 0:
            continue
        source_ids, evidence = sources(review["note"][:200], [review["id"]], {
            "review": {"id": review["id"], "local_date": review["local_date"],
                       "timezone": review["timezone"], "revision": review["revision"],
                       "snapshot": snapshot, "note_excerpt": review["note"][:300]},
        })
        candidates.append(_candidate(
            "daily_review", review["id"],
            {"revision": review["revision"], "snapshot": snapshot},
            f"查看 {review['local_date']} 的每日复盘",
            f"该复盘记录未完成任务 {unfinished} 项、计划冲突 {conflicts} 项；"
            "建议审核次日安排。复盘备注只作依据，不会写入长期记忆。",
            evidence, source_ids, policy,
        ))
    return candidates


def scan_suggestions(
    repository: Repository, now: datetime | None = None,
) -> list[dict[str, Any]]:
    current = now or datetime.now(timezone.utc)
    stamp = _stamp(current)
    inserted: list[str] = []
    with repository.transaction() as connection:
        strategy_version, policy = _active_strategy(connection)
        for candidate in _collect(connection, current, policy):
            suggestion_id = uuid4().hex
            cursor = connection.execute(
                "INSERT INTO proactive_suggestions(id,dedupe_key,kind,subject_id,title,"
                "explanation,evidence_json,source_ids_json,proposed_threshold,"
                "strategy_version,generated_at_utc,updated_at_utc) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(dedupe_key) DO NOTHING",
                (suggestion_id, candidate["dedupe_key"], candidate["kind"],
                 candidate["subject_id"], candidate["title"], candidate["explanation"],
                 _json(candidate["evidence"]), _json(candidate["source_ids"]),
                 candidate["proposed_threshold"], strategy_version, stamp, stamp),
            )
            if cursor.rowcount:
                inserted.append(suggestion_id)
        if inserted:
            _audit(connection, "proactive_suggestions_created",
                   {"suggestion_ids": inserted, "strategy_version": strategy_version}, "system")
        rows = [connection.execute("SELECT * FROM proactive_suggestions WHERE id=?", (item,)).fetchone()
                for item in inserted]
    return [_suggestion_row(row) for row in rows]


def edit_suggestion(
    repository: Repository, suggestion_id: str, revision: int,
    title: str, explanation: str, proposed_threshold: int,
) -> dict[str, Any]:
    title, explanation = title.strip(), explanation.strip()
    if not 1 <= len(title) <= 160 or not 1 <= len(explanation) <= 1000:
        raise ValueError("invalid_suggestion_text")
    with repository.transaction() as connection:
        row = connection.execute("SELECT * FROM proactive_suggestions WHERE id=?",
                                 (suggestion_id,)).fetchone()
        if row is None:
            raise KeyError(suggestion_id)
        if row["status"] != "pending" or row["revision"] != revision:
            raise ValueError("stale_suggestion_revision")
        field = _POLICY_FIELD[row["kind"]]
        current = SuggestionStrategy.model_validate_json(
            connection.execute("SELECT policy_json FROM suggestion_strategy_versions "
                               "WHERE version=?", (row["strategy_version"],)).fetchone()[0]
        )
        SuggestionStrategy.model_validate({**current.model_dump(), field: proposed_threshold})
        connection.execute(
            "UPDATE proactive_suggestions SET title=?,explanation=?,proposed_threshold=?,"
            "revision=revision+1,updated_at_utc=? WHERE id=?",
            (title, explanation, proposed_threshold, _stamp(datetime.now(timezone.utc)), suggestion_id),
        )
        _audit(connection, "proactive_suggestion_edited", {"suggestion_id": suggestion_id,
                                                       "revision": revision + 1}, "user")
        updated = connection.execute("SELECT * FROM proactive_suggestions WHERE id=?",
                                     (suggestion_id,)).fetchone()
    return _suggestion_row(updated)


def resolve_suggestion(
    repository: Repository, suggestion_id: str, revision: int, accept: bool,
    *, as_of: datetime | None = None,
) -> dict[str, Any]:
    with repository.transaction() as connection:
        row = connection.execute("SELECT * FROM proactive_suggestions WHERE id=?",
                                 (suggestion_id,)).fetchone()
        if row is None:
            raise KeyError(suggestion_id)
        if row["status"] != "pending" or row["revision"] != revision:
            raise ValueError("stale_suggestion_revision")
        active_version, policy = _active_strategy(connection)
        accepted_version = None
        if accept:
            current = next((item for item in _collect(connection, as_of or datetime.now(timezone.utc), policy)
                            if item["dedupe_key"] == row["dedupe_key"]), None)
            if current is None or _json(current["evidence"]) != row["evidence_json"]:
                raise ValueError("stale_suggestion_evidence")
            field = _POLICY_FIELD[row["kind"]]
            next_policy = SuggestionStrategy.model_validate({
                **policy.model_dump(), field: row["proposed_threshold"],
            })
            accepted_version = active_version
            if next_policy != policy:
                accepted_version = connection.execute(
                    "SELECT COALESCE(MAX(version),0)+1 FROM suggestion_strategy_versions"
                ).fetchone()[0]
                connection.execute(
                    "INSERT INTO suggestion_strategy_versions(version,policy_json,parent_version,"
                    "source_suggestion_id,created_at_utc) VALUES (?,?,?,?,?)",
                    (accepted_version, next_policy.model_dump_json(), active_version,
                     suggestion_id, _stamp(datetime.now(timezone.utc))),
                )
                connection.execute(
                    "UPDATE suggestion_strategy_state SET active_version=? WHERE singleton=1",
                    (accepted_version,),
                )
        connection.execute(
            "UPDATE proactive_suggestions SET status=?,revision=revision+1,"
            "accepted_strategy_version=?,updated_at_utc=? WHERE id=?",
            ("accepted" if accept else "rejected", accepted_version,
             _stamp(datetime.now(timezone.utc)), suggestion_id),
        )
        _audit(connection, "proactive_suggestion_accepted" if accept else "proactive_suggestion_rejected",
               {"suggestion_id": suggestion_id, "strategy_version": accepted_version}, "user")
        updated = connection.execute("SELECT * FROM proactive_suggestions WHERE id=?",
                                     (suggestion_id,)).fetchone()
    return _suggestion_row(updated)


def rollback_strategy(
    repository: Repository, target_version: int, expected_active_version: int,
) -> dict[str, Any]:
    with repository.transaction() as connection:
        active_version, _ = _active_strategy(connection)
        if active_version != expected_active_version:
            raise ValueError("stale_strategy_version")
        if target_version == active_version:
            raise ValueError("strategy_already_active")
        target = connection.execute(
            "SELECT policy_json FROM suggestion_strategy_versions WHERE version=?",
            (target_version,),
        ).fetchone()
        if target is None:
            raise KeyError(target_version)
        policy = SuggestionStrategy.model_validate_json(target["policy_json"])
        next_version = connection.execute(
            "SELECT COALESCE(MAX(version),0)+1 FROM suggestion_strategy_versions"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO suggestion_strategy_versions(version,policy_json,parent_version,"
            "rollback_target_version,created_at_utc) VALUES (?,?,?,?,?)",
            (next_version, policy.model_dump_json(), active_version, target_version,
             _stamp(datetime.now(timezone.utc))),
        )
        connection.execute(
            "UPDATE suggestion_strategy_state SET active_version=? WHERE singleton=1",
            (next_version,),
        )
        _audit(connection, "proactive_strategy_rolled_back",
               {"from_version": active_version, "target_version": target_version,
                "new_version": next_version}, "user")
    return current_strategy(repository)
