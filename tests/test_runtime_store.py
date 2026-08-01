import os
import tempfile
import unittest
import json
from unittest.mock import patch

from cyberclaw.core.runtime_store import RuntimeStore
from cyberclaw.core.contracts.store import (
    approve_active_contract,
    contract_has_valid_approval,
    load_active_contract,
)


class TestRuntimeStore(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = RuntimeStore(os.path.join(self.temp_dir.name, "runtime.sqlite3"), legacy_tasks_file=None)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_contract_approval_is_hash_scoped(self):
        self.store.record_contract_approval("c1", "sha256:one", "owner")
        self.assertTrue(self.store.has_contract_approval("c1", "sha256:one"))
        self.assertFalse(self.store.has_contract_approval("c1", "sha256:two"))

    def test_run_events_keep_execution_order(self):
        run_id = self.store.start_run("thread", "objective", "chat")
        self.store.append_run_event(run_id, "first")
        self.store.append_run_event(run_id, "second")
        self.assertEqual(
            [event["event"] for event in self.store.get_run_events(run_id)],
            ["first", "second"],
        )

    def test_approval_supports_reject_expire_and_exactly_once_consume(self):
        run_id = self.store.start_run("thread", "approval", "guarded_action")
        action_id = self.store.create_pending_action(
            run_id, "thread", "shell", {"command": "echo exact"}, "hash",
            tool_call_id="call-exact", risk_level="high", clause="test.approval",
        )
        self.assertTrue(self.store.approve_pending_action(action_id, "owner"))
        consumed = self.store.consume_action(
            action_id,
            thread_id="thread",
            tool_call_id="call-exact",
            tool_name="shell",
            args={"command": "echo exact"},
            contract_hash="hash",
        )
        self.assertIsNotNone(consumed)
        self.assertIsNone(self.store.consume_action(
            action_id,
            thread_id="thread",
            tool_call_id="call-exact",
            tool_name="shell",
            args={"command": "echo exact"},
            contract_hash="hash",
        ))

        rejected_id = self.store.create_pending_action(
            run_id, "thread", "shell", {"command": "echo no"}, "hash",
            tool_call_id="call-reject",
        )
        self.assertTrue(self.store.reject_pending_action(rejected_id, "owner"))
        self.assertEqual(self.store.get_action(rejected_id)["status"], "rejected")

        expired_id = self.store.create_pending_action(
            run_id, "thread", "shell", {"command": "echo late"}, "hash",
            ttl_minutes=-1,
            tool_call_id="call-expired",
        )
        self.assertEqual(self.store.get_action(expired_id)["status"], "expired")
        self.assertFalse(self.store.approve_pending_action(expired_id, "owner"))

    def test_active_contract_approval_persists_hash_and_registry(self):
        contract_path = os.path.join(self.temp_dir.name, "current.contract.json")
        with open(contract_path, "w", encoding="utf-8") as file:
            json.dump({
                "contract_version": "0.2",
                "id": "registry-contract",
                "owner": "owner",
                "objective": "registry approval",
                "status": "draft",
                "scope": {},
                "tool_policy": {},
            }, file)

        with patch("cyberclaw.core.contracts.store.ACTIVE_CONTRACT_FILE", contract_path), \
             patch("cyberclaw.core.runtime_store.runtime_store", self.store):
            approved = approve_active_contract("owner")
            reloaded = load_active_contract()

        self.assertEqual(reloaded.status, "approved")
        self.assertTrue(contract_has_valid_approval(reloaded))
        self.assertTrue(self.store.has_contract_approval(
            approved.id,
            approved.approval.contract_hash,
        ))


if __name__ == "__main__":
    unittest.main()
