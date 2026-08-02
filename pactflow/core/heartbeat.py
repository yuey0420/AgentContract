import asyncio
from datetime import datetime
from .bus import task_queue
from .runtime_store import runtime_store

async def pacemaker_loop(check_interval: int = 10):
    """
    后台心脏起搏器协程（带并发锁和循环任务续期功能）
    """
    while True:
        await asyncio.sleep(check_interval)
        
        try:
            triggered_tasks = runtime_store.claim_due_tasks(datetime.now())
        except Exception:
            continue

        for t in triggered_tasks:
            system_msg = (
                f"【系统内部心跳触发】\n"
                f"你设定的定时任务已到期，请立即主动提醒用户或执行动作。\n"
                f"任务 ID：{t['id']}\n"
                f"任务内容：{t['description']}"
            )
            await task_queue.put(system_msg)
