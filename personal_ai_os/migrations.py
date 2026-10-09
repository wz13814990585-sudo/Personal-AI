"""Transactional schema upgrades for databases created before P1."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4


CURRENT_VERSION = 12


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def backup_database(connection: sqlite3.Connection, path: Path, version: int) -> Path:
    """Use SQLite's snapshot API so a WAL database is backed up consistently."""
    destination = path.with_name(f"{path.stem}.backup-v{version}-{uuid4().hex}{path.suffix}")
    target = sqlite3.connect(destination)
    try:
        connection.backup(target)
    except Exception:
        target.close()
        destination.unlink(missing_ok=True)
        raise
    target.close()
    return destination


def migrate_v1(connection: sqlite3.Connection) -> None:
    """All statements run under the caller's BEGIN IMMEDIATE transaction."""
    if "run_id" not in _columns(connection, "feedback"):
        connection.execute("ALTER TABLE feedback ADD COLUMN run_id TEXT REFERENCES runs(id)")
    if "explanation" not in _columns(connection, "memory_proposals"):
        connection.execute(
            "ALTER TABLE memory_proposals ADD COLUMN explanation TEXT NOT NULL DEFAULT ''"
        )
    for name, declaration in (
        ("domain", "TEXT NOT NULL DEFAULT 'general'"),
        ("recurrence_rule_id", "TEXT"),
        ("version", "INTEGER NOT NULL DEFAULT 1"),
    ):
        if name not in _columns(connection, "tasks"):
            connection.execute(f"ALTER TABLE tasks ADD COLUMN {name} {declaration}")

    statements = (
        """CREATE TABLE recurrence_rules (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, domain TEXT NOT NULL,
            frequency TEXT NOT NULL, weekdays_json TEXT NOT NULL, timezone TEXT NOT NULL,
            start_date TEXT NOT NULL, end_date TEXT, preferred_time TEXT,
            estimated_minutes INTEGER NOT NULL, priority TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active', version INTEGER NOT NULL DEFAULT 1,
            CHECK (domain IN ('general','study','life')),
            CHECK (frequency IN ('daily','weekly_days')),
            CHECK (status IN ('active','paused')),
            CHECK (estimated_minutes > 0)
        )""",
        """CREATE TABLE recurrence_instances (
            id TEXT PRIMARY KEY, rule_id TEXT NOT NULL REFERENCES recurrence_rules(id),
            local_date TEXT NOT NULL, task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
            unassigned_reason TEXT, created_at_utc TEXT NOT NULL,
            UNIQUE(rule_id,local_date)
        )""",
        """CREATE TABLE habits (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, timezone TEXT NOT NULL,
            target_per_week INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'active',
            version INTEGER NOT NULL DEFAULT 1,
            CHECK (status IN ('active','paused')),
            CHECK (target_per_week BETWEEN 1 AND 7)
        )""",
        """CREATE TABLE habit_checkins (
            id TEXT PRIMARY KEY, habit_id TEXT NOT NULL REFERENCES habits(id),
            local_date TEXT NOT NULL, completed INTEGER NOT NULL, note TEXT NOT NULL DEFAULT '',
            updated_at_utc TEXT NOT NULL,
            UNIQUE(habit_id,local_date), CHECK (completed IN (0,1))
        )""",
        "CREATE INDEX idx_recurrence_rule ON recurrence_instances(rule_id,local_date)",
        "CREATE INDEX idx_habit_checkin ON habit_checkins(habit_id,local_date)",
        "CREATE INDEX idx_task_rule ON tasks(recurrence_rule_id)",
    )
    for statement in statements:
        connection.execute(statement)


