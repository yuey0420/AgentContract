import os
import json
import threading
import queue
import atexit
import hashlib
import re
from datetime import datetime, timezone


_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|token|secret|password|credential|authorization|cookie)",
    re.IGNORECASE,
)
_CONTENT_KEYS = {"content", "new_content", "file_content", "result_summary"}


def _content_fingerprint(value: str) -> dict[str, object]:
    encoded = value.encode("utf-8", errors="replace")
    return {
        "redacted": True,
        "length": len(value),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def sanitize_log_value(value, key: str | None = None):
    """Remove credentials and large user-controlled payloads before persistence."""
    if key and _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if key in _CONTENT_KEYS and isinstance(value, str):
        return _content_fingerprint(value)
    if isinstance(value, dict):
        return {str(k): sanitize_log_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_log_value(item) for item in value]
    if isinstance(value, str) and len(value) > 2000:
        return {
            "truncated": True,
            "preview": value[:500],
            "length": len(value),
            "sha256": hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest(),
        }
    return value

# 内存队列 + 守护线程
class JSONLEventLogger:
    # 单例模式
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, log_dir: str = "logs"):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._init_logger(log_dir)
            return cls._instance
        
    def _init_logger(self, log_dir: str):
        self.log_dir = log_dir
        os.makedirs(self.log_dir, exist_ok=True)

        # 无界内存队列，用于缓冲日志事件
        self.log_queue = queue.Queue(maxsize=10000)
        self._shutdown = False

        self.worker_thread = threading.Thread(target=self._write_loop, daemon=True)
        self.worker_thread.start()

        # 确保程序被关闭时，队列里的剩下日志能写完
        atexit.register(self.shutdown)

    # 后台线程的死循环：一直盯着队列，有日志就写，没日志就阻塞休眠
    def _write_loop(self):
        while True:
            log_item = self.log_queue.get()

            if log_item is None:
                self.log_queue.task_done()
                break

            try:
                thread_id = log_item.get("thread_id", "system")
                safe_id = "".join(c for c in thread_id if c.isalnum() or c in "-_") or "default"
                file_path = os.path.join(self.log_dir, f"{safe_id}.jsonl")

                with open(file_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(log_item, ensure_ascii=False) + "\n")
            except Exception as e:
                print(f"[Logger Error] 异步写日志失败: {e}")
            finally:
                self.log_queue.task_done()

    # 前台调用的埋点方法
    def log_event(self, thread_id: str, event: str, **kwargs):
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        log_item = {
            "ts": now_utc,
            "thread_id": thread_id,
            "event": event,
            **sanitize_log_value(kwargs),
        }

        if not self._shutdown:
            self.log_queue.put(log_item, timeout=5)

    def flush(self):
        self.log_queue.join()

    def shutdown(self):
        if self._shutdown:
            return
        self._shutdown = True
        self.log_queue.put(None)
        self.log_queue.join()
        self.worker_thread.join(timeout=5)

audit_logger = JSONLEventLogger()
