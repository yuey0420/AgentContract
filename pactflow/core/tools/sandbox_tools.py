import os
import shlex
import shutil
import subprocess
import hashlib
import tempfile
from datetime import datetime, timezone
from threading import RLock
from pathlib import Path
from .base import pactflow_tool
from ..config import OFFICE_DIR
from ..execution import current_execution
import re
import platform

SYS_OS = platform.system()
_write_lock = RLock()


def _execution_result(command, exit_code=None, stdout="", stderr="", *, status=None):
    return {"schema_version": "execution/1", "command": command,
            "status": status or ("succeeded" if exit_code == 0 else "failed"),
            "exit_code": exit_code, "stdout": stdout[-16000:], "stderr": stderr[-16000:],
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "environment_id": "local-host", "coverage_gaps": ["no_os_or_network_isolation"]}

def _get_safe_path(relative_path: str) -> str:
    """
    将模型传入的相对路径转换为绝对路径，并死死检查它是否越界！
    如果模型尝试传入 "../../etc/passwd"，这里会直接把它拦截。
    """
    # 将 OFFICE_DIR 转化为标准绝对路径
    base_dir = Path(OFFICE_DIR).resolve(strict=False)
    target_path = (base_dir / relative_path).resolve(strict=False)

    try:
        target_path.relative_to(base_dir)
    except ValueError:
        raise PermissionError(f"越权拦截：你试图访问沙盒外的路径 '{relative_path}'！你只能在 office 工位内活动。")

    return str(target_path)


_BLOCKED_EXECUTABLES = {
    "bash", "bash.exe", "cmd", "cmd.exe", "pwsh", "pwsh.exe",
    "powershell", "powershell.exe", "sh", "sh.exe", "zsh", "zsh.exe",
}
_INLINE_CODE_FLAGS = {"-c", "-e", "--eval", "/c"}
_INTERNAL_COMMANDS = {"dir", "echo", "ls"}
_SHELL_META = re.compile(r"[;&|<>`\r\n]|\$\(")
_SECRET_ENV = re.compile(
    r"(?:key|token|secret|password|credential|authorization|cookie)",
    re.IGNORECASE,
)


def _restricted_environment() -> dict[str, str]:
    allowed = {
        "COMSPEC", "LANG", "LC_ALL", "PATH", "PATHEXT", "SYSTEMROOT",
        "TEMP", "TMP", "WINDIR", "PYTHONIOENCODING",
    }
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in allowed and not _SECRET_ENV.search(key)
    }
    env["HOME"] = OFFICE_DIR
    env["USERPROFILE"] = OFFICE_DIR
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _parse_restricted_command(command: str) -> list[str]:
    if not command.strip():
        raise ValueError("命令不能为空")
    if _SHELL_META.search(command):
        raise PermissionError("命令包含管道、重定向或命令链符号")

    parts = shlex.split(command, posix=SYS_OS != "Windows")
    if not parts:
        raise ValueError("命令不能为空")

    executable = os.path.basename(parts[0]).lower()
    if executable in _BLOCKED_EXECUTABLES:
        raise PermissionError(f"禁止启动嵌套 Shell：{parts[0]}")
    if executable.startswith(("python", "node")) and any(
        arg.lower() in _INLINE_CODE_FLAGS for arg in parts[1:]
    ):
        raise PermissionError("禁止解释器执行内联代码")

    for arg in parts[1:]:
        if arg.startswith("~") or "%" in arg or "$env:" in arg.lower():
            raise PermissionError("命令参数包含主目录或环境变量展开")
        if ".." in Path(arg).parts:
            raise PermissionError("命令参数包含父目录跳转")
        if os.path.isabs(arg):
            candidate = Path(arg).resolve(strict=False)
            try:
                candidate.relative_to(Path(OFFICE_DIR).resolve(strict=False))
            except ValueError as exc:
                raise PermissionError(f"命令参数指向 office 外部：{arg}") from exc

    if executable in _INTERNAL_COMMANDS:
        parts[0] = executable
        return parts

    resolved = shutil.which(parts[0], path=_restricted_environment().get("PATH"))
    if resolved is None:
        raise FileNotFoundError(f"找不到可执行程序：{parts[0]}")
    parts[0] = resolved
    return parts

