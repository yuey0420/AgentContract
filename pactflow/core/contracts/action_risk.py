import os
import shlex
from typing import Literal


ShellEffect = Literal["read", "write", "destructive", "external", "execute"]

_READ_ONLY_COMMANDS = {
    "dir", "ls", "pytest", "py.test", "ruff", "mypy",
}
_WRITE_COMMANDS = {
    "cp", "copy", "mkdir", "md", "move", "mv", "ren", "rename", "touch",
}
_DESTRUCTIVE_COMMANDS = {
    "del", "erase", "rd", "rmdir", "rm", "truncate",
}
_EXTERNAL_COMMANDS = {
    "curl", "ftp", "gh", "scp", "ssh", "wget",
}


def _command_parts(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=os.name != "nt")
    except ValueError:
        return []


def classify_shell_effect(command: str) -> ShellEffect:
    """Classify a restricted shell command by its observable side effect."""
    parts = _command_parts(command)
    if not parts:
        return "execute"
    executable = os.path.basename(parts[0]).lower().removesuffix(".exe")
    lowered = [part.lower() for part in parts[1:]]

    if executable == "echo":
        return "read"
    if executable == "git":
        subcommand = lowered[0] if lowered else ""
        if subcommand in {"status", "diff", "log", "show", "branch", "rev-parse"}:
            return "read"
        if subcommand in {"push", "pull", "fetch", "clone", "remote"}:
            return "external"
        if subcommand == "clean":
            return "destructive"
        return "write"
    if executable in _READ_ONLY_COMMANDS:
        return "read"
    if executable in _DESTRUCTIVE_COMMANDS:
        return "destructive"
    if executable in _EXTERNAL_COMMANDS:
        return "external"
    if executable in _WRITE_COMMANDS:
        return "write"
    return "execute"


def shell_risk_level(command: str) -> str:
    return {
        "read": "low",
        "write": "medium",
        "execute": "high",
        "destructive": "critical",
        "external": "critical",
    }[classify_shell_effect(command)]
