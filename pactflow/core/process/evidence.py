"""Filesystem baseline and reconciliation helpers for audited runs."""

from __future__ import annotations

import fnmatch
import hashlib
import os
from pathlib import Path
from typing import Any


def capture_filesystem_snapshot(root_path: str, *, max_files: int = 5000) -> dict[str, Any]:
    root = Path(root_path).resolve(strict=False)
    files: dict[str, Any] = {}
    if not root.exists():
        return {"root": str(root), "files": files, "truncated": False}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        try:
            relative = path.relative_to(root).as_posix()
            data = path.read_bytes()
        except (OSError, ValueError):
            continue
        files[relative] = {
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        }
        if len(files) >= max_files:
            return {"root": str(root), "files": files, "truncated": True}
    return {"root": str(root), "files": files, "truncated": False}


def diff_filesystem_snapshots(before: dict[str, Any], after: dict[str, Any]) -> dict[str, list[str]]:
    old = before.get("files", {})
    new = after.get("files", {})
    added = sorted(path for path in new if path not in old)
    deleted = sorted(path for path in old if path not in new)
    modified = sorted(path for path in new if path in old and new[path] != old[path])
    return {"added": added, "modified": modified, "deleted": deleted}


def reconcile_changes(
    diff: dict[str, list[str]],
    allowed_patterns: list[str],
    reported_paths: list[str] | None = None,
) -> dict[str, Any]:
    reported = set(reported_paths or [])
    changed = sorted(set(diff.get("added", [])) | set(diff.get("modified", [])) | set(diff.get("deleted", [])))

    def allowed(path: str) -> bool:
        return any(fnmatch.fnmatch(path, pattern) for pattern in allowed_patterns) if allowed_patterns else False

    in_scope = [path for path in changed if allowed(path)]
    violations = [path for path in changed if not allowed(path)]
    unreported = [path for path in in_scope if path not in reported]
    categories: list[str] = []
    if in_scope:
        categories.append("in_scope_addition")
    if violations:
        categories.append("scope_violation")
    if unreported:
        categories.append("unreported_change")
    return {
        "changed": changed,
        "in_scope": in_scope,
        "scope_violations": violations,
        "unreported": unreported,
        "categories": categories,
        "status": "passed" if not violations and not unreported else "needs_review",
    }

