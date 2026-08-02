import os
from dotenv import load_dotenv

load_dotenv()

CORE_DIR = os.path.dirname(os.path.abspath(__file__))
PACKAGE_DIR = os.path.dirname(CORE_DIR)
PROJECT_ROOT = os.path.dirname(PACKAGE_DIR)
# 通过 __file__ 和多次 os.path.dirname 向上回溯，自动计算出项目的目录结构，让代码不依赖写死的路径。
# PactFlow 命名优先；保留旧环境变量，避免升级后丢失已有工作区。
WORKSPACE_DIR = os.getenv(
    "PACTFLOW_WORKSPACE",
    os.getenv("CYBERCLAW_WORKSPACE", os.path.join(PROJECT_ROOT, "workspace")),
)

DB_PATH = os.path.join(WORKSPACE_DIR, "state.sqlite3")     # 状态机：潜意识与短期记忆
RUNTIME_DB_PATH = os.path.join(WORKSPACE_DIR, "runtime.sqlite3")
MEMORY_DIR = os.path.join(WORKSPACE_DIR, "memory")         # 显性记忆：Markdown 画像
PERSONAS_DIR = os.path.join(WORKSPACE_DIR, "personas")     # 人设区：系统 Prompt
SCRIPTS_DIR = os.path.join(WORKSPACE_DIR, "scripts")       # 脚本区：自动化武器库
OFFICE_DIR = os.path.join(WORKSPACE_DIR, "office")         # 沙盒工位 唯一被允许执行文件与shell操作的空间
SKILLS_DIR = os.path.join(OFFICE_DIR, "skills")            # 技能卡槽
TASKS_FILE = os.path.join(WORKSPACE_DIR, "tasks.json")

CONTRACTS_DIR = os.path.join(WORKSPACE_DIR, "contracts")
CONTRACT_ACTIVE_DIR = os.path.join(CONTRACTS_DIR, "active")
CONTRACT_DRAFTS_DIR = os.path.join(CONTRACTS_DIR, "drafts")
CONTRACT_ARCHIVE_DIR = os.path.join(CONTRACTS_DIR, "archive")
CONTRACT_REPORTS_DIR = os.path.join(CONTRACTS_DIR, "reports")
CONTRACT_TEMPLATES_DIR = os.path.join(CONTRACTS_DIR, "templates")
ACTIVE_CONTRACT_FILE = os.path.join(CONTRACT_ACTIVE_DIR, "current.contract.json")
STRICT_CONTRACTS = os.getenv(
    "PACTFLOW_STRICT_CONTRACTS",
    os.getenv("CYBERCLAW_STRICT_CONTRACTS", "false"),
).lower() in {"1", "true", "yes", "on"}

for d in [
    WORKSPACE_DIR,
    MEMORY_DIR,
    PERSONAS_DIR,
    SCRIPTS_DIR,
    OFFICE_DIR,
    SKILLS_DIR,
    CONTRACTS_DIR,
    CONTRACT_ACTIVE_DIR,
    CONTRACT_DRAFTS_DIR,
    CONTRACT_ARCHIVE_DIR,
    CONTRACT_REPORTS_DIR,
    CONTRACT_TEMPLATES_DIR,
]:
    os.makedirs(d, exist_ok=True)
# 遍历所有定义好的目录路径，并自动创建它们。这是一种“开箱即用”的设计，确保程序在第一次运行时环境是完备的。
print(f"[Config] Workspace ready: {WORKSPACE_DIR}")
