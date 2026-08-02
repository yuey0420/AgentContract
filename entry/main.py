import os
import sys
import time
import asyncio
import random
import json
from datetime import datetime, timezone
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.styles import Style
from prompt_toolkit.application import get_app
# 终端交互库 prompt_toolkit 负责实现更高级的输入框、底部状态栏、异步刷新

from pactflow.core.agent import create_agent_app
from pactflow.core.config import DB_PATH
from pactflow.core.bus import task_queue
from pactflow.core.heartbeat import pacemaker_loop
from pactflow.core.approval import ApprovalService
from pactflow.core.runtime_store import runtime_store
# create_agent_app：创建 agent graph
# DB_PATH：SQLite 记忆库位置
# task_queue：任务队列
# pacemaker_loop：心跳后台任务

def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')
# 清屏 os.name：Python 的 os 模块提供的一个字符串，标识当前运行的操作系统类型。
# 'nt' —— Windows 系统（NT 内核）
# 'posix' —— Linux、macOS 等类 Unix 系统


def type_line(text: str, delay: float = 0.008): # 打字机效果
    for ch in text:
        print(ch, end='', flush=True)
        time.sleep(delay)
    print()
# print(ch, end='')：打印当前字符 ch，并且 end='' 表示打印后不换行，让下一个字符打印在同一行，紧挨着前一个。
# flush=True：强制立即把字符输出到控制台，而不是等缓冲区满了或程序结束才显示。这是实现“逐字出现”效果的关键。


def print_banner(): # print_banner()：打印欢迎界面
    clear_screen()

    CYAN = '\033[38;5;51m'
    PURPLE = '\033[38;5;141m'
    SILVER = '\033[38;5;250m'
    DIM = '\033[2m'
    BOLD = '\033[1m'
    RESET = '\033[0m'
    WHITE = '\033[37m'

    logo = f"""{CYAN}{BOLD}
 ██████╗██╗   ██╗██████╗ ███████╗██████╗
██╔════╝╚██╗ ██╔╝██╔══██╗██╔════╝██╔══██╗
██║      ╚████╔╝ ██████╔╝█████╗  ██████╔╝
██║       ╚██╔╝  ██╔══██╗██╔══╝  ██╔══██╗
╚██████╗   ██║   ██████╔╝███████╗██║  ██║
 ╚═════╝   ╚═╝   ╚═════╝ ╚══════╝╚═╝  ╚═╝

 ██████╗██╗      █████╗ ██╗    ██╗
██╔════╝██║     ██╔══██╗██║    ██║
██║     ██║     ███████║██║ █╗ ██║
██║     ██║     ██╔══██║██║███╗██║
╚██████╗███████╗██║  ██║╚███╔███╔╝
 ╚═════╝╚══════╝╚═╝  ╚═╝ ╚══╝╚══╝
{RESET}"""

    logo = (
        f"{CYAN}{BOLD}  PF  PACTFLOW{RESET}\n"
        f"{SILVER}      Contract-Governed Agent Runtime{RESET}"
    )

    sub_title = f"{WHITE}{BOLD} * Welcome to {PURPLE}{BOLD}PactFlow{RESET}{WHITE}{BOLD} !  {RESET}"

    quotes = [
        "It works on my machine.",
        "It compiles! Ship it.",
        "Git commit, push, pray.",
        "There's no place like 127.0.0.1.",
        "sudo make me a sandwich.",
        "Works fine in dev.",
        "May the source be with you.",
        "Ctrl+C, Ctrl+V, Deploy.",
        "Hello, World."
    ]
    quote = random.choice(quotes)
    meta = f" {SILVER}*{RESET} {CYAN}{quote}{RESET}"

    tip = (
        f"{PURPLE} * {RESET}"
        f"{SILVER}{PURPLE}{BOLD}PactFlow{RESET} 已完成启动。输入任务开始，输入 {PURPLE}/exit{RESET}{SILVER} 退出。{RESET}\n"
    )

    print(logo)
    print(sub_title)
    print() 
    time.sleep(0.12)
    print(meta)
    print() 
    type_line(tip, delay=0.004)


def cprint(text="", end="\n"): # cprint()：统一彩色输出
    print_formatted_text(ANSI(str(text)), end=end) 