@pactflow_tool
def list_office_files(sub_dir: str = "") -> str:
    """
    查看你的 office 工位里有哪些文件和文件夹。
    如果 sub_dir 为空，则查看工位根目录。
    """
    try:
        target_dir = _get_safe_path(sub_dir)
        if not os.path.exists(target_dir):
            return f"目录不存在：{sub_dir}"
        
        items = os.listdir(target_dir)
        if not items:
            return f"[{sub_dir if sub_dir else 'office 根目录'}] 是空的。"
        
        # 格式化输出，标注是文件还是文件夹
        result = []
        for item in items:
            item_path = os.path.join(target_dir, item)
            item_type = "📁" if os.path.isdir(item_path) else "📄"
            result.append(f"{item_type} {item}")
            
        return "\n".join(result)
    except Exception as e:
        return str(e)
    
@pactflow_tool
def read_office_file(filepath: str) -> str:
    """
    读取 office 工位里指定文件的内容。
    filepath 参数应该是相对于 office 的路径，例如 "test.py" 或 "skills/my_skill.py"。
    """
    try:
        target_path = _get_safe_path(filepath)
        if not os.path.exists(target_path):
            return f"文件不存在：{filepath}"
        
        with open(target_path, "r", encoding="utf-8") as f:
            content = f.read()
            # 防爆截断：防止读取几个 G 的日志把 Token 撑爆
            if len(content) > 10000:
                return content[:10000] + "\n\n...[内容过长，已被安全截断]..."
            return content
    except Exception as e:
        return str(e)
    
@pactflow_tool
def write_office_file(filepath: str, content: str, mode: str = "w") -> str:
    """
    在 office 工位里操作文件内容。
    
    参数说明:
    - filepath: 相对路径，例如 "spider.py" 或 "docs/readme.md"。
    - content: 要写入的具体文本或代码内容。
    - mode: 写入模式。
        - "w" (默认): 【覆盖/新建】模式。如果文件已存在，将彻底清空原内容并写入新内容！
        - "a": 【追加】模式。保留原内容，将新内容追加到文件最末尾（常用于写日志或在文件末尾新增函数）。
        
    ⚠️ 智能体操作规范：
    1. 如果你要修改一个长文件中间的某几行，目前最安全的做法是：读取原文件，在你的内存中完成替换，然后用 "w" 模式把【完整的最新代码】重写进去。
    2. 如果你需要重命名文件或删除文件，请直接使用 execute_office_shell 工具执行 `mv` 或 `rm` 命令。
    3. 禁止编写 与 跳出office工位 相关的任何语言脚本！
    """
    try:
        target_path = _get_safe_path(filepath)
        
        # 严格校验传入的 mode
        if mode not in ["w", "a"]:
             return "❌ 错误：mode 参数必须是 'w' (覆盖) 或 'a' (追加)。"
        
        # 如果模型想在子目录里写文件，确保子目录存在
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        
        with open(target_path, mode, encoding="utf-8") as f:
            # 如果是追加模式，且内容不是以换行符开头，自动补一个换行，防止代码粘连
            if mode == "a" and not content.startswith("\n"):
                f.write("\n" + content)
            else:
                f.write(content)
                
        action = "覆盖/新建" if mode == "w" else "追加"
        return f" ● 成功以 {action} 模式写入文件：{filepath} (共 {len(content)} 字符)"
    except Exception as e:
        return str(e)
    

