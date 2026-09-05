"""Filesystem baseline and reconciliation helpers for audited runs."""

from __future__ import annotations

import fnmatch
import hashlib
import os
import json
from pathlib import Path
from typing import Any


def capture_filesystem_snapshot(root_path: str, *, max_files: int = 5000) -> dict[str, Any]:
    root = Path(root_path).resolve(strict=False)
    files: dict[str, Any] = {}
    gaps: list[dict] = []
    truncated = False
    if not root.exists():
        gaps.append({"path": str(root), "reason": "root_missing"})
    def onerror(error):
        gaps.append({"path": error.filename, "reason": str(error)})

    for directory, dirs, names in os.walk(root, followlinks=False, onerror=onerror):
        for name in list(dirs):
            path = Path(directory) / name
            try:
                path.resolve().relative_to(root)
                if path.is_symlink():
                    raise ValueError("symlink_directory")
            except (OSError, ValueError) as error:
                dirs.remove(name)
                gaps.append({"path": str(path), "reason": str(error)})
        for name in sorted(names):
            path = Path(directory) / name
            try:
                path.resolve().relative_to(root)
                if path.is_symlink():
                    raise ValueError("symlink_file")
                if len(files) >= max_files:
                    truncated = True
                    break
                relative = path.relative_to(root).as_posix()
                data = path.read_bytes()
                files[relative] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
            except (OSError, ValueError) as error:
                gaps.append({"path": str(path), "reason": str(error)})
        if truncated:
            break
    return {"root": str(root), "files": files, "truncated": truncated,
            "coverage": "limited" if truncated or gaps else "complete", "coverage_gaps": gaps,
            "recovery": "hashes_only", "excluded": []}


def snapshot_fingerprint(snapshot: dict) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(snapshot.get("files", {}), sort_keys=True).encode()).hexdigest()


def diff_filesystem_snapshots(before: dict[str, Any], after: dict[str, Any]) -> dict[str, list[str]]:
    old = before.get("files", {})
    new = after.get("files", {})
    added = sorted(path for path in new if path not in old)
    deleted = sorted(path for path in old if path not in new)
    modified = sorted(path for path in new if path in old and new[path] != old[path])
    return {"added": added, "modified": modified, "deleted": deleted,
            "coverage_gaps": before.get("coverage_gaps", []) + after.get("coverage_gaps", []),
            "coverage": "limited" if before.get("truncated") or after.get("truncated") or before.get("coverage") == "limited" or after.get("coverage") == "limited" else "complete"}


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
        "coverage": diff.get("coverage", "complete"),
        "coverage_gaps": diff.get("coverage_gaps", []),
        "status": "passed" if not violations and not unreported and diff.get("coverage") != "limited" else "needs_review",
    }