def migrate_v2(connection: sqlite3.Connection) -> None:
    """Expand the Agent role CHECK without losing user-edited five-role rows."""
    connection.execute("ALTER TABLE agent_configs RENAME TO agent_configs_v1")
    connection.execute("""CREATE TABLE agent_configs (
        role TEXT PRIMARY KEY, description TEXT NOT NULL, instructions TEXT NOT NULL,
        tool_subset_json TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
        CHECK (role IN ('orchestrator','memory','learning','life','schedule','task'))
    )""")
    connection.execute(
        "INSERT INTO agent_configs(role,description,instructions,tool_subset_json,version) "
        "SELECT role,description,instructions,tool_subset_json,version FROM agent_configs_v1"
    )
    connection.execute("DROP TABLE agent_configs_v1")


def migrate_v3(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE daily_plan_proposals (
        id TEXT PRIMARY KEY, local_date TEXT NOT NULL, timezone TEXT NOT NULL,
        trigger_key TEXT NOT NULL UNIQUE, source_run_id TEXT REFERENCES runs(id),
        status TEXT NOT NULL DEFAULT 'draft', revision INTEGER NOT NULL DEFAULT 0,
        baseline_fingerprint TEXT NOT NULL, actions_json TEXT NOT NULL,
        conflicts_json TEXT NOT NULL, result_json TEXT,
        created_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL,
        CHECK (status IN ('draft','committed','rejected'))
    )""")
    connection.execute(
        "CREATE INDEX idx_daily_plan_date ON daily_plan_proposals(local_date,created_at_utc)"
    )


def migrate_v4(connection: sqlite3.Connection) -> None:
    """Durable local worker state, reminders and user-editable daily reviews."""
    for statement in (
        """CREATE TABLE scheduled_jobs (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, dedupe_key TEXT NOT NULL UNIQUE,
            payload_json TEXT NOT NULL, due_at_utc TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', lease_owner TEXT,
            lease_until_utc TEXT, attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT, created_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL,
            CHECK (kind IN ('recurrence_scan','daily_plan','reminder','daily_review')),
            CHECK (status IN ('pending','leased','completed','failed','missed','skipped'))
        )""",
        "CREATE INDEX idx_worker_due ON scheduled_jobs(status,due_at_utc,lease_until_utc)",
        """CREATE TABLE task_reminders (
            task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
            lead_minutes INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
            CHECK (lead_minutes BETWEEN 0 AND 10080), CHECK (enabled IN (0,1))
        )""",
        """CREATE TABLE notifications (
            id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE REFERENCES scheduled_jobs(id),
            task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
            title TEXT NOT NULL, body TEXT NOT NULL, original_due_at_utc TEXT NOT NULL,
            visible_at_utc TEXT NOT NULL, status TEXT NOT NULL,
            system_status TEXT NOT NULL, error TEXT, read_at_utc TEXT,
            CHECK (status IN ('delivered','missed')),
            CHECK (system_status IN ('disabled','unknown','delivered','failed','unsupported','missed'))
        )""",
        "CREATE INDEX idx_notifications_visible ON notifications(visible_at_utc)",
        """CREATE TABLE daily_reviews (
            id TEXT PRIMARY KEY, local_date TEXT NOT NULL, timezone TEXT NOT NULL,
            snapshot_json TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
            revision INTEGER NOT NULL DEFAULT 0, created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL, UNIQUE(local_date,timezone)
        )""",
    ):
        connection.execute(statement)


def migrate_v5(connection: sqlite3.Connection) -> None:
    """Validated read-only checkpoints and measured DeepSeek request usage."""
    connection.execute("ALTER TABLE runs ADD COLUMN retry_of_run_id TEXT REFERENCES runs(id)")
    connection.execute("CREATE INDEX idx_runs_retry_of ON runs(retry_of_run_id)")
    connection.execute("""CREATE TABLE run_checkpoints (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
        step_id TEXT NOT NULL, role TEXT NOT NULL, schema_name TEXT NOT NULL,
        input_fingerprint TEXT NOT NULL, output_json TEXT NOT NULL,
        created_at_utc TEXT NOT NULL, UNIQUE(run_id,step_id)
    )""")
    connection.execute("""CREATE TABLE model_usage (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
        step_id TEXT NOT NULL, attempt INTEGER NOT NULL,
        request_index INTEGER NOT NULL, model_id TEXT NOT NULL,
        input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER,
        duration_ms INTEGER NOT NULL, error_code TEXT, created_at_utc TEXT NOT NULL,
        UNIQUE(run_id,step_id,attempt,request_index),
        CHECK (attempt > 0 AND request_index > 0 AND duration_ms >= 0),
        CHECK (input_tokens IS NULL OR input_tokens >= 0),
        CHECK (output_tokens IS NULL OR output_tokens >= 0),
        CHECK (total_tokens IS NULL OR total_tokens >= 0)
    )""")
    connection.execute("CREATE INDEX idx_model_usage_run ON model_usage(run_id,step_id)")


def migrate_v6(connection: sqlite3.Connection) -> None:
    """User-defined Agent metadata; execution remains a later P2 step."""
    connection.execute("""CREATE TABLE custom_agents (
        id TEXT PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE,
        description TEXT NOT NULL, instructions TEXT NOT NULL,
        tool_subset_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'paused',
        version INTEGER NOT NULL DEFAULT 1,
        created_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL,
        CHECK (status IN ('active','paused')), CHECK (version > 0)
    )""")


def migrate_v7(connection: sqlite3.Connection) -> None:
    """Isolated review proposals and read-only custom Agent run history."""
    for statement in (
        """CREATE TABLE custom_agent_runs (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, agent_id TEXT REFERENCES custom_agents(id),
            agent_version INTEGER, request_text TEXT NOT NULL, status TEXT NOT NULL,
            agent_snapshot_json TEXT, output_json TEXT, error_code TEXT, model_id TEXT NOT NULL,
            started_at_utc TEXT NOT NULL, ended_at_utc TEXT,
            CHECK (kind IN ('proposal','advice')),
            CHECK (status IN ('running','success','failed')),
            CHECK ((kind='proposal' AND agent_id IS NULL AND agent_version IS NULL)
                OR (kind='advice' AND agent_id IS NOT NULL AND agent_version IS NOT NULL))
        )""",
        "CREATE INDEX idx_custom_runs_agent ON custom_agent_runs(agent_id,started_at_utc)",
        """CREATE TABLE custom_agent_proposals (
            id TEXT PRIMARY KEY, source_run_id TEXT NOT NULL UNIQUE REFERENCES custom_agent_runs(id),
            status TEXT NOT NULL DEFAULT 'pending', revision INTEGER NOT NULL DEFAULT 0,
            name TEXT NOT NULL, description TEXT NOT NULL, instructions TEXT NOT NULL,
            suggested_tools_json TEXT NOT NULL, explanation TEXT NOT NULL,
            accepted_agent_id TEXT REFERENCES custom_agents(id),
            created_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL,
            CHECK (status IN ('pending','accepted','rejected')), CHECK (revision >= 0)
        )""",
        """CREATE TABLE custom_agent_trace_events (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES custom_agent_runs(id),
            actor TEXT NOT NULL, event_type TEXT NOT NULL, summary_json TEXT NOT NULL,
            duration_ms INTEGER, created_at_utc TEXT NOT NULL,
            CHECK (actor IN ('user','agent','system'))
        )""",
        "CREATE INDEX idx_custom_trace_run ON custom_agent_trace_events(run_id,created_at_utc)",
        """CREATE TABLE custom_agent_usage (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES custom_agent_runs(id),
            request_index INTEGER NOT NULL, model_id TEXT NOT NULL,
            input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER,
            duration_ms INTEGER NOT NULL, error_code TEXT, created_at_utc TEXT NOT NULL,
            UNIQUE(run_id,request_index), CHECK (request_index > 0 AND duration_ms >= 0),
            CHECK (input_tokens IS NULL OR input_tokens >= 0),
            CHECK (output_tokens IS NULL OR output_tokens >= 0),
            CHECK (total_tokens IS NULL OR total_tokens >= 0)
        )""",
    ):
        connection.execute(statement)


def migrate_v8(connection: sqlite3.Connection) -> None:
    """Reviewable proactive advice and immutable strategy history."""
    for statement in (
        """CREATE TABLE IF NOT EXISTS suggestion_strategy_versions (
            version INTEGER PRIMARY KEY, policy_json TEXT NOT NULL,
            parent_version INTEGER, rollback_target_version INTEGER,
            source_suggestion_id TEXT, created_at_utc TEXT NOT NULL,
            CHECK (version > 0)
        )""",
        """CREATE TABLE IF NOT EXISTS suggestion_strategy_state (
            singleton INTEGER PRIMARY KEY CHECK (singleton=1),
            active_version INTEGER NOT NULL REFERENCES suggestion_strategy_versions(version)
        )""",
        """CREATE TABLE IF NOT EXISTS proactive_suggestions (
            id TEXT PRIMARY KEY, dedupe_key TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL, subject_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', revision INTEGER NOT NULL DEFAULT 0,
            title TEXT NOT NULL, explanation TEXT NOT NULL,
            evidence_json TEXT NOT NULL, source_ids_json TEXT NOT NULL,
            proposed_threshold INTEGER NOT NULL,
            strategy_version INTEGER NOT NULL REFERENCES suggestion_strategy_versions(version),
            accepted_strategy_version INTEGER REFERENCES suggestion_strategy_versions(version),
            generated_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL,
            CHECK (kind IN ('overdue_task','habit_gap','daily_review')),
            CHECK (status IN ('pending','accepted','rejected')),
            CHECK (revision >= 0)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_suggestions_status ON proactive_suggestions(status,generated_at_utc)",
    ):
        connection.execute(statement)
    connection.execute(
        "INSERT OR IGNORE INTO suggestion_strategy_versions(version,policy_json,created_at_utc) "
        "VALUES (1,'{\"overdue_days\":1,\"habit_gap_min\":2,\"review_unfinished_min\":2}',"
        "strftime('%Y-%m-%dT%H:%M:%fZ','now'))"
    )
    connection.execute(
        "INSERT OR IGNORE INTO suggestion_strategy_state(singleton,active_version) VALUES (1,1)"
    )


def migrate_v9(connection: sqlite3.Connection) -> None:
    """Local external capability directory and durable, individually reviewed writes."""
    for statement in (
        """CREATE TABLE IF NOT EXISTS mcp_servers (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'paused', version INTEGER NOT NULL DEFAULT 1,
            created_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL,
            CHECK (kind IN ('calendar','mail','mcp')),
            CHECK (status IN ('active','paused')), CHECK (version > 0)
        )""",
        """CREATE TABLE IF NOT EXISTS mcp_operations (
            server_id TEXT NOT NULL REFERENCES mcp_servers(id),
            operation TEXT NOT NULL, access TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (server_id,operation),
            CHECK (access IN ('read','write')), CHECK (enabled IN (0,1))
        )""",
        """CREATE TABLE IF NOT EXISTS external_write_proposals (
            id TEXT PRIMARY KEY, server_id TEXT NOT NULL REFERENCES mcp_servers(id),
            operation TEXT NOT NULL, account_id TEXT NOT NULL, target TEXT NOT NULL,
            payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            revision INTEGER NOT NULL DEFAULT 0, idempotency_key TEXT NOT NULL UNIQUE,
            result_json TEXT, error_code TEXT, lease_until_utc TEXT, created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL,
            CHECK (status IN ('pending','rejected','attempting','succeeded','failed','timeout','unknown')),
            CHECK (revision >= 0)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_external_proposals_status ON external_write_proposals(status,created_at_utc)",
        """CREATE TABLE IF NOT EXISTS external_audit (
            id TEXT PRIMARY KEY, server_id TEXT, operation TEXT,
            proposal_id TEXT, event_type TEXT NOT NULL, detail_json TEXT NOT NULL,
            created_at_utc TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_external_audit_time ON external_audit(created_at_utc)",
    ):
        connection.execute(statement)


def migrate_v10(connection: sqlite3.Connection) -> None:
    """Device credentials, short-lived view sessions and opt-in delivery ledger."""
    for statement in (
        """CREATE TABLE IF NOT EXISTS remote_devices (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'active', notifications_enabled INTEGER NOT NULL DEFAULT 0,
            notifications_enabled_at_utc TEXT,
            version INTEGER NOT NULL DEFAULT 1, expires_at_utc TEXT NOT NULL,
            created_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL, last_used_at_utc TEXT,
            CHECK (status IN ('active','revoked')), CHECK (notifications_enabled IN (0,1)),
            CHECK (version > 0)
        )""",
        """CREATE TABLE IF NOT EXISTS remote_sessions (
            id TEXT PRIMARY KEY, device_id TEXT NOT NULL REFERENCES remote_devices(id),
            token_hash TEXT NOT NULL UNIQUE, expires_at_utc TEXT NOT NULL,
            created_at_utc TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_remote_session_device ON remote_sessions(device_id)",
        """CREATE TABLE IF NOT EXISTS remote_deliveries (
            id TEXT PRIMARY KEY, notification_id TEXT NOT NULL REFERENCES notifications(id),
            device_id TEXT NOT NULL REFERENCES remote_devices(id),
            status TEXT NOT NULL DEFAULT 'pending', idempotency_key TEXT NOT NULL UNIQUE,
            lease_until_utc TEXT, error_code TEXT, created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL, UNIQUE(notification_id,device_id),
            CHECK (status IN ('pending','attempting','delivered','failed','unknown','missed','cancelled'))
        )""",
        "CREATE INDEX IF NOT EXISTS idx_remote_delivery_status ON remote_deliveries(status,lease_until_utc)",
    ):
        connection.execute(statement)


def migrate_v11(connection: sqlite3.Connection) -> None:
    """Add explicit body-read permission to pre-existing mail server entries."""
    connection.execute(
        "INSERT OR IGNORE INTO mcp_operations(server_id,operation,access,enabled) "
        "SELECT id,'get_message','read',0 FROM mcp_servers WHERE kind='mail'"
    )


def migrate_v12(connection: sqlite3.Connection) -> None:
    """Keep approved learning resource links on formal tasks."""
    for name, declaration in (
        ("resource_url", "TEXT"), ("resource_source", "TEXT"),
        ("resource_channel", "TEXT"), ("resource_duration_seconds", "INTEGER"),
    ):
        if name not in _columns(connection, "tasks"):
            connection.execute(f"ALTER TABLE tasks ADD COLUMN {name} {declaration}")


def migrate(connection: sqlite3.Connection, path: Path, *, existing_user_db: bool) -> None:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version > CURRENT_VERSION:
        raise RuntimeError("database_schema_newer_than_application")
    if version == CURRENT_VERSION:
        return
    if existing_user_db:
        backup_database(connection, path, version)
    connection.execute("BEGIN IMMEDIATE")
    try:
        if version < 1:
            migrate_v1(connection)
        if version < 2:
            migrate_v2(connection)
        if version < 3:
            migrate_v3(connection)
        if version < 4:
            migrate_v4(connection)
        if version < 5:
            migrate_v5(connection)
        if version < 6:
            migrate_v6(connection)
        if version < 7:
            migrate_v7(connection)
        if version < 8:
            migrate_v8(connection)
        if version < 9:
            migrate_v9(connection)
        if version < 10:
            migrate_v10(connection)
        if version < 11:
            migrate_v11(connection)
        if version < 12:
            migrate_v12(connection)
        connection.execute(f"PRAGMA user_version={CURRENT_VERSION}")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
