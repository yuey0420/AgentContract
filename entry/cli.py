import os
import typer
import questionary
import logging
from rich.console import Console
from rich.panel import Panel
from rich.status import Status
from dotenv import set_key, load_dotenv, unset_key
import sys

from pactflow.core.provider import get_provider
from langchain_core.messages import HumanMessage

# 导入依赖
# typer：做命令行程序
# questionary：做交互式提问
# rich：做更好看的终端输出
# dotenv：读写 .env
# get_provider：创建大模型客户端
# 算出当前文件目录和项目根目录
# ENTRY_DIR 是 entry/
# PROJECT_ROOT 是项目根目录

ENTRY_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(ENTRY_DIR) 

os.chdir(PROJECT_ROOT)
# 把当前工作目录切到项目根目录
# 这样后面读 .env、导入模块时更稳定


if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
# 把项目根目录加入 Python 模块搜索路径
# 这样可以正常 import entry.main、import pactflow.core...


app = typer.Typer(help="PactFlow - Contract-Governed Agent Runtime")# 创建一个 CLI 应用对象
console = Console()
# 创建 Rich 的输出对象

cyber_style = questionary.Style([
    ('qmark', 'fg:#8d52ff bold'),       
    ('question', 'fg:#00ffff bold'),    
    ('answer', 'fg:#8d52ff bold'),      
    ('pointer', 'fg:#00ffff bold'),     
    ('highlighted', 'fg:#00ffff bold'), 
    ('selected', 'fg:#00ffff'),
    ('instruction', 'fg:#808080 dim'),  
])
# 定义 questionary 的样式

ENV_PATH = os.path.join(PROJECT_ROOT, ".env")
# ENV_PATH 指向项目根目录下的 .env

@app.command("config") # 把下面这个函数注册成 config 命令
def config_wizard():
    console.clear()
    console.print(Panel(
        "Welcome to [bold #20c77a]PactFlow[/bold #20c77a].\n\n[dim]请完成模型配置，密钥只保存在本地环境文件中。[/dim]",
        title="[bold white]PactFlow Config[/bold white]",
        border_style="#8d52ff"
    ))
    provider_raw = questionary.select(
        "选择你的模型提供商 (Provider):",
        choices=["openai", "anthropic", "aliyun (openai compatible)","tencent (openai compatible)", "z.ai (openai compatible)", "other (openai compatible)", "ollama"],
        style=cyber_style,
        instruction="(按上下键选择，回车确认)"
    ).ask()
# 用 questionary.select(...).ask() 弹出选择菜单

    if not provider_raw:
        console.print("[dim #8d52ff]✦   录入中断，PactFlow 配置已取消。[/dim #8d52ff]")
        return

    provider = provider_raw.split(" ")[0].strip()
    is_openai_compatible = "openai" in provider_raw.lower()

    model_name = questionary.text(
        "输入指定的模型型号 (如 gpt-4o-mini, qwen-max, glm-4 等):",
        style=cyber_style
    ).ask()

    if model_name is None:
        console.print("[dim #8d52ff]✦   录入中断，PactFlow 配置已取消。[/dim #8d52ff]")
        return

    api_key = ""
    env_key = ""
    if provider != "ollama":
        if is_openai_compatible:
            env_key = "OPENAI_API_KEY"
        elif provider == "anthropic":
            env_key = "ANTHROPIC_API_KEY"

        api_key = questionary.password(
            f"输入你的 {env_key} (对应 {provider_raw}):",
            style=cyber_style
        ).ask()

        if api_key is None:
            console.print("[dim #8d52ff]✦   录入中断，PactFlow 配置已取消。[/dim #8d52ff]")
            return
# questionary.password(...) 会隐藏输入内容

    base_url = ""
    if provider in ["openai", "anthropic"]:
        base_url = questionary.text(
            f"输入 {provider} 代理 Base URL (直连请直接回车跳过):",
            style=cyber_style
        ).ask()
    elif provider == "ollama":
        base_url = questionary.text(
            "输入 Ollama Base URL (默认 http://localhost:11434，直接回车跳过):",
            style=cyber_style
        ).ask()
    else:
        base_url = questionary.text(
            "输入兼容 Base URL (不填直接回车将使用官方默认地址):",
            style=cyber_style
        ).ask()

    if base_url is None:
        console.print("[dim #8d52ff]✦   录入中断，PactFlow 配置已取消。[/dim #8d52ff]")
        return

    console.print("\n[dim]━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━[/dim]")

# with Status(...) Rich 的加载动画，告诉用户正在测试连接
    with Status(f"[bold #8d52ff]正在连接 {provider.upper()} 引擎并发送探测包...[/bold #8d52ff]", spinner="dots", spinner_style="#00ffff"):
        try:
            if env_key and api_key:
                os.environ[env_key] = api_key
            if base_url:
                if is_openai_compatible:
                    os.environ["OPENAI_API_BASE"] = base_url
                else:
                    os.environ[f"{provider.upper()}_BASE_URL"] = base_url

            llm = get_provider(provider_name=provider, model_name=model_name)
            response = llm.invoke([HumanMessage(content="回复我'收到'。")])
