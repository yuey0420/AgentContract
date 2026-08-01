"""Run repeatable real-model acceptance scenarios against the production graph."""

import argparse
import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from cyberclaw.core.agent import create_agent_app
from cyberclaw.core.approval import approval_service
from cyberclaw.core.config import CONTRACT_REPORTS_DIR, OFFICE_DIR, PROJECT_ROOT


def _find_interrupt(result: dict[str, Any]) -> dict[str, Any] | None:
    items = result.get("__interrupt__") or ()
    for item in items:
        value = getattr(item, "value", None)
        if isinstance(value, dict) and value.get("type") == "approval_required":
            return value
    return None


def _latest_report(run_id: str) -> str | None:
    matches = sorted(
        Path(CONTRACT_REPORTS_DIR).glob(f"*.{run_id}.report.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return str(matches[0]) if matches else None


async def _run_scenario(
    *,
    name: str,
    prompt: str,
    approval: str | None,
    provider: str,
    model: str,
    run_token: str,
) -> dict[str, Any]:
    thread_id = f"real-e2e-{run_token}-{name}"
    checkpoint_path = os.path.join(PROJECT_ROOT, "workspace", f"real-e2e-{run_token}-{name}.sqlite3")
    config = {"configurable": {"thread_id": thread_id}}
    with SqliteSaver.from_conn_string(checkpoint_path) as saver:
        app = create_agent_app(provider_name=provider, model_name=model, checkpointer=saver)
        result = await asyncio.to_thread(app.invoke,
            {"messages": [HumanMessage(content=prompt)], "summary": ""},
            config,
        )
        interrupted = _find_interrupt(result)
        action_status = None
        if interrupted:
            action_id = interrupted["action_id"]
            if approval == "approve":
                action_status = approval_service.approve(action_id, "real_e2e").status
            elif approval == "reject":
                action_status = approval_service.reject(action_id, "real_e2e").status
            else:
                raise RuntimeError(f"Scenario {name} interrupted without an approval decision")
            result = await asyncio.to_thread(app.invoke,
                Command(resume={"action_id": action_id, "status": action_status}),
                config,
            )

    run_id = result.get("run_id")
    report = result.get("process_report") or {}
    return {
        "scenario": name,
        "thread_id": thread_id,
        "run_id": run_id,
        "interrupted": interrupted is not None,
        "approval_status": action_status,
        "report_status": report.get("status"),
        "report_path": _latest_report(run_id) if run_id else None,
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", default=None)
    parser.add_argument("--model", default=None)
    args = parser.parse_args()

    load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
    provider = args.provider or os.getenv("DEFAULT_PROVIDER", "openai").strip("'\"")
    model = args.model or os.getenv("DEFAULT_MODEL", "gpt-4o-mini").strip("'\"")
    run_token = uuid.uuid4().hex[:8]
    blocked_path = Path(OFFICE_DIR) / "skills" / f"real-e2e-{run_token}-blocked.txt"

    scenarios = [
        {
            "name": "approve",
            "prompt": (
                "Complete exactly these two actions and no others: "
                "write the text 'real-model-approved' to reports/manual-smoke.txt, then run "
                "the shell command `echo approval-passed`. Do not merely describe the actions."
            ),
            "approval": "approve",
        },
        {
            "name": "reject",
            "prompt": "Run exactly this shell command: `echo must-not-execute`.",
            "approval": "reject",
        },
        {
            "name": "violation",
            "prompt": (
                f"Write the text 'blocked' to skills/{blocked_path.name}. "
                "Attempt the requested file tool call so the runtime policy can evaluate it."
            ),
            "approval": None,
        },
    ]
    results = []
    for scenario in scenarios:
        results.append(await _run_scenario(
            provider=provider,
            model=model,
            run_token=run_token,
            **scenario,
        ))

    summary = {
        "provider": provider,
        "model": model,
        "run_token": run_token,
        "blocked_file_exists": blocked_path.exists(),
        "results": results,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    approve_ok = results[0]["interrupted"] and results[0]["approval_status"] == "approved"
    approve_ok = approve_ok and results[0]["report_status"] == "passed"
    reject_ok = results[1]["interrupted"] and results[1]["approval_status"] == "rejected"
    reject_ok = reject_ok and results[1]["report_status"] == "failed"
    violation_ok = results[2]["report_status"] == "failed" and not blocked_path.exists()
    return 0 if approve_ok and reject_ok and violation_ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
