"""V3 tables and transactions on the existing RuntimeStore connection."""

import json
import uuid
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


class ProcessStorage:
    def initialize_process_storage(self):
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS process_runs (
                    run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
                    work_item_id TEXT NOT NULL, contract_json TEXT,
                    run_status TEXT NOT NULL DEFAULT 'preparing', terminal_result TEXT,
                    waiting_json TEXT, repair_count INTEGER NOT NULL DEFAULT 0,
                    max_repairs INTEGER NOT NULL DEFAULT 3, delivery_json TEXT,
                    workspace_root TEXT NOT NULL, context_revision INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS collaboration_revisions (
                    kind TEXT NOT NULL, object_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    run_id TEXT, actor TEXT NOT NULL, payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL, PRIMARY KEY(kind, object_id, revision)
                );
                CREATE TABLE IF NOT EXISTS decision_requests (
                    request_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id),
                    request_key TEXT NOT NULL, revision INTEGER NOT NULL,
                    status TEXT NOT NULL, payload_json TEXT NOT NULL,
                    resolution_json TEXT, expires_at TEXT, UNIQUE(run_id, request_key, revision)
                );
                CREATE TABLE IF NOT EXISTS action_executions (
                    run_id TEXT NOT NULL REFERENCES runs(run_id), tool_call_id TEXT NOT NULL,
                    action_hash TEXT NOT NULL, tool_name TEXT NOT NULL, capability TEXT NOT NULL,
                    resource TEXT, status TEXT NOT NULL, authorization_ref TEXT,
                    attempt_id TEXT NOT NULL, result_json TEXT, started_at TEXT, finished_at TEXT,
                    PRIMARY KEY(run_id, tool_call_id)
                );
            """)
            process_columns = {row[1] for row in conn.execute("PRAGMA table_info(process_runs)")}
            if "work_revision" not in process_columns:
                conn.execute("ALTER TABLE process_runs ADD COLUMN work_revision INTEGER NOT NULL DEFAULT 1")
            action_columns = {row[1] for row in conn.execute("PRAGMA table_info(action_executions)")}
            if "policy_fingerprint" not in action_columns:
                conn.execute("ALTER TABLE action_executions ADD COLUMN policy_fingerprint TEXT")
            grant_columns = {row[1] for row in conn.execute("PRAGMA table_info(approval_grants)")}
            if "source_action_id" not in grant_columns:
                conn.execute("ALTER TABLE approval_grants ADD COLUMN source_action_id TEXT")
                conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_grant_source ON approval_grants(source_action_id) WHERE source_action_id IS NOT NULL")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(task_nodes)")}
            if "logical_node_id" not in columns:
                conn.execute("ALTER TABLE task_nodes ADD COLUMN logical_node_id TEXT")
                rows = conn.execute("SELECT node_id, run_id, parent_node_id, depends_on_json FROM task_nodes").fetchall()
                for row in rows:
                    mapping = {r["node_id"]: r["run_id"] + ":" + r["node_id"] for r in rows if r["run_id"] == row["run_id"]}
                    conn.execute("""UPDATE task_nodes SET node_id=?, logical_node_id=?,
                                 parent_node_id=?, depends_on_json=? WHERE node_id=?""",
                                 (mapping[row["node_id"]], row["node_id"],
                                  mapping.get(row["parent_node_id"], row["parent_node_id"]),
                                  encode([mapping.get(d, d) for d in json.loads(row["depends_on_json"])]), row["node_id"]))
                conn.execute("INSERT OR REPLACE INTO runtime_meta VALUES ('process_schema', '1')")
                conn.execute("INSERT OR REPLACE INTO runtime_meta VALUES ('legacy_node_history', 'Previously overwritten records cannot be recovered')")

    @staticmethod
    def _process_event(conn, run_id, event, payload):
        conn.execute("INSERT INTO run_events(run_id, ts, event, payload_json) VALUES (?, ?, ?, ?)",
                     (run_id, now(), event, encode(payload)))

    def bind_process_run(self, run_id, work_item_id, contract, workspace_root, max_repairs=3):
        with self._connect() as conn:
            conn.execute("""INSERT OR IGNORE INTO process_runs
                         (run_id, work_item_id, contract_json, workspace_root, max_repairs)
                         VALUES (?, ?, ?, ?, ?)""",
                         (run_id, work_item_id, encode(contract) if contract else None, workspace_root, max_repairs))

    def get_process_run(self, run_id):
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM process_runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        for key in ("contract", "waiting", "delivery"):
            value = result.pop(key + "_json")
            result[key] = json.loads(value) if value else None
        return result

    def transition_run(self, run_id, status, *, result=None, waiting=None):
        if status not in {"preparing", "executing", "verifying", "waiting", "finished"}:
            raise ValueError("unknown run_status")
        if (status == "finished") != (result in {"succeeded", "failed", "cancelled", "needs_review"}):
            raise ValueError("terminal_result is required only for finished runs")
        if status == "waiting" and not all(key in (waiting or {}) for key in ("reason", "blocked_node_ids", "resume_target")):
            raise ValueError("waiting requires reason, blocked nodes and resume target")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT run_status FROM process_runs WHERE run_id=?", (run_id,)).fetchone()
            if not row or row[0] == "finished":
                return False
            conn.execute("UPDATE process_runs SET run_status=?, terminal_result=?, waiting_json=? WHERE run_id=?",
                         (status, result, encode(waiting) if waiting else None, run_id))
            legacy_status = {"succeeded": "completed", "failed": "failed", "cancelled": "cancelled", "needs_review": "needs_review"}.get(result, "running")
            conn.execute("UPDATE runs SET phase=?, status=?, updated_at=?, completed_at=? WHERE run_id=?",
                         ({"preparing": "prepare", "executing": "execute", "verifying": "verify", "waiting": "waiting", "finished": "finalize"}[status],
                          legacy_status, now(), now() if result else None, run_id))
            self._process_event(conn, run_id, "execution.state_changed", {"run_status": status, "terminal_result": result, "waiting": waiting})
        return True

    def revise_record(self, kind, object_id, payload, *, expected_revision, actor="runtime", run_id=None):
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            revision = conn.execute("SELECT COALESCE(MAX(revision), 0) FROM collaboration_revisions WHERE kind=? AND object_id=?", (kind, object_id)).fetchone()[0]
            if revision != expected_revision:
                raise ValueError("revision_conflict")
            revision += 1
            conn.execute("INSERT INTO collaboration_revisions VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (kind, object_id, revision, run_id, actor, encode(payload), now()))
            if run_id:
                self._process_event(conn, run_id, "collaboration." + kind + "_revised", {"object_id": object_id, "revision": revision, "actor": actor})
        return revision

    def get_record(self, kind, object_id, revision=None):
        with self._connect() as conn:
            row = conn.execute("""SELECT * FROM collaboration_revisions WHERE kind=? AND object_id=?
                               AND (? IS NULL OR revision=?) ORDER BY revision DESC LIMIT 1""", (kind, object_id, revision, revision)).fetchone()
        return {**dict(row), "payload": json.loads(row["payload_json"])} if row else None

    def request_decision(self, run_id, request_key, payload, *, revision=1, expires_at=None):
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM decision_requests WHERE run_id=? AND request_key=? AND revision=?", (run_id, request_key, revision)).fetchone()
            if row:
                if row["payload_json"] != encode(payload):
                    raise ValueError("revision_conflict")
                return row["request_id"]
            conn.execute("UPDATE decision_requests SET status='cancelled' WHERE run_id=? AND request_key=? AND status='pending'", (run_id, request_key))
            request_id = uuid.uuid4().hex
            conn.execute("INSERT INTO decision_requests VALUES (?, ?, ?, ?, 'pending', ?, NULL, ?)",
                         (request_id, run_id, request_key, revision, encode(payload), expires_at))
            self._process_event(conn, run_id, "collaboration.decision_requested", {"request_id": request_id, "revision": revision, **payload})
        return request_id

    def list_decisions(self, run_id):
        with self._connect() as conn:
            conn.execute("UPDATE decision_requests SET status='expired' WHERE run_id=? AND status='pending' AND expires_at<=?", (run_id, now()))
            rows = conn.execute("SELECT * FROM decision_requests WHERE run_id=? ORDER BY rowid", (run_id,)).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload_json"]), "resolution": json.loads(row["resolution_json"]) if row["resolution_json"] else None} for row in rows]

    def resolve_decision(self, request_id, expected_revision, actor, choice):
        if not actor or actor in {"runtime", "agent", "model"}:
            raise ValueError("decision requires a trusted human actor")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM decision_requests WHERE request_id=?", (request_id,)).fetchone()
            if not row or row["revision"] != expected_revision or row["status"] != "pending":
                raise ValueError("decision_conflict")
            if row["expires_at"] and row["expires_at"] <= now():
                raise ValueError("decision_expired")
            payload = json.loads(row["payload_json"])
            if choice not in [item["id"] for item in payload.get("options", [])]:
                raise ValueError("unknown decision option")
            resolution = {"actor": actor, "choice": choice, "resolved_at": now(), "authorization_effect": "none"}
            conn.execute("UPDATE decision_requests SET status='resolved', resolution_json=? WHERE request_id=?", (encode(resolution), request_id))
            self._process_event(conn, row["run_id"], "collaboration.decision_resolved", {"request_id": request_id, **resolution})
        return resolution

    def reserve_repair(self, run_id, failure):
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute("UPDATE process_runs SET repair_count=repair_count+1 WHERE run_id=? AND repair_count<max_repairs AND run_status!='finished'", (run_id,))
            if cursor.rowcount:
                self._process_event(conn, run_id, "verification.repair_requested", failure)
            return cursor.rowcount == 1

    def register_execution(self, run_id, call_id, tool, args, contract_hash, capability, resource):
        fingerprint = self.action_hash(tool, args, contract_hash)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""INSERT OR IGNORE INTO action_executions
                         (run_id, tool_call_id, action_hash, tool_name, capability, resource, status, attempt_id)
                         VALUES (?, ?, ?, ?, ?, ?, 'requested', ?)""",
                         (run_id, call_id, fingerprint, tool, capability, resource, uuid.uuid4().hex))
            row = conn.execute("SELECT * FROM action_executions WHERE run_id=? AND tool_call_id=?", (run_id, call_id)).fetchone()
            if row["action_hash"] != fingerprint or row["capability"] != capability or row["resource"] != resource:
                raise ValueError("tool_call_id reused with changed arguments or contract")
        return dict(row)

    def bind_execution_policy(self, run_id, call_id, fingerprint):
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT policy_fingerprint FROM action_executions WHERE run_id=? AND tool_call_id=?", (run_id, call_id)).fetchone()
            if row[0] is not None and row[0] != fingerprint:
                return False
            conn.execute("UPDATE action_executions SET policy_fingerprint=? WHERE run_id=? AND tool_call_id=?", (fingerprint, run_id, call_id))
        return True

    def execution_budget(self, run_id, call_id, capability, resource, limits, conn=None):
        if conn is None:
            with self._connect() as connection:
                return self.execution_budget(run_id, call_id, capability, resource, limits, connection)
        rows = conn.execute("""SELECT capability, resource FROM action_executions WHERE run_id=? AND tool_call_id!=?
                             AND status IN ('authorized','started','succeeded','failed','outcome_unknown')""", (run_id, call_id)).fetchall()
        if limits.max_tool_calls is not None and len(rows) >= limits.max_tool_calls:
            return "limits.max_tool_calls"
        written = {row["resource"] for row in rows if row["capability"] == "write"}
        if capability == "write" and limits.max_written_files is not None and resource not in written and len(written) >= limits.max_written_files:
            return "limits.max_written_files"
        return None

    def authorize_execution(self, run_id, call_id, limits=None, approval_id=None, grant_id=None):
        # Approval consumption and budget reservation share a transaction.
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM action_executions WHERE run_id=? AND tool_call_id=?", (run_id, call_id)).fetchone()
            if row["status"] == "authorized":
                return None
            if row["status"] != "requested":
                return "execution.invalid_state"
            if limits:
                error = self.execution_budget(run_id, call_id, row["capability"], row["resource"], limits, conn)
                if error:
                    return error
            if approval_id:
                cursor = conn.execute("""UPDATE pending_actions SET status='consumed', consumed_at=?
                                      WHERE action_id=? AND run_id=? AND tool_call_id=? AND action_hash=?
                                      AND status='approved' AND expires_at>?""", (now(), approval_id, run_id, call_id, row["action_hash"], now()))
                if not cursor.rowcount:
                    return "approval.invalid_or_expired"
                self._process_event(conn, run_id, "approval_consumed", {"action_id": approval_id, "tool": row["tool_name"]})
            if grant_id:
                cursor = conn.execute("""UPDATE approval_grants SET uses=uses+1 WHERE grant_id=? AND run_id=?
                                      AND revoked_at IS NULL AND expires_at>? AND uses<max_uses""", (grant_id, run_id, now()))
                if not cursor.rowcount:
                    return "approval.invalid_or_expired"
                self._process_event(conn, run_id, "approval_grant_consumed", {"grant_id": grant_id, "tool_call_id": call_id})
            conn.execute("UPDATE action_executions SET status='authorized', authorization_ref=? WHERE run_id=? AND tool_call_id=?", (approval_id or grant_id, run_id, call_id))
        return None

    def start_execution(self, run_id, call_id):
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT capability FROM action_executions WHERE run_id=? AND tool_call_id=?", (run_id, call_id)).fetchone()
            process = conn.execute("SELECT workspace_root FROM process_runs WHERE run_id=?", (run_id,)).fetchone()
            if current and process and current[0] in {"write", "execute", "external"}:
                busy = conn.execute("""SELECT 1 FROM action_executions a JOIN process_runs p ON p.run_id=a.run_id
                                      WHERE p.workspace_root=? AND a.status IN ('started', 'outcome_unknown')
                                      AND a.capability IN ('write', 'execute', 'external') LIMIT 1""", (process[0],)).fetchone()
                if busy:
                    return False
            cursor = conn.execute("UPDATE action_executions SET status='started', started_at=? WHERE run_id=? AND tool_call_id=? AND status='authorized'", (now(), run_id, call_id))
            return cursor.rowcount == 1

    def finish_execution(self, run_id, call_id, status, result, event=None, payload=None):
        with self._connect() as conn:
            conn.execute("UPDATE action_executions SET status=?, result_json=?, finished_at=? WHERE run_id=? AND tool_call_id=?", (status, encode(result), now(), run_id, call_id))
            if event:
                self._process_event(conn, run_id, event, payload or {})

    def save_delivery(self, run_id, report):
        with self._connect() as conn:
            conn.execute("UPDATE process_runs SET delivery_json=? WHERE run_id=?", (encode(report), run_id))
            self._process_event(conn, run_id, "delivery.ready", {"technical_status": report["delivery"]["technical_status"], "human_acceptance": report["delivery"]["human_acceptance"]})