@pactflow_tool
def execute_office_shell(command: str) -> dict:
    """
    在 office 工位中执行 Shell 命令。
    
    ⚠️ 【极其重要的环境限制】：
    1. 💻 跨平台注意：当前宿主机可能是 Windows、Linux 或 Mac。请根据你得到的环境反馈，使用对应的原生 Shell 命令（例如 Win 用 dir/del，Linux 用 ls/rm）。如果命令报错，请自行调整重试！
    2. 这是一个非交互式终端！所有命令必须携带免确认参数（如 -y, --quiet）。
    3. 禁止使用 cd 命令跳出当前目录，你的活动范围仅限 office。
    4. [无状态警告] 每次执行都是独立的终端进程！需要进入子目录请使用“命令链”或相对路径。
    5. 禁止一切形式跳出office工位!!! 例如运行跳出或查看office路径的任何脚本以及其他高危操作。
    """
    try:
        argv = _parse_restricted_command(command)

        if argv[0] in {"dir", "ls"}:
            sub_dir = argv[1] if len(argv) > 1 else ""
            target = _get_safe_path(sub_dir)
            if not os.path.isdir(target):
                return _execution_result(command, stderr=f"目录不存在：{sub_dir}")
            listing = "\n".join(sorted(os.listdir(target))) or "(空目录)"
            return _execution_result(command, 0, listing)
        if argv[0] == "echo":
            return _execution_result(command, 0, ' '.join(argv[1:]))

        result = subprocess.run(
            argv,
            shell=False,
            cwd=OFFICE_DIR,
            env=_restricted_environment(),
            capture_output=True,
            encoding='utf-8',
            errors='replace',
            timeout=current_execution.get().shell_timeout
        )
        
        return _execution_result(command, result.returncode, result.stdout, result.stderr)
        
    except subprocess.TimeoutExpired:
        timeout = current_execution.get().shell_timeout
        return _execution_result(command, stderr=f"命令执行超时（{timeout}s）", status="outcome_unknown")
    except (PermissionError, ValueError, FileNotFoundError) as e:
        return _execution_result(command, stderr=f"权限拒绝：{e}")
    except Exception as e:
        return _execution_result(command, stderr=f"执行异常：{e}", status="outcome_unknown")


@pactflow_tool
def patch_office_file(filepath: str, expected_content_hash: str, content: str) -> dict:
    """Replace an office file only when its SHA256 still matches the expected version."""
    try:
        target = Path(_get_safe_path(filepath))
        with _write_lock:
            original = target.read_bytes()
            actual = hashlib.sha256(original).hexdigest()
            if actual != expected_content_hash.removeprefix("sha256:"):
                return {"status": "failed", "reason": "content_conflict", "actual_hash": actual,
                        "expected_hash": expected_content_hash, "path": filepath}
            fd, temporary = tempfile.mkstemp(prefix=".patch-", dir=target.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content.encode("utf-8"))
                    stream.flush()
                    os.fsync(stream.fileno())
                if hashlib.sha256(target.read_bytes()).hexdigest() != actual:
                    return {"status": "failed", "reason": "content_conflict", "path": filepath}
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return {"status": "succeeded", "path": filepath, "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest()}
    except (OSError, ValueError) as error:
        return {"status": "failed", "reason": str(error)}


@pactflow_tool
def run_office_check(check_id: str) -> dict:
    """Run a fixed check from the current approved contract's check registry."""
    from ..process.evidence import capture_filesystem_snapshot, snapshot_fingerprint
    definition = (current_execution.get().checks or {}).get(check_id)
    if not definition or not isinstance(definition.get("argv"), list) or not definition["argv"]:
        return {"status": "failed", "reason": "unknown_check", "check_id": check_id}
    try:
        cwd = _get_safe_path(definition.get("cwd", ""))
        argv = definition["argv"]
        if any(not isinstance(arg, str) for arg in argv):
            raise ValueError("check argv must contain strings")
        result = subprocess.run(argv, cwd=cwd, shell=False, env=_restricted_environment(),
                                capture_output=True, encoding="utf-8", errors="replace",
                                timeout=min(int(definition.get("timeout", 60)), current_execution.get().shell_timeout))
        output = _execution_result(check_id, result.returncode, result.stdout, result.stderr)
        snapshot = capture_filesystem_snapshot(OFFICE_DIR)
        output.update({"check_id": check_id, "checker_version": definition.get("version", "1"),
                       "workspace_revision": snapshot_fingerprint(snapshot), "coverage": snapshot["coverage"]})
        return output
    except subprocess.TimeoutExpired:
        return _execution_result(check_id, stderr="Check timed out", status="outcome_unknown")
    except (OSError, ValueError) as error:
        return _execution_result(check_id, stderr=str(error))
