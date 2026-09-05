from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutionContext:
    thread_id: str = "system_default"
    run_id: str | None = None
    shell_timeout: int = 60
    checks: dict | None = None


current_execution: ContextVar[ExecutionContext] = ContextVar(
    "pactflow_execution",
    default=ExecutionContext(),
)
