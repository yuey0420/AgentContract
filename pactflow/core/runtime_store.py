import json
import os
import sqlite3
import uuid
import hashlib
import fnmatch
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import RUNTIME_DB_PATH, TASKS_FILE


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_action_args(args: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in args.items():
        lowered = key.lower()
        if any(marker in lowered for marker in ("key", "token", "secret", "password", "credential")):
            safe[key] = "[REDACTED]"
        elif key in {"content", "new_content"} and isinstance(value, str):
            safe[key] = {"redacted": True, "length": len(value)}
        else:
            safe[key] = value
    return safe


class RuntimeStore:
    """Transactional store for scheduled work, approvals, runs, and evidence."""

    def __init__(self, db_path: str = RUNTIME_DB_PATH, legacy_tasks_file: str | None = TASKS_FILE):
        self.db_path = db_path
        self.legacy_tasks_file = legacy_tasks_file
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._initialize()
        if legacy_tasks_file:
            self.migrate_legacy_tasks(legacy_tasks_file)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def _initialize(self):
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runtime_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scheduled_tasks (
                    id TEXT PRIMARY KEY,
                    target_time TEXT NOT NULL,
                    description TEXT NOT NULL,
                    repeat TEXT,
                    repeat_count INTEGER,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_due
                    ON scheduled_tasks(status, target_time);
                CREATE TABLE IF NOT EXISTS contract_approvals (
                    contract_id TEXT NOT NULL,
                    contract_hash TEXT NOT NULL,
                    approved_by TEXT NOT NULL,
                    approved_at TEXT NOT NULL,
                    revoked_at TEXT,
                    PRIMARY KEY(contract_id, contract_hash)
                );
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    objective TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    status TEXT NOT NULL,
                    contract_id TEXT,
                    contract_hash TEXT,
                    task_id TEXT,
                    task_version TEXT,
                    plan_version TEXT,
                    policy_version TEXT,
                    plan_acknowledged INTEGER NOT NULL DEFAULT 0,
                    plan_acknowledged_by TEXT,
                    plan_acknowledged_at TEXT,
                    acceptance_decision TEXT,
                    acceptance_decided_by TEXT,
                    acceptance_decided_at TEXT,
                    closure_result TEXT,
                    closed_by TEXT,
                    closed_at TEXT,
                    effective_policy_json TEXT,
                    reconciliation_json TEXT,
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_runs_thread ON runs(thread_id, started_at);
                CREATE TABLE IF NOT EXISTS run_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    event TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );
                CREATE INDEX IF NOT EXISTS idx_run_events_run ON run_events(run_id, id);
                CREATE TABLE IF NOT EXISTS task_nodes (
                    node_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    parent_node_id TEXT,
                    title TEXT NOT NULL,
                    objective TEXT NOT NULL,
                    sequence INTEGER NOT NULL DEFAULT 0,
                    depends_on_json TEXT NOT NULL DEFAULT '[]',
                    acceptance_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'pending',
                    input_json TEXT NOT NULL DEFAULT '{}',
                    output_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT,
                    started_at TEXT,
                    completed_at TEXT,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );
                CREATE INDEX IF NOT EXISTS idx_task_nodes_run
                    ON task_nodes(run_id, sequence, node_id);
                CREATE TABLE IF NOT EXISTS run_baselines (
                    baseline_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    backend TEXT NOT NULL,
                    root_path TEXT NOT NULL,
                    revision TEXT,
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );
                CREATE INDEX IF NOT EXISTS idx_run_baselines_run ON run_baselines(run_id);
                CREATE TABLE IF NOT EXISTS run_checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    node_id TEXT,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    evidence_level TEXT NOT NULL,
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );
                CREATE INDEX IF NOT EXISTS idx_run_checkpoints_run ON run_checkpoints(run_id, created_at);
                CREATE TABLE IF NOT EXISTS run_repositories (
                    repository_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    path TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'primary',
                    revision TEXT,
                    parent_repository_id TEXT,
                    dependency_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );
                CREATE INDEX IF NOT EXISTS idx_run_repositories_run ON run_repositories(run_id, name);
                CREATE TABLE IF NOT EXISTS instructions (
                    instruction_id TEXT PRIMARY KEY,
                    run_id TEXT,
                    thread_id TEXT NOT NULL,
                    tool_call_id TEXT NOT NULL,
                    parent_instruction_id TEXT,
                    tool_name TEXT NOT NULL,
                    capability TEXT NOT NULL,
                    resource TEXT,
                    args_json TEXT NOT NULL,
                    reference_tool_ids_json TEXT NOT NULL,
                    trustworthiness TEXT NOT NULL,
                    confidentiality TEXT NOT NULL,
                    risk_level TEXT NOT NULL,
                    authority TEXT NOT NULL,
                    governance_mode TEXT NOT NULL,
                    policy_decision TEXT NOT NULL DEFAULT 'allow',
                    status TEXT NOT NULL DEFAULT 'requested',
                    result_trustworthiness TEXT,
                    result_confidentiality TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_instructions_run_tool_call
                    ON instructions(run_id, tool_call_id);
                CREATE INDEX IF NOT EXISTS idx_instructions_run
                    ON instructions(run_id, created_at);
                CREATE TABLE IF NOT EXISTS pending_actions (
                    action_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    args_json TEXT NOT NULL,
                    action_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    approved_by TEXT,
                    approved_at TEXT,
                    consumed_at TEXT,
                    tool_call_id TEXT,
                    risk_level TEXT,
                    clause TEXT,
                    reason TEXT,
                    rejected_by TEXT,
                    rejected_at TEXT,
                    expired_at TEXT
                );
                CREATE TABLE IF NOT EXISTS approval_grants (
                    grant_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    contract_hash TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    capability TEXT NOT NULL,
                    resource_pattern TEXT NOT NULL,
                    max_uses INTEGER NOT NULL,
                    uses INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT,
                    approved_by TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );
                CREATE INDEX IF NOT EXISTS idx_approval_grants_match
                    ON approval_grants(run_id, thread_id, contract_hash, tool_name, capability);
                """
            )
            existing_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(pending_actions)").fetchall()
            }
            migrations = {
                "tool_call_id": "TEXT",
                "risk_level": "TEXT",
                "clause": "TEXT",
                "reason": "TEXT",
                "rejected_by": "TEXT",
                "rejected_at": "TEXT",
                "expired_at": "TEXT",
                "contract_hash": "TEXT",
                "capability": "TEXT",
                "resource": "TEXT",
                "process_profile": "TEXT",
                "effect": "TEXT",
            }
            for column, column_type in migrations.items():
                if column not in existing_columns:
                    conn.execute(f"ALTER TABLE pending_actions ADD COLUMN {column} {column_type}")

            existing_run_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(runs)").fetchall()
            }
            run_migrations = {
                "task_id": "TEXT",
                "task_version": "TEXT",
                "plan_version": "TEXT",
                "policy_version": "TEXT",
                "plan_acknowledged": "INTEGER NOT NULL DEFAULT 0",
                "plan_acknowledged_by": "TEXT",
                "plan_acknowledged_at": "TEXT",
                "acceptance_decision": "TEXT",
                "acceptance_decided_by": "TEXT",
                "acceptance_decided_at": "TEXT",
                "closure_result": "TEXT",
                "closed_by": "TEXT",
                "closed_at": "TEXT",
                "effective_policy_json": "TEXT",
                "reconciliation_json": "TEXT",
            }
            for column, column_type in run_migrations.items():
                if column not in existing_run_columns:
                    conn.execute(f"ALTER TABLE runs ADD COLUMN {column} {column_type}")

    def migrate_legacy_tasks(self, tasks_file: str):
        with self._connect() as conn:
            migrated = conn.execute(
                "SELECT value FROM runtime_meta WHERE key = 'legacy_tasks_migrated'"
            ).fetchone()
            if migrated:
                return

            tasks: list[dict[str, Any]] = []
            if os.path.exists(tasks_file):
                try:
                    with open(tasks_file, "r", encoding="utf-8") as file:
                        tasks = json.load(file) or []
                except (OSError, json.JSONDecodeError, TypeError):
                    tasks = []

            now = _utc_now()
            for task in tasks:
                if not task.get("target_time") or not task.get("description"):
                    continue
                conn.execute(
                    """
                    INSERT OR IGNORE INTO scheduled_tasks
                    (id, target_time, description, repeat, repeat_count, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        str(task.get("id") or uuid.uuid4().hex[:8]),
                        str(task["target_time"]),
                        str(task["description"]),
                        task.get("repeat"),
                        task.get("repeat_count"),
                        now,
                        now,
                    ),
                )
            conn.execute(
                "INSERT OR REPLACE INTO runtime_meta(key, value) VALUES('legacy_tasks_migrated', ?)",
                (now,),
            )

    def create_scheduled_task(
        self,
        target_time: str,
        description: str,
        repeat: str | None = None,
        repeat_count: int | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        task_id = task_id or uuid.uuid4().hex[:8]
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO scheduled_tasks
                (id, target_time, description, repeat, repeat_count, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (task_id, target_time, description, repeat, repeat_count, now, now),
            )
        return {
            "id": task_id,
            "target_time": target_time,
            "description": description,
            "repeat": repeat,
            "repeat_count": repeat_count,
        }

    def list_scheduled_tasks(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, target_time, description, repeat, repeat_count
                FROM scheduled_tasks WHERE status = 'pending'
                ORDER BY target_time, id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_scheduled_task(self, task_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE scheduled_tasks SET status = 'cancelled', updated_at = ? WHERE id = ? AND status = 'pending'",
                (_utc_now(), task_id),
            )
        return cursor.rowcount == 1

    def modify_scheduled_task(
        self,
        task_id: str,
        new_time: str | None = None,
        new_description: str | None = None,
    ) -> bool:
        updates = ["updated_at = ?"]
        values: list[Any] = [_utc_now()]
        if new_time is not None:
            updates.append("target_time = ?")
            values.append(new_time)
        if new_description is not None:
            updates.append("description = ?")
            values.append(new_description)
        values.extend([task_id])
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE scheduled_tasks SET {', '.join(updates)} WHERE id = ? AND status = 'pending'",
                values,
            )
        return cursor.rowcount == 1

    @staticmethod
    def _next_time(target: datetime, repeat: str, now: datetime) -> datetime:
        delta = {
            "hourly": timedelta(hours=1),
            "daily": timedelta(days=1),
            "weekly": timedelta(days=7),
        }[repeat]
        next_time = target + delta
        while next_time <= now:
            next_time += delta
        return next_time

    def claim_due_tasks(self, now: datetime | None = None) -> list[dict[str, Any]]:
        now = now or datetime.now()
        now_text = now.strftime("%Y-%m-%d %H:%M:%S")
        triggered: list[dict[str, Any]] = []
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT id, target_time, description, repeat, repeat_count
                FROM scheduled_tasks
                WHERE status = 'pending' AND target_time <= ?
                ORDER BY target_time, id
                """,
                (now_text,),
            ).fetchall()
            for row in rows:
                task = dict(row)
                triggered.append(task)
                repeat = task.get("repeat")
                repeat_count = task.get("repeat_count")
                if repeat in {"hourly", "daily", "weekly"} and (repeat_count is None or repeat_count > 1):
                    target = datetime.strptime(task["target_time"], "%Y-%m-%d %H:%M:%S")
                    next_time = self._next_time(target, repeat, now).strftime("%Y-%m-%d %H:%M:%S")
                    next_count = repeat_count - 1 if repeat_count is not None else None
                    conn.execute(
                        "UPDATE scheduled_tasks SET target_time = ?, repeat_count = ?, updated_at = ? WHERE id = ?",
                        (next_time, next_count, _utc_now(), task["id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE scheduled_tasks SET status = 'completed', updated_at = ? WHERE id = ?",
                        (_utc_now(), task["id"]),
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return triggered

    def record_contract_approval(
        self,
        contract_id: str,
        contract_hash: str,
        approved_by: str,
        approved_at: str | None = None,
    ):
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO contract_approvals
                (contract_id, contract_hash, approved_by, approved_at, revoked_at)
                VALUES (?, ?, ?, ?, NULL)
                """,
                (contract_id, contract_hash, approved_by, approved_at or _utc_now()),
            )

    def has_contract_approval(self, contract_id: str, contract_hash: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM contract_approvals
                WHERE contract_id = ? AND contract_hash = ? AND revoked_at IS NULL
                """,
                (contract_id, contract_hash),
            ).fetchone()
        return row is not None

    def start_run(
        self,
        thread_id: str,
        objective: str,
        mode: str,
        contract_id: str | None = None,
        contract_hash: str | None = None,
        *,
        task_id: str | None = None,
        task_version: str | None = None,
        plan_version: str | None = None,
        policy_version: str | None = None,
        effective_policy: dict[str, Any] | None = None,
    ) -> str:
        run_id = uuid.uuid4().hex
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs
                (run_id, thread_id, objective, mode, phase, status, contract_id, contract_hash,
                 task_id, task_version, plan_version, policy_version, effective_policy_json,
                 started_at, updated_at)
                VALUES (?, ?, ?, ?, 'prepare', 'running', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id, thread_id, objective, mode, contract_id, contract_hash,
                    task_id, task_version, plan_version, policy_version,
                    json.dumps(effective_policy or {}, ensure_ascii=False, sort_keys=True, default=str),
                    now, now,
                ),
            )
        return run_id

    def update_run(self, run_id: str, *, phase: str, status: str, error: str | None = None):
        completed_at = _utc_now() if status in {"completed", "failed", "cancelled"} else None
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE runs SET phase = ?, status = ?, error = ?, updated_at = ?, completed_at = COALESCE(?, completed_at)
                WHERE run_id = ?
                """,
                (phase, status, error, _utc_now(), completed_at, run_id),
            )

    def update_run_mode(self, run_id: str, mode: str):
        with self._connect() as conn:
            conn.execute(
                "UPDATE runs SET mode = ?, updated_at = ? WHERE run_id = ?",
                (mode, _utc_now(), run_id),
            )

    def set_run_reconciliation(self, run_id: str, reconciliation: dict[str, Any]) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE runs SET reconciliation_json = ?, updated_at = ? WHERE run_id = ?",
                (json.dumps(reconciliation, ensure_ascii=False, default=str), _utc_now(), run_id),
            )
        return cursor.rowcount == 1

    def get_run_reconciliation(self, run_id: str) -> dict[str, Any] | None:
        run = self.get_run(run_id)
        if not run or not run.get("reconciliation_json"):
            return None
        try:
            return json.loads(run["reconciliation_json"])
        except (TypeError, json.JSONDecodeError):
            return None

    def get_effective_policy(self, run_id: str) -> dict[str, Any] | None:
        run = self.get_run(run_id)
        if not run or not run.get("effective_policy_json"):
            return None
        try:
            return json.loads(run["effective_policy_json"])
        except (TypeError, json.JSONDecodeError):
            return None

    def create_task_nodes(self, run_id: str, nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        now = _utc_now()
        with self._connect() as conn:
            for node in nodes:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO task_nodes
                    (node_id, run_id, parent_node_id, title, objective, sequence,
                     depends_on_json, acceptance_json, status, input_json, output_json,
                     error, started_at, completed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(node["node_id"]), run_id, node.get("parent_node_id"),
                        str(node.get("title") or node.get("objective") or "task"),
                        str(node.get("objective") or ""), int(node.get("sequence", 0)),
                        json.dumps(node.get("depends_on") or [], ensure_ascii=False),
                        json.dumps(node.get("acceptance") or [], ensure_ascii=False, default=str),
                        str(node.get("status") or "pending"),
                        json.dumps(node.get("input") or {}, ensure_ascii=False, default=str),
                        json.dumps(node.get("output") or {}, ensure_ascii=False, default=str),
                        node.get("error"), node.get("started_at"), node.get("completed_at"),
                    ),
                )
        return self.list_task_nodes(run_id)

    def list_task_nodes(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM task_nodes WHERE run_id = ? ORDER BY sequence, node_id",
                (run_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            for field in ("depends_on_json", "acceptance_json", "input_json", "output_json"):
                try:
                    item[field.removesuffix("_json")] = json.loads(item.pop(field))
                except (TypeError, json.JSONDecodeError):
                    item[field.removesuffix("_json")] = [] if field != "input_json" and field != "output_json" else {}
            result.append(item)
        return result

    def update_task_node(
        self,
        node_id: str,
        *,
        status: str,
        output: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> bool:
        now = _utc_now()
        completed_at = now if status in {"completed", "failed", "cancelled"} else None
        started_at = now if status == "running" else None
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE task_nodes SET status = ?, output_json = COALESCE(?, output_json),
                    error = ?, started_at = COALESCE(?, started_at),
                    completed_at = COALESCE(?, completed_at)
                WHERE node_id = ?
                """,
                (
                    status,
                    json.dumps(output, ensure_ascii=False, default=str) if output is not None else None,
                    error, started_at, completed_at, node_id,
                ),
            )
        return cursor.rowcount == 1

    def create_baseline(
        self,
        run_id: str,
        snapshot: dict[str, Any],
        *,
        backend: str = "filesystem",
        root_path: str = "",
        revision: str | None = None,
        baseline_id: str | None = None,
    ) -> str:
        baseline_id = baseline_id or uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO run_baselines
                (baseline_id, run_id, backend, root_path, revision, snapshot_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (baseline_id, run_id, backend, root_path, revision,
                 json.dumps(snapshot, ensure_ascii=False, default=str), _utc_now()),
            )
        return baseline_id

    def get_baseline(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM run_baselines WHERE run_id = ? ORDER BY created_at DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        try:
            item["snapshot"] = json.loads(item.pop("snapshot_json"))
        except (TypeError, json.JSONDecodeError):
            item["snapshot"] = {}
        return item

    def create_checkpoint(
        self,
        run_id: str,
        kind: str,
        status: str,
        evidence_level: str,
        evidence: dict[str, Any] | None = None,
        node_id: str | None = None,
    ) -> str:
        checkpoint_id = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO run_checkpoints
                (checkpoint_id, run_id, node_id, kind, status, evidence_level, evidence_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (checkpoint_id, run_id, node_id, kind, status, evidence_level,
                 json.dumps(evidence or {}, ensure_ascii=False, default=str), _utc_now()),
            )
        return checkpoint_id

    def list_checkpoints(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM run_checkpoints WHERE run_id = ? ORDER BY created_at, checkpoint_id",
                (run_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["evidence"] = json.loads(item.pop("evidence_json"))
            except (TypeError, json.JSONDecodeError):
                item["evidence"] = {}
            result.append(item)
        return result

    def register_repository(
        self,
        run_id: str,
        name: str,
        path: str,
        *,
        role: str = "primary",
        revision: str | None = None,
        parent_repository_id: str | None = None,
        dependency: dict[str, Any] | None = None,
    ) -> str:
        repository_id = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO run_repositories
                (repository_id, run_id, name, path, role, revision, parent_repository_id, dependency_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (repository_id, run_id, name, path, role, revision, parent_repository_id,
                 json.dumps(dependency or {}, ensure_ascii=False, default=str), _utc_now()),
            )
        return repository_id

    def list_repositories(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM run_repositories WHERE run_id = ? ORDER BY name, repository_id",
                (run_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["dependency"] = json.loads(item.pop("dependency_json"))
            except (TypeError, json.JSONDecodeError):
                item["dependency"] = {}
            result.append(item)
        return result

    def acknowledge_plan(
        self,
        run_id: str,
        acknowledged_by: str = "runtime",
        plan_version: str | None = None,
    ) -> bool:
        """Record the plan gate without changing the existing tool execution API."""
        now = _utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE runs
                SET phase = 'execute', plan_acknowledged = 1,
                    plan_acknowledged_by = ?, plan_acknowledged_at = ?,
                    plan_version = COALESCE(?, plan_version), updated_at = ?
                WHERE run_id = ? AND status = 'running' AND plan_acknowledged = 0
                """,
                (acknowledged_by, now, plan_version, now, run_id),
            )
            changed = cursor.rowcount == 1
        if changed:
            self.append_run_event(run_id, "plan_acknowledged", {
                "acknowledged_by": acknowledged_by,
                "plan_version": plan_version,
            })
        return changed

    def decide_acceptance(
        self,
        run_id: str,
        decision: str,
        decided_by: str = "runtime",
        reason: str = "",
    ) -> bool:
        if decision not in {"accepted", "rejected", "needs_review", "waived"}:
            raise ValueError(f"Unsupported acceptance decision: {decision}")
        now = _utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE runs
                SET phase = 'acceptance', acceptance_decision = ?,
                    acceptance_decided_by = ?, acceptance_decided_at = ?, updated_at = ?
                WHERE run_id = ? AND status = 'running' AND acceptance_decision IS NULL
                """,
                (decision, decided_by, now, now, run_id),
            )
            changed = cursor.rowcount == 1
        if changed:
            self.append_run_event(run_id, "acceptance_decided", {
                "decision": decision,
                "decided_by": decided_by,
                "reason": reason,
            })
        return changed

    def close_run(self, run_id: str, closed_by: str = "runtime", result: str = "accepted") -> bool:
        if result not in {"accepted", "rejected", "needs_review", "cancelled"}:
            raise ValueError(f"Unsupported closure result: {result}")
        now = _utc_now()
        status = "completed" if result == "accepted" else ("cancelled" if result == "cancelled" else "needs_review")
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE runs
                SET phase = 'finalize', status = ?, closure_result = ?,
                    closed_by = ?, closed_at = ?, updated_at = ?,
                    completed_at = CASE WHEN ? IN ('completed', 'cancelled') THEN ? ELSE completed_at END
                WHERE run_id = ? AND status = 'running'
                  AND (acceptance_decision IS NOT NULL OR ? = 'cancelled')
                """,
                (status, result, closed_by, now, now, status, now, run_id, result),
            )
            changed = cursor.rowcount == 1
        if changed:
            self.append_run_event(run_id, "run_closed", {
                "result": result,
                "closed_by": closed_by,
            })
        return changed

    def append_run_event(self, run_id: str, event: str, payload: dict[str, Any] | None = None):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO run_events(run_id, ts, event, payload_json) VALUES (?, ?, ?, ?)",
                (run_id, _utc_now(), event, json.dumps(payload or {}, ensure_ascii=False, default=str)),
            )

    def get_run_events(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT ts, event, payload_json FROM run_events WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
        return [
            {"ts": row["ts"], "event": row["event"], **json.loads(row["payload_json"])}
            for row in rows
        ]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def record_instruction(self, instruction: dict[str, Any]) -> str:
        instruction_id = str(instruction["instruction_id"])
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO instructions
                (instruction_id, run_id, thread_id, tool_call_id, parent_instruction_id,
                 tool_name, capability, resource, args_json, reference_tool_ids_json,
                 trustworthiness, confidentiality, risk_level, authority, governance_mode,
                 policy_decision, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'allow', 'requested', ?, ?)
                ON CONFLICT(instruction_id) DO UPDATE SET
                    parent_instruction_id = excluded.parent_instruction_id,
                    args_json = excluded.args_json,
                    reference_tool_ids_json = excluded.reference_tool_ids_json,
                    trustworthiness = excluded.trustworthiness,
                    confidentiality = excluded.confidentiality,
                    risk_level = excluded.risk_level,
                    authority = excluded.authority,
                    governance_mode = excluded.governance_mode,
                    updated_at = excluded.updated_at
                """,
                (
                    instruction_id,
                    instruction.get("run_id"),
                    str(instruction["thread_id"]),
                    str(instruction["tool_call_id"]),
                    instruction.get("parent_instruction_id"),
                    str(instruction["tool_name"]),
                    str(instruction["capability"]),
                    instruction.get("resource"),
                    json.dumps(
                        _safe_action_args(dict(instruction.get("args") or {})),
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    ),
                    json.dumps(instruction.get("reference_tool_ids") or []),
                    str(instruction.get("trustworthiness") or "trusted"),
                    str(instruction.get("confidentiality") or "public"),
                    str(instruction.get("risk_level") or "low"),
                    str(instruction.get("authority") or "model"),
                    str(instruction.get("governance_mode") or "enforce"),
                    now,
                    now,
                ),
            )
        return instruction_id

    def update_instruction(
        self,
        instruction_id: str,
        *,
        status: str,
        policy_decision: str | None = None,
        result_trustworthiness: str | None = None,
        result_confidentiality: str | None = None,
    ):
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE instructions
                SET status = ?,
                    policy_decision = COALESCE(?, policy_decision),
                    result_trustworthiness = COALESCE(?, result_trustworthiness),
                    result_confidentiality = COALESCE(?, result_confidentiality),
                    updated_at = ?
                WHERE instruction_id = ?
                """,
                (
                    status,
                    policy_decision,
                    result_trustworthiness,
                    result_confidentiality,
                    _utc_now(),
                    instruction_id,
                ),
            )

    def get_run_instructions(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM instructions WHERE run_id = ? ORDER BY created_at, instruction_id",
                (run_id,),
            ).fetchall()
        instructions: list[dict[str, Any]] = []
        for row in rows:
            instruction = dict(row)
            instruction["args"] = json.loads(instruction.pop("args_json"))
            instruction["reference_tool_ids"] = json.loads(
                instruction.pop("reference_tool_ids_json")
            )
            instructions.append(instruction)
        return instructions

    @staticmethod
    def action_hash(tool_name: str, args: dict[str, Any], contract_hash: str | None) -> str:
        payload = json.dumps(
            {"tool": tool_name, "args": args, "contract_hash": contract_hash},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def create_pending_action(
        self,
        run_id: str,
        thread_id: str,
        tool_name: str,
        args: dict[str, Any],
        contract_hash: str | None,
        ttl_minutes: int = 15,
        *,
        tool_call_id: str | None = None,
        risk_level: str | None = None,
        clause: str | None = None,
        reason: str | None = None,
        capability: str | None = None,
        resource: str | None = None,
        process_profile: str | None = None,
        effect: str | None = None,
    ) -> str:
        if tool_call_id:
            with self._connect() as conn:
                existing = conn.execute(
                    """
                    SELECT action_id FROM pending_actions
                    WHERE run_id = ? AND tool_call_id = ?
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (run_id, tool_call_id),
                ).fetchone()
            if existing:
                return str(existing["action_id"])

        action_id = uuid.uuid4().hex[:12]
        created = datetime.now(timezone.utc)
        expires = created + timedelta(minutes=ttl_minutes)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pending_actions
                (action_id, run_id, thread_id, tool_name, args_json, action_hash, status, created_at, expires_at,
                 tool_call_id, risk_level, clause, reason, contract_hash, capability, resource,
                 process_profile, effect)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action_id,
                    run_id,
                    thread_id,
                    tool_name,
                    json.dumps(_safe_action_args(args), ensure_ascii=False, sort_keys=True, default=str),
                    self.action_hash(tool_name, args, contract_hash),
                    created.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    tool_call_id,
                    risk_level,
                    clause,
                    reason,
                    contract_hash,
                    capability,
                    resource,
                    process_profile,
                    effect,
                ),
            )
        return action_id

    def create_approval_grant(
        self,
        *,
        run_id: str,
        thread_id: str,
        contract_hash: str,
        tool_name: str,
        capability: str,
        resource_pattern: str,
        approved_by: str,
        ttl_minutes: int = 15,
        max_uses: int = 20,
    ) -> dict[str, Any]:
        grant_id = uuid.uuid4().hex[:12]
        created = datetime.now(timezone.utc)
        expires = created + timedelta(minutes=ttl_minutes)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO approval_grants
                (grant_id, run_id, thread_id, contract_hash, tool_name, capability,
                 resource_pattern, max_uses, uses, created_at, expires_at, approved_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    grant_id, run_id, thread_id, contract_hash, tool_name, capability,
                    resource_pattern, max(1, max_uses),
                    created.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    expires.strftime("%Y-%m-%dT%H:%M:%SZ"), approved_by,
                ),
            )
            conn.execute(
                "INSERT INTO run_events(run_id, ts, event, payload_json) VALUES (?, ?, 'approval_grant_created', ?)",
                (
                    run_id, _utc_now(), json.dumps({
                        "grant_id": grant_id,
                        "tool": tool_name,
                        "capability": capability,
                        "resource_pattern": resource_pattern,
                        "max_uses": max(1, max_uses),
                    }, ensure_ascii=False),
                ),
            )
        return self.get_approval_grant(grant_id) or {"grant_id": grant_id}

    def get_approval_grant(self, grant_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM approval_grants WHERE grant_id = ?", (grant_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_approval_grants(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM approval_grants WHERE run_id = ? ORDER BY created_at, grant_id",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def revoke_approval_grant(self, grant_id: str) -> bool:
        now = _utc_now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT run_id FROM approval_grants WHERE grant_id = ? AND revoked_at IS NULL",
                (grant_id,),
            ).fetchone()
            if not row:
                return False
            cursor = conn.execute(
                "UPDATE approval_grants SET revoked_at = ? WHERE grant_id = ? AND revoked_at IS NULL",
                (now, grant_id),
            )
            if cursor.rowcount == 1:
                conn.execute(
                    "INSERT INTO run_events(run_id, ts, event, payload_json) VALUES (?, ?, 'approval_grant_revoked', ?)",
                    (row["run_id"], now, json.dumps({"grant_id": grant_id})),
                )
        return cursor.rowcount == 1

    def consume_matching_approval_grant(
        self,
        *,
        run_id: str,
        thread_id: str,
        contract_hash: str,
        tool_name: str,
        capability: str,
        resource: str | None,
    ) -> dict[str, Any] | None:
        now = _utc_now()
        normalized_resource = (resource or "").replace("\\", "/")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT * FROM approval_grants
                WHERE run_id = ? AND thread_id = ? AND contract_hash = ?
                  AND tool_name = ? AND capability = ? AND revoked_at IS NULL
                  AND expires_at > ? AND uses < max_uses
                ORDER BY created_at, grant_id
                """,
                (run_id, thread_id, contract_hash, tool_name, capability, now),
            ).fetchall()
            row = next((
                candidate for candidate in rows
                if fnmatch.fnmatch(normalized_resource, candidate["resource_pattern"])
            ), None)
            if row is None:
                conn.rollback()
                return None
            cursor = conn.execute(
                """
                UPDATE approval_grants SET uses = uses + 1
                WHERE grant_id = ? AND revoked_at IS NULL AND expires_at > ? AND uses < max_uses
                """,
                (row["grant_id"], now),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                return None
            conn.execute(
                "INSERT INTO run_events(run_id, ts, event, payload_json) VALUES (?, ?, 'approval_grant_consumed', ?)",
                (
                    run_id, now, json.dumps({
                        "grant_id": row["grant_id"], "tool": tool_name,
                        "capability": capability, "resource": resource,
                    }, ensure_ascii=False),
                ),
            )
            conn.commit()
            result = dict(row)
            result["uses"] = int(result["uses"]) + 1
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_action(self, action_id: str) -> dict[str, Any] | None:
        self.expire_actions(action_id=action_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pending_actions WHERE action_id = ?", (action_id,)
            ).fetchone()
        if row is None:
            return None
        action = dict(row)
        action["args"] = json.loads(action.pop("args_json"))
        return action

    def list_run_actions(self, run_id: str) -> list[dict[str, Any]]:
        self.expire_actions(run_id=run_id)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pending_actions WHERE run_id = ? ORDER BY created_at",
                (run_id,),
            ).fetchall()
        actions = []
        for row in rows:
            action = dict(row)
            action["args"] = json.loads(action.pop("args_json"))
            actions.append(action)
        return actions

    def expire_actions(self, *, action_id: str | None = None, run_id: str | None = None) -> int:
        now = _utc_now()
        filters = ["status IN ('pending', 'approved')", "expires_at <= ?"]
        values: list[Any] = [now]
        if action_id:
            filters.append("action_id = ?")
            values.append(action_id)
        if run_id:
            filters.append("run_id = ?")
            values.append(run_id)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT action_id, run_id FROM pending_actions WHERE {' AND '.join(filters)}",
                values,
            ).fetchall()
            if not rows:
                return 0
            conn.execute(
                f"UPDATE pending_actions SET status = 'expired', expired_at = ? WHERE {' AND '.join(filters)}",
                [now, *values],
            )
            for row in rows:
                conn.execute(
                    "INSERT INTO run_events(run_id, ts, event, payload_json) VALUES (?, ?, 'approval_expired', ?)",
                    (row["run_id"], now, json.dumps({"action_id": row["action_id"]})),
                )
        return len(rows)

    def approve_pending_action(self, action_id: str, approved_by: str) -> bool:
        self.expire_actions(action_id=action_id)
        now = _utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE pending_actions
                SET status = 'approved', approved_by = ?, approved_at = ?
                WHERE action_id = ? AND status = 'pending' AND expires_at > ?
                """,
                (approved_by, now, action_id, now),
            )
            if cursor.rowcount == 1:
                row = conn.execute(
                    "SELECT run_id FROM pending_actions WHERE action_id = ?", (action_id,)
                ).fetchone()
                conn.execute(
                    "INSERT INTO run_events(run_id, ts, event, payload_json) VALUES (?, ?, 'approval_approved', ?)",
                    (row["run_id"], now, json.dumps({"action_id": action_id, "approved_by": approved_by})),
                )
        return cursor.rowcount == 1

    def reject_pending_action(self, action_id: str, rejected_by: str) -> bool:
        self.expire_actions(action_id=action_id)
        now = _utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE pending_actions
                SET status = 'rejected', rejected_by = ?, rejected_at = ?
                WHERE action_id = ? AND status = 'pending' AND expires_at > ?
                """,
                (rejected_by, now, action_id, now),
            )
            if cursor.rowcount == 1:
                row = conn.execute(
                    "SELECT run_id FROM pending_actions WHERE action_id = ?", (action_id,)
                ).fetchone()
                conn.execute(
                    "INSERT INTO run_events(run_id, ts, event, payload_json) VALUES (?, ?, 'approval_rejected', ?)",
                    (row["run_id"], now, json.dumps({"action_id": action_id, "rejected_by": rejected_by})),
                )
        return cursor.rowcount == 1

    def consume_action(
        self,
        action_id: str,
        *,
        thread_id: str,
        tool_name: str,
        args: dict[str, Any],
        contract_hash: str | None,
        tool_call_id: str | None = None,
    ) -> dict[str, Any] | None:
        self.expire_actions(action_id=action_id)
        expected = self.action_hash(tool_name, args, contract_hash)
        now = _utc_now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM pending_actions
                WHERE action_id = ? AND thread_id = ? AND tool_name = ? AND action_hash = ?
                  AND status = 'approved' AND consumed_at IS NULL AND expires_at > ?
                  AND (? IS NULL OR tool_call_id = ?)
                """,
                (action_id, thread_id, tool_name, expected, now, tool_call_id, tool_call_id),
            ).fetchone()
            if row is None:
                conn.rollback()
                return None
            conn.execute(
                "UPDATE pending_actions SET status = 'consumed', consumed_at = ? WHERE action_id = ?",
                (now, action_id),
            )
            conn.execute(
                "INSERT INTO run_events(run_id, ts, event, payload_json) VALUES (?, ?, 'approval_consumed', ?)",
                (row["run_id"], now, json.dumps({"action_id": action_id, "tool": tool_name})),
            )
            conn.commit()
            return dict(row)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def consume_approved_action(
        self,
        thread_id: str,
        tool_name: str,
        args: dict[str, Any],
        contract_hash: str | None,
    ) -> bool:
        expected = self.action_hash(tool_name, args, contract_hash)
        now = _utc_now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT action_id FROM pending_actions
                WHERE thread_id = ? AND action_hash = ? AND status = 'approved'
                  AND consumed_at IS NULL AND expires_at > ?
                ORDER BY approved_at LIMIT 1
                """,
                (thread_id, expected, now),
            ).fetchone()
            if row is None:
                conn.rollback()
                return False
            conn.execute(
                "UPDATE pending_actions SET status = 'consumed', consumed_at = ? WHERE action_id = ?",
                (now, row["action_id"]),
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def list_pending_actions(self, thread_id: str | None = None) -> list[dict[str, Any]]:
        self.expire_actions()
        query = "SELECT * FROM pending_actions WHERE status = 'pending'"
        values: tuple[Any, ...] = ()
        if thread_id:
            query += " AND thread_id = ?"
            values = (thread_id,)
        query += " ORDER BY created_at"
        with self._connect() as conn:
            rows = conn.execute(query, values).fetchall()
        return [dict(row) for row in rows]


runtime_store = RuntimeStore()
