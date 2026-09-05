"""Office-bound project copies and content recovery, without OS isolation claims."""

import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from .evidence import capture_filesystem_snapshot


def _atomic_bytes(target, data):
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".content-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def capture_recoverable_snapshot(root_path, object_dir):
    snapshot = capture_filesystem_snapshot(root_path)
    root = Path(root_path).resolve()
    objects = Path(object_dir)
    snapshot["content_objects"] = {}
    for relative, entry in snapshot["files"].items():
        try:
            source = (root / relative).resolve()
            source.relative_to(root)
            data = source.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            if digest != entry["sha256"]:
                raise ValueError("file_changed_during_baseline")
            target = objects / digest
            if not target.exists():
                _atomic_bytes(target, data)
            snapshot["content_objects"][relative] = digest
        except (OSError, ValueError) as error:
            snapshot["coverage"] = "limited"
            snapshot["coverage_gaps"].append({"path": relative, "reason": str(error)})
    snapshot["recovery"] = "content_objects"
    return snapshot


def restore_baseline_file(snapshot, relative, object_dir, expected_content_hash):
    root = Path(snapshot["root"]).resolve()
    target = (root / relative).resolve()
    target.relative_to(root)
    digest = snapshot.get("content_objects", {}).get(relative)
    if not digest or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("baseline content is unavailable")
    data = (Path(object_dir) / digest).read_bytes()
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("baseline content hash mismatch")
    actual = hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else "missing"
    if actual != expected_content_hash.removeprefix("sha256:"):
        raise ValueError("content_conflict")
    _atomic_bytes(target, data)
    return {"path": relative, "content_hash": digest, "status": "restored"}


def capture_git_baseline(root_path):
    if not shutil.which("git"):
        return {"status": "unavailable", "reason": "git_not_installed"}
    def git(*args):
        result = subprocess.run(["git", "-C", str(root_path), *args], capture_output=True, encoding="utf-8", errors="replace", timeout=20)
        if result.returncode:
            raise ValueError(result.stderr.strip())
        return result.stdout
    try:
        return {"status": "recorded", "head": git("rev-parse", "HEAD").strip(),
                "index_patch": git("diff", "--cached", "--binary", "--no-ext-diff"),
                "worktree_patch": git("diff", "--binary", "--no-ext-diff"),
                "untracked": git("ls-files", "--others", "--exclude-standard", "-z").split("\0"),
                "ignored": git("ls-files", "--others", "--ignored", "--exclude-standard", "-z").split("\0"),
                "recovery": "patches_plus_filesystem_content", "external_side_effects": "not_recoverable"}
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        return {"status": "unavailable", "reason": str(error)}


def import_project_copy(store, source_path, office_root, project_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", project_id):
        raise ValueError("project_id must contain letters, digits, underscores or hyphens")
    source = Path(source_path).resolve(strict=True)
    office = Path(office_root).resolve()
    target = office / "products" / project_id
    if target.exists() or source == target or source in target.parents:
        raise ValueError("destination exists or is inside source")
    scan = capture_filesystem_snapshot(str(source))
    if scan["coverage"] != "complete":
        raise ValueError("source scan is incomplete or contains links")
    git_baseline = capture_git_baseline(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Exclude repository internals; the source checkout is never modified.
    shutil.copytree(source, target, symlinks=False, ignore=shutil.ignore_patterns(".git"))
    copied = capture_filesystem_snapshot(str(target))
    for relative, entry in scan["files"].items():
        if ".git" not in Path(relative).parts and copied["files"].get(relative) != entry:
            raise ValueError("source_changed_during_copy; inspect incomplete destination")
    binding = {"project_id": project_id, "root": str(target), "office_relative_root": target.relative_to(office).as_posix(),
               "source": str(source), "mode": "isolated_copy", "baseline": copied, "git_baseline": git_baseline,
               "os_isolation": False, "network_isolation": False, "write_model": "single_writer"}
    store.revise_record("workspace_binding", project_id, binding, expected_revision=0, actor="local_user")
    return binding
