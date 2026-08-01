import json
import os
import sqlite3
import uuid
import hashlib
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
            }
            for column, column_type in migrations.items():
                if column not in existing_columns:
                    conn.execute(f"ALTER TABLE pending_actions ADD COLUMN {column} {column_type}")

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
    ) -> str:
        run_id = uuid.uuid4().hex
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs
                (run_id, thread_id, objective, mode, phase, status, contract_id, contract_hash, started_at, updated_at)
                VALUES (?, ?, ?, ?, 'prepare', 'running', ?, ?, ?, ?)
                """,
                (run_id, thread_id, objective, mode, contract_id, contract_hash, now, now),
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
                 tool_call_id, risk_level, clause, reason)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)
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
                ),
            )
        return action_id

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