# 用 prompt_toolkit 的方式打印带 ANSI 颜色的文本

async def async_main(): 
    print_banner()
# async def 定义的异步函数：调用时不会立即执行，而是返回一个协程对象（coroutine object）。你需要通过 await 或在事件循环中运行它，才能真正执行函数体。
    from dotenv import load_dotenv
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
# os.path.join() 是 Python 标准库 os.path 模块中的一个函数，用于智能地拼接一个或多个路径片段，
# 从当前文件的上两级目录中寻找 .env 文件，并加载其中的环境变量（如 API 密钥、默认配置）。
# os.path.abspath(__file__) 获取当前文件的绝对路径，os.path.dirname 两次回退到父目录的父目录，再拼接 .env。
    
    load_dotenv(env_path)
    
    current_provider = os.getenv("DEFAULT_PROVIDER", "openai")
    current_model = os.getenv("DEFAULT_MODEL", "gpt-4o-mini")

    with SqliteSaver.from_conn_string(DB_PATH) as memory:
        app = create_agent_app(provider_name=current_provider, model_name=current_model, checkpointer=memory)
        config = {"configurable": {"thread_id": "local_geek_master"}}
# SqliteSaver 用于保存对话和 interrupt checkpoint；图调用在线程中执行，
# 避免 Python 3.10 + LangGraph 1.2 的异步 interrupt 上下文缺失。
        class SpinnerState:
            action_words = [
                "Thinking...",              
                "Working...",               
                "Beep boop...",             
                "Eating bugs...",           
                "Charging battery...",      
                "Brewing coffee...",        
                "Blinking lights...",       
                "Polishing pixels...",      
                "Scanning matrix...",       
                "Warming up circuits...",   
                "Syncing data...",          
                "Pinging server..."         
            ]
            current_words = [] 
            is_spinning = False
            start_time = 0
            frames = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
            is_tool_calling = False
            pending_approval = None
            tool_msg = ""           

        spinner = SpinnerState()
        approval_timeout_task = None

        def find_checkpoint_approval(snapshot):
            for task in getattr(snapshot, "tasks", ()):
                for interrupt_item in getattr(task, "interrupts", ()):
                    value = getattr(interrupt_item, "value", None)
                    if isinstance(value, dict) and value.get("type") == "approval_required":
                        return value
            return None

        def render_approval(approval, *, restored=False):
            arguments = json.dumps(approval.get("arguments", {}), ensure_ascii=False, indent=2)
            title = "已恢复待审批操作" if restored else "高风险操作需要审批"
            cprint(f"  \033[38;5;214m┌─ {title} ─────────────\033[0m")
            cprint(f"  \033[38;5;250m│ 工具：{approval.get('tool')}\033[0m")
            cprint(f"  \033[38;5;250m│ 参数：{arguments}\033[0m")
            cprint(f"  \033[38;5;250m│ 风险：{approval.get('risk_level', 'high')}\033[0m")
            cprint(f"  \033[38;5;250m│ 条款：{approval.get('clause') or 'runtime approval'}\033[0m")
            cprint(f"  \033[38;5;250m│ 原因：{approval.get('reason')}\033[0m")
            cprint(f"  \033[38;5;250m│ 有效期至：{approval.get('expires_at')}\033[0m")
            cprint("  \033[38;5;214m└─ 是否执行？请输入 Y/N ───────────\033[0m")

        async def expire_and_resume(approval):
            expires_at = datetime.strptime(
                approval["expires_at"], "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc)
            delay = max(0.0, (expires_at - datetime.now(timezone.utc)).total_seconds())
            await asyncio.sleep(delay)
            if not spinner.pending_approval or spinner.pending_approval.get("action_id") != approval["action_id"]:
                return
            result = ApprovalService(runtime_store).get(approval["action_id"])
            if result.status != "expired":
                return
            spinner.pending_approval = None
            cprint("  \033[38;5;214m⌛ 审批已超时，原工具调用不会执行，正在恢复流程。\033[0m")
            await task_queue.put(Command(resume={
                "action_id": approval["action_id"],
                "status": "expired",
            }))

        def set_pending_approval(approval, *, restored=False):
            nonlocal approval_timeout_task
            spinner.pending_approval = approval
            render_approval(approval, restored=restored)
            if approval_timeout_task:
                approval_timeout_task.cancel()
            approval_timeout_task = asyncio.create_task(expire_and_resume(approval))


        def get_bottom_toolbar():
            if not spinner.is_spinning:
                return ANSI("") 
            
            elapsed = time.time() - spinner.start_time
            if spinner.is_tool_calling:
                display_msg = spinner.tool_msg
            else:
                idx_word = int(elapsed) % len(spinner.current_words)
                display_msg = f"👾 {spinner.current_words[idx_word]}"

            idx_frame = int(elapsed * 12) % len(spinner.frames)
            frame = spinner.frames[idx_frame]
            

            return ANSI(f"  \033[38;5;51m{frame}\033[0m \033[38;5;250m{display_msg}\033[0m \033[38;5;141m[{elapsed:.1f}s]\033[0m")

        prompt_message = ANSI("  \033[38;5;51m❯\033[0m ")
        placeholder_text = ANSI("\033[3m\033[38;5;242minput...\033[0m")

        async def agent_worker():
            while True:
                user_input = await task_queue.get()
                if isinstance(user_input, str) and user_input.lower() in ["/exit", "/quit"]:
                    task_queue.task_done()
                    break
                
                spinner.current_words = spinner.action_words.copy()
                random.shuffle(spinner.current_words)
                
                spinner.start_time = time.time()
                spinner.is_spinning = True
                spinner.is_tool_calling = False
                
                inputs = (
                    user_input
                    if isinstance(user_input, Command)
                    else {"messages": [HumanMessage(content=user_input)]}
                )
                try:
                    result = await asyncio.to_thread(app.invoke, inputs, config)
                    events = [{"__interrupt__": result["__interrupt__"]}] if "__interrupt__" in result else [{"result": result}]
                    for event in events:
                        for node_name, node_data in event.items():
                            if node_name == "__interrupt__":
                                interrupt_item = node_data[0]
                                approval = interrupt_item.value
                                spinner.is_spinning = False
                                spinner.is_tool_calling = False
                                set_pending_approval(approval)
                                continue
                            if node_name == "result":
                                messages = node_data.get("messages", [])
                                last_msg = messages[-1] if messages else None
                                if last_msg is None:
                                    continue
                                
                                if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
                                    for tc in last_msg.tool_calls:
                                        spinner.is_tool_calling = True
                                        spinner.tool_msg = f"唤醒内置工具 : {tc['name']}..."
                                        cprint(f"  ●\033[38;5;51m Tool Call: \033[0m{tc['name']}")
                                        cprint('')
# app 是 LangGraph 编出来的 agent；调用在线程中执行，输入 UI 仍保持异步。


                                elif last_msg.content:
                                    spinner.is_spinning = False
                                    
                                    lines = last_msg.content.strip().split('\n')
                                    if lines:
                                        formatted_out = f"  \033[38;5;141m❯\033[0m \033[38;5;250m{lines[0]}"
                                        for line in lines[1:]:
                                            formatted_out += f"\n    {line}"
                                        formatted_out += "\033[0m" 
                                        cprint(formatted_out)
                                    
                            elif node_name == "verify":
                                report = node_data.get("process_report", {})
                                status = report.get("status")
                                if status in {"failed", "inconclusive"}:
                                    cprint(f"  \033[38;5;214m✦ 流程验收状态：{status}，请查看契约报告。\033[0m")
                                spinner.is_tool_calling = False
                            elif node_name != "agent":
                                spinner.is_tool_calling = False

                    report = result.get("process_report") or {}
                    if report.get("status") in {"failed", "inconclusive"}:
                        cprint(
                            f"  \033[38;5;214m✦ 流程验收状态：{report['status']}，请查看契约报告。\033[0m"
                        )
                                
                except Exception as e:
                    spinner.is_spinning = False
                    cprint(f"  \033[31m[ ⚠️ 引擎异常 : {e} ]\033[0m")

                spinner.is_spinning = False
                cprint() # 空出舒适的行距
                task_queue.task_done()

        async def user_input_loop():
            custom_style = Style.from_dict({
                'bottom-toolbar': 'bg:default fg:default noreverse',
            })
# Style.from_dict 从 Python 字典构建一个样式对象，用于控制 prompt_toolkit 界面组件（如提示行、工具栏、菜单）的颜色、粗体、斜体、背景色等。
            session = PromptSession(
                bottom_toolbar=get_bottom_toolbar,
                style=custom_style,
                erase_when_done=True, # 输入后清空输入框，不把原始 prompt 留在界面上
                reserve_space_for_menu=0  
            )
            
            async def redraw_timer(): # 每隔 0.08 秒检查 spinner 是否旋转，若是则调用 get_app().invalidate() 触发 UI 重绘（从而更新底部工具栏的动画帧和计时）
                while True:
                    if spinner.is_spinning:
                        try:
                            get_app().invalidate()
                        except Exception:
                            pass
                    await asyncio.sleep(0.08)
                    
            redraw_task = asyncio.create_task(redraw_timer())
            
            while True:
                try:
                    user_input = await session.prompt_async(prompt_message, placeholder=placeholder_text)

                    user_input = user_input.strip()
                    if not user_input:
                        continue

                    if spinner.pending_approval:
                        approval = spinner.pending_approval
                        action_id = approval["action_id"]
                        service = ApprovalService(runtime_store)
                        normalized = user_input.lower()
                        if normalized in {"y", "yes"}:
                            result = service.approve(action_id, "local_user")
                            label = (
                                "已批准，正在精确恢复原工具调用"
                                if result.status == "approved"
                                else f"审批未生效（状态：{result.status}），原工具调用不会执行"
                            )
                        elif normalized in {"n", "no"}:
                            result = service.reject(action_id, "local_user")
                            label = "已拒绝，原工具调用不会执行"
                        else:
                            cprint("  \033[31m请输入 Y（批准）或 N（拒绝）。\033[0m")
                            continue

                        spinner.pending_approval = None
                        if approval_timeout_task:
                            approval_timeout_task.cancel()
                        cprint(f"  \033[38;5;51m✓ {label}。\033[0m")
                        await task_queue.put(Command(resume={
                            "action_id": action_id,
                            "status": result.status,
                        }))
                        continue

                    if user_input.lower().startswith("/approve "):
                        cprint("  \033[31m当前版本使用内联 Y/N 审批，无需手输审批编号。\033[0m")
                        continue
                    

                    padded_bubble = f"  ❯ {user_input}    "
                    cprint(f"\033[48;2;38;38;38m\033[38;5;255m{padded_bubble}\033[0m\n")
                    
                    await task_queue.put(user_input)
                    if user_input.lower() in ["/exit", "/quit"]:
                        cprint("  \033[38;5;141m✦ 记忆已固化，PactFlow 进入休眠。\033[0m")
                        break
                        
                except (KeyboardInterrupt, EOFError):
                    cprint("\n  \033[38;5;141m✦ 强制中断，PactFlow 进入休眠。\033[0m")
                    await task_queue.put("/exit")
                    break

            redraw_task.cancel()  # 关闭刷新任务

        with patch_stdout():
# patch_stdout() 的作用是：
# 避免异步打印把输入框界面冲坏
# 让终端输出和输入控件更和谐地共存
            worker = asyncio.create_task(agent_worker())
            heartbeat_worker = asyncio.create_task(pacemaker_loop(check_interval=10))   
            checkpoint_approval = find_checkpoint_approval(
                await asyncio.to_thread(app.get_state, config)
            )
            if checkpoint_approval:
                stored = ApprovalService(runtime_store).get(checkpoint_approval["action_id"])
                if stored.status == "pending":
                    set_pending_approval(checkpoint_approval, restored=True)
                else:
                    cprint(f"  \033[38;5;214m✦ 恢复审批状态：{stored.status}，正在继续原流程。\033[0m")
                    await task_queue.put(Command(resume={
                        "action_id": checkpoint_approval["action_id"],
                        "status": stored.status,
                    }))
# pacemaker_loop(...)
# 后台心跳任务系统，每 10 秒扫一次到期任务
            await user_input_loop()
            await task_queue.join()
            worker.cancel()
            heartbeat_worker.cancel()
            if approval_timeout_task:
                approval_timeout_task.cancel()

def main():
    asyncio.run(async_main())
# 外部调用时只需要调普通函数 main()
# 它内部再启动异步事件循环来跑 async_main()
if __name__ == "__main__":
    main()
