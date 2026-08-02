import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from pactflow.core.heartbeat import pacemaker_loop
from pactflow.core.runtime_store import RuntimeStore


class TestRuntimeScheduledTasks(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = RuntimeStore(os.path.join(self.temp_dir.name, "runtime.sqlite3"), legacy_tasks_file=None)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_due_one_time_task_is_completed(self):
        due = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        self.store.create_scheduled_task(due, "测试提醒", task_id="task-1")

        triggered = self.store.claim_due_tasks(datetime.now())

        self.assertEqual([task["id"] for task in triggered], ["task-1"])
        self.assertEqual(self.store.list_scheduled_tasks(), [])

    def test_repeating_task_advances_beyond_now(self):
        due = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
        self.store.create_scheduled_task(due, "每日提醒", repeat="daily", repeat_count=3, task_id="task-2")

        triggered = self.store.claim_due_tasks(datetime.now())
        remaining = self.store.list_scheduled_tasks()

        self.assertEqual(len(triggered), 1)
        self.assertEqual(remaining[0]["repeat_count"], 2)
        self.assertGreater(
            datetime.strptime(remaining[0]["target_time"], "%Y-%m-%d %H:%M:%S"),
            datetime.now(),
        )

    def test_claim_is_transactionally_idempotent(self):
        due = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        self.store.create_scheduled_task(due, "一次提醒", task_id="task-3")
        self.assertEqual(len(self.store.claim_due_tasks(datetime.now())), 1)
        self.assertEqual(self.store.claim_due_tasks(datetime.now()), [])


class TestPacemaker(unittest.IsolatedAsyncioTestCase):
    async def test_pacemaker_enqueues_due_task(self):
        fake_store = unittest.mock.Mock()
        fake_store.claim_due_tasks.return_value = [{"id": "task-4", "description": "喝水"}]
        fake_queue = AsyncMock()

        async def stop_after_first_sleep(_seconds):
            if fake_store.claim_due_tasks.called:
                raise asyncio.CancelledError

        with patch("pactflow.core.heartbeat.runtime_store", fake_store), \
             patch("pactflow.core.heartbeat.task_queue", fake_queue), \
             patch("pactflow.core.heartbeat.asyncio.sleep", side_effect=stop_after_first_sleep):
            with self.assertRaises(asyncio.CancelledError):
                await pacemaker_loop(check_interval=0)

        fake_queue.put.assert_awaited_once()
        message = fake_queue.put.await_args.args[0]
        self.assertIn("task-4", message)
        self.assertIn("喝水", message)


if __name__ == "__main__":
    unittest.main()
