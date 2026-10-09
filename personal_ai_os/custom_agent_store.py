"""Durable review proposals and isolated read-only custom Agent runs."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .contracts import CustomAgentCreate, CustomAgentProposalOutput
from .storage import Repository


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


class CustomAgentStore:
    def __init__(self, repository: Repository):
        self.repository = repository

    def create_run(
        self, kind: str, request_text: str, model_id: str,
        agent: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if kind not in {"proposal", "advice"} or (kind == "advice") != (agent is not None):
            raise ValueError("invalid_custom_run_kind")
        run_id = uuid4().hex
        with self.repository.transaction() as connection:
            connection.execute(
                "INSERT INTO custom_agent_runs(id,kind,agent_id,agent_version,request_text,"
                "status,agent_snapshot_json,model_id,started_at_utc) "
                "VALUES (?,?,?,?,?,'running',?,?,?)",
                (run_id, kind, agent["id"] if agent else None,
                 agent["version"] if agent else None, request_text,
                 json.dumps(agent, ensure_ascii=False) if agent else None, model_id, _now()),
            )
            self._event(connection, run_id, "run_started", {"kind": kind}, "system")
        return self.get_run(run_id)

    def finish_run(
        self, run_id: str, status: str, *, output: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        if status not in {"success", "failed"}:
            raise ValueError("invalid_custom_run_status")
        with self.repository.transaction() as connection:
            cursor = connection.execute(
                "UPDATE custom_agent_runs SET status=?,output_json=?,error_code=?,ended_at_utc=? "
                "WHERE id=? AND status='running'",
                (status, json.dumps(output, ensure_ascii=False) if output is not None else None,
                 error_code, _now(), run_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("custom_run_not_running")
            self._event(connection, run_id, "run_succeeded" if status == "success" else "run_failed",
                        {"error_code": error_code} if error_code else {},
                        "system")
        return self.get_run(run_id)

    def complete_proposal_run(
        self, run_id: str, output: CustomAgentProposalOutput,
    ) -> dict[str, Any]:
        proposal_id = uuid4().hex
        now = _now()
        with self.repository.transaction() as connection:
            cursor = connection.execute(
                "UPDATE custom_agent_runs SET status='success',output_json=?,ended_at_utc=? "
                "WHERE id=? AND kind='proposal' AND status='running'",
                (output.model_dump_json(), now, run_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("custom_proposal_run_not_running")
            connection.execute(
                "INSERT INTO custom_agent_proposals(id,source_run_id,name,description,"
                "instructions,suggested_tools_json,explanation,created_at_utc,updated_at_utc) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (proposal_id, run_id, output.name, output.description, output.instructions,
                 json.dumps(output.suggested_tools), output.explanation, now, now),
            )
            self._event(connection, run_id, "proposal_ready", {"proposal_id": proposal_id}, "system")
        return self.get_proposal(proposal_id)

    def list_runs(self) -> list[dict[str, Any]]:
        with self.repository.connection() as connection:
            rows = connection.execute("SELECT * FROM custom_agent_runs ORDER BY rowid DESC").fetchall()
        return [self._run_row(row) for row in rows]

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self.repository.connection() as connection:
            row = connection.execute("SELECT * FROM custom_agent_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return self._run_row(row)

    @staticmethod
    def _run_row(row: sqlite3.Row) -> dict[str, Any]:
        result = _row(row)
        raw_output = result.pop("output_json")
        raw_snapshot = result.pop("agent_snapshot_json")
        result["output"] = json.loads(raw_output) if raw_output else None
        result["agent_snapshot"] = json.loads(raw_snapshot) if raw_snapshot else None
        return result

    @staticmethod
    def _proposal_row(row: sqlite3.Row) -> dict[str, Any]:
        result = _row(row)
        result["suggested_tools"] = json.loads(result.pop("suggested_tools_json"))
        return result

    def list_proposals(self, status: str | None = None) -> list[dict[str, Any]]:
        with self.repository.connection() as connection:
            if status is None:
                rows = connection.execute("SELECT * FROM custom_agent_proposals ORDER BY rowid DESC").fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM custom_agent_proposals WHERE status=? ORDER BY rowid DESC",
                    (status,),
                ).fetchall()
        return [self._proposal_row(row) for row in rows]

    def get_proposal(self, proposal_id: str) -> dict[str, Any]:
        with self.repository.connection() as connection:
            row = connection.execute(
                "SELECT * FROM custom_agent_proposals WHERE id=?", (proposal_id,)
            ).fetchone()
        if row is None:
            raise KeyError(proposal_id)
        return self._proposal_row(row)

    def accept_proposal(
        self, proposal_id: str, revision: int, data: CustomAgentCreate,
    ) -> dict[str, Any]:
        agent_id = uuid4().hex
        now = _now()
        try:
            with self.repository.transaction() as connection:
                proposal = connection.execute(
                    "SELECT * FROM custom_agent_proposals WHERE id=?", (proposal_id,)
                ).fetchone()
                if proposal is None:
                    raise KeyError(proposal_id)
                if proposal["status"] != "pending" or proposal["revision"] != revision:
                    raise ValueError("stale_custom_agent_proposal")
                connection.execute(
                    "INSERT INTO custom_agents(id,name,description,instructions,tool_subset_json,"
                    "status,version,created_at_utc,updated_at_utc) "
                    "VALUES (?,?,?,?,?,'paused',1,?,?)",
                    (agent_id, data.name, data.description, data.instructions,
                     json.dumps(sorted(data.tool_subset)), now, now),
                )
                connection.execute(
                    "UPDATE custom_agent_proposals SET status='accepted',revision=revision+1,"
                    "accepted_agent_id=?,updated_at_utc=? WHERE id=?",
                    (agent_id, now, proposal_id),
                )
                self._event(connection, proposal["source_run_id"], "proposal_accepted",
                            {"proposal_id": proposal_id, "agent_id": agent_id}, "user")
        except sqlite3.IntegrityError as exc:
            raise ValueError("duplicate_custom_agent_name") from exc
        return self.repository.get_custom_agent(agent_id)

    def reject_proposal(self, proposal_id: str, revision: int) -> dict[str, Any]:
        with self.repository.transaction() as connection:
            proposal = connection.execute(
                "SELECT * FROM custom_agent_proposals WHERE id=?", (proposal_id,)
            ).fetchone()
            if proposal is None:
                raise KeyError(proposal_id)
            if proposal["status"] != "pending" or proposal["revision"] != revision:
                raise ValueError("stale_custom_agent_proposal")
            connection.execute(
                "UPDATE custom_agent_proposals SET status='rejected',revision=revision+1,"
                "updated_at_utc=? WHERE id=?", (_now(), proposal_id),
            )
            self._event(connection, proposal["source_run_id"], "proposal_rejected",
                        {"proposal_id": proposal_id}, "user")
        return self.get_proposal(proposal_id)

    @staticmethod
    def _event(
        connection: sqlite3.Connection, run_id: str, event_type: str,
        summary: dict[str, Any], actor: str, duration_ms: int | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO custom_agent_trace_events(id,run_id,actor,event_type,summary_json,"
            "duration_ms,created_at_utc) VALUES (?,?,?,?,?,?,?)",
            (uuid4().hex, run_id, actor, event_type,
             json.dumps(summary, ensure_ascii=False), duration_ms, _now()),
        )

    def append_event(
        self, run_id: str, event_type: str, summary: dict[str, Any],
        *, actor: str = "system", duration_ms: int | None = None,
    ) -> None:
        with self.repository.transaction() as connection:
            self._event(connection, run_id, event_type, summary, actor, duration_ms)

    def list_events(self, run_id: str) -> list[dict[str, Any]]:
        with self.repository.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM custom_agent_trace_events WHERE run_id=? "
                "ORDER BY created_at_utc,rowid", (run_id,),
            ).fetchall()
        return [_row(row) for row in rows]

    def record_usage(
        self, run_id: str, requests: list[dict[str, Any]], error_code: str | None,
    ) -> None:
        with self.repository.transaction() as connection:
            for index, request in enumerate(requests, start=1):
                connection.execute(
                    "INSERT INTO custom_agent_usage(id,run_id,request_index,model_id,"
                    "input_tokens,output_tokens,total_tokens,duration_ms,error_code,created_at_utc) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (uuid4().hex, run_id, index, request["model_id"],
                     request.get("input_tokens"), request.get("output_tokens"),
                     request.get("total_tokens"), request["duration_ms"], error_code, _now()),
                )

    def list_usage(self, run_id: str | None = None) -> list[dict[str, Any]]:
        with self.repository.connection() as connection:
            if run_id is None:
                rows = connection.execute("SELECT * FROM custom_agent_usage ORDER BY rowid").fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM custom_agent_usage WHERE run_id=? ORDER BY request_index",
                    (run_id,),
                ).fetchall()
        return [_row(row) for row in rows]

    def fail_interrupted_runs(self) -> int:
        with self.repository.transaction() as connection:
            rows = connection.execute(
                "SELECT id FROM custom_agent_runs WHERE status='running'"
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE custom_agent_runs SET status='failed',error_code='interrupted',"
                    "ended_at_utc=? WHERE id=?", (_now(), row["id"]),
                )
                self._event(connection, row["id"], "run_failed",
                            {"error_code": "interrupted"}, "system")
        return len(rows)
