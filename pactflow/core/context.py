from typing import Annotated, TypedDict
from langchain_core.messages import BaseMessage, SystemMessage, HumanMessage
from langgraph.graph.message import add_messages

class AgentState(TypedDict, total=False):
    # 存储对话历史。
    messages: Annotated[list[BaseMessage], add_messages]
    
    # 摘要压缩
    summary: str

    # 每次用户请求对应一个独立运行，避免跨轮次混用验收证据。
    run_id: str
    execution_mode: str
    process_phase: str
    lifecycle_status: str
    task_id: str
    task_version: str
    plan_version: str
    policy_version: str
    process_profile: str
    task_plan: list[dict]
    effective_policy: dict
    repositories: list[dict]
    orchestrate_subtasks: bool
    process_report: dict

def trim_context_messages(messages: list[BaseMessage], trigger_turns: int = 8, keep_turns: int = 4) -> tuple[list[BaseMessage], list[BaseMessage]]:
    # 按照完整用户回合来裁剪上下文：即 一个会从从HumanMessage开始，直到下一个HumanMessage结束，会把AIMessage、tool_calls、ToolMessage一并保留
    first_system = next((m for m in messages if isinstance(m, SystemMessage)), None)
    non_system_msgs = [m for m in messages if not isinstance(m, SystemMessage)]

    if not non_system_msgs:
        return ([first_system] if first_system else []), []
    
    turns: list[list[BaseMessage]] = []
    current_turn: list[BaseMessage] = []

    # 遍历非系统信息，按回合进行分组
    for msg in non_system_msgs:
        if isinstance(msg, HumanMessage):
            if current_turn:
                turns.append(current_turn)
            current_turn = [msg]
        else:
            if current_turn:
                current_turn.append(msg)
    
    # 保存最后一个回合
    if current_turn:
        turns.append(current_turn)

    total_turns = len(turns)

    if total_turns < trigger_turns:
        final_messages = ([first_system] if first_system else []) + non_system_msgs
        return final_messages, []
    
    recent_turns = turns[-keep_turns:]
    discarded_turns = turns[:-keep_turns]

    final_messages: list[BaseMessage] = []
    if first_system:
        final_messages.append(first_system)
    for turn in recent_turns:
        final_messages.extend(turn)

    discarded_messages: list[BaseMessage] = []
    for turn in discarded_turns:
        discarded_messages.extend(turn)

    return final_messages, discarded_messages

    