# get_provider(...) 创建对应的大模型客户端   llm.invoke(...) 发一条很短的测试消息
            console.print(" [bold #00ffff][ 配置成功!][/bold #00ffff]")
            
        except Exception as e:

            console.print(f" [bold #8d52ff][ 配置失败!][/bold #8d52ff]  无法连接到模型，请检查 Key、Base URL、模型型号 或 网络！\n[dim]错误信息: {str(e)}[/dim]")
            return


    if not os.path.exists(ENV_PATH):
        open(ENV_PATH, 'w').close()

    logging.getLogger("dotenv.main").setLevel(logging.ERROR)
# 把 dotenv 的日志级别调低，避免输出太吵

    unset_key(ENV_PATH, "OPENAI_API_BASE")
    unset_key(ENV_PATH, "ANTHROPIC_BASE_URL")
    unset_key(ENV_PATH, "OLLAMA_BASE_URL")
# 先清掉旧的 Base URL 配置，避免混用

    if env_key and api_key:
        set_key(ENV_PATH, env_key, api_key)
        
    if base_url:
        if is_openai_compatible:
            set_key(ENV_PATH, "OPENAI_API_BASE", base_url)
        else:
            set_key(ENV_PATH, f"{provider.upper()}_BASE_URL", base_url)
    
    set_key(ENV_PATH, "DEFAULT_PROVIDER", provider)
    set_key(ENV_PATH, "DEFAULT_MODEL", model_name)

    console.print(Panel(
        f"配置已保存至 [#8d52ff]{ENV_PATH}[/#8d52ff]\n"
        f"当前默认提供商: [#8d52ff]{provider}[/#8d52ff] | 模型: [#8d52ff]{model_name}[/#8d52ff]\n\n"
        f"👉 输入 [bold #00ffff]pactflow run[/bold #00ffff] 即可启动系统！",
        border_style="#00ffff"
    ))

def _show_boot_error():
    console.print(Panel(
        "[bold #00ffff]PactFlow未完成配置![/bold #00ffff]\n\n"
        "[#8d52ff]检测到 API Key、模型或Baseurl。请重新执行以下命令完成配置：[/#8d52ff]\n"
        "[bold #00ffff]pactflow config[/bold #00ffff]",
        title="[bold #8d52ff]⚠️ Boot Sequence Failed[/bold #8d52ff]",
        border_style="#8d52ff"
    ))


@app.command("run")
def run_agent():
    load_dotenv(ENV_PATH)
    provider = os.getenv("DEFAULT_PROVIDER")
    model = os.getenv("DEFAULT_MODEL")
    if not provider or not model:
        _show_boot_error()
        raise typer.Exit()  # raise typer.Exit() 的核心作用是一个控制开关，用来在预设条件下主动、干净地终止整个命令行程序的运行
    if provider != "ollama":
        if provider in ["openai", "aliyun", "z.ai", "tencent", "other"]: 
            if not os.getenv("OPENAI_API_KEY"):
                _show_boot_error()
                raise typer.Exit()
                
        elif provider == "anthropic":
            if not os.getenv("ANTHROPIC_API_KEY"):
                _show_boot_error()
                raise typer.Exit()
        
    import entry.main as pactflow_main
    pactflow_main.main()
#entry.main 是一个模块（例如 entry/main.py），
# 该模块里定义了一个名为 main 的函数。所以先导入模块，再调用模块里的 main() 函数。
# 它没有在文件最顶部就 import entry.main
# 而是等配置检查通过后再导入
# 这样能减少不必要的启动负担，也避免在配置不完整时过早进入主程序逻辑

@app.command("monitor")
def run_monitor():    
        
    try:
        import entry.monitor as pactflow_monitor
        pactflow_monitor.main()
    except ImportError as e:
        console.print(f"[bold red]启动失败：找不到监视器模块！[/bold red]\n[dim]请确保 monitor.py 和 cli.py 在同一目录下。\n报错信息: {e}[/dim]")


@app.command("contract-approve")
def contract_approve(approved_by: str = typer.Option("local_user", help="批准人标识")):
    from pactflow.core.contracts.store import approve_active_contract

    try:
        contract = approve_active_contract(approved_by)
    except Exception as exc:
        console.print(f"[bold red]契约批准失败：{exc}[/bold red]")
        raise typer.Exit(code=1)
    console.print(
        f"[bold green]契约已批准[/bold green] id={contract.id} "
        f"hash={contract.approval.contract_hash}"
    )


@app.command("contract-status")
def contract_status():
    from pactflow.core.contracts.store import compute_contract_hash, contract_has_valid_approval, load_active_contract
    from pactflow.core.runtime_store import runtime_store

    contract = load_active_contract()
    if contract is None:
        console.print("当前没有 active contract。")
        return
    contract_hash = compute_contract_hash(contract)
    registry_ok = runtime_store.has_contract_approval(contract.id, contract_hash)
    console.print({
        "id": contract.id,
        "status": contract.status,
        "hash": contract_hash,
        "hash_valid": contract_has_valid_approval(contract),
        "registry_approved": registry_ok,
    })


@app.command("approve-action")
def approve_action(
    action_id: str,
    approved_by: str = typer.Option("local_user", help="批准人标识"),
):
    from pactflow.core.approval import approval_service

    result = approval_service.approve(action_id, approved_by)
    if result.status != "approved":
        console.print("[bold red]批准失败：编号不存在、已处理或已经过期。[/bold red]")
        raise typer.Exit(code=1)
    console.print(f"[bold green]已批准一次性操作 {action_id}。[/bold green]")

def main():
    app()

if __name__ == "__main__":
    main()
