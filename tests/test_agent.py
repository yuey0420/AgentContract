import unittest
import os
import sys
from unittest.mock import Mock, patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from pactflow.core.context import AgentState
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage


class TestAgent(unittest.TestCase):

    def test_agent_state_initialization(self):
        """测试 AgentState 的初始化"""
        from pactflow.core.context import AgentState

        initial_state = AgentState(
            messages=[],
            summary=""
        )

        self.assertEqual(initial_state["messages"], [])
        self.assertEqual(initial_state["summary"], "")
# assertEqual是unittest 框架提供的断言方法 检查 initial_state 这个对象的两个字段是否等于预期的值

    @patch('pactflow.core.agent.get_provider') # 替换 Agent 实际引用的 get_provider
    @patch('pactflow.core.agent.load_dynamic_skills') # 替换 Agent 实际引用的技能加载器
    @patch('pactflow.core.agent.BUILTIN_TOOLS', []) # 将 Agent 已导入的工具列表替换为空
# 为什么要 Mock？
# 避免真实 API 调用：测试时不应该真的调用 OpenAI 或其他 API
# 隔离环境：不依赖网络、数据库、文件系统
# 加速测试：Mock 对象执行极快
# 可控性：可以精确控制返回值

    def test_create_agent_app_basic(self, mock_load_skills, mock_get_provider):
        """测试创建基础代理应用（带 Mock）"""
        from pactflow.core.agent import create_agent_app

        # Mock provider 返回值
        mock_provider = Mock()
        mock_provider.bind_tools.return_value = Mock() #  bind_tools 返回一个 Mock
        mock_get_provider.return_value = mock_provider #  # get_provider 返回这个 Mock

        # Mock 动态技能加载,Mock 动态技能加载返回空列表
        mock_load_skills.return_value = []

        try:
            app = create_agent_app(provider_name="openai", model_name="gpt-4o-mini")
            self.assertIsNotNone(app)
        except Exception as e:
            # 即使出现其他错误也记录
            print(f"Unexpected error: {e}")
            raise

    @patch('pactflow.core.agent.get_provider')
    @patch('pactflow.core.agent.load_dynamic_skills')
    @patch('pactflow.core.agent.BUILTIN_TOOLS', [])
    def test_create_agent_app_with_custom_tools(self, mock_load_skills, mock_get_provider):
        """测试创建带有自定义工具的代理应用（带 Mock）"""
        from pactflow.core.agent import create_agent_app
        from langchain_core.tools import tool

        # Mock provider 返回值
        mock_provider = Mock()
        mock_provider.bind_tools.return_value = Mock()
        mock_get_provider.return_value = mock_provider

        # Mock 动态技能加载
        mock_load_skills.return_value = []

        # 创建一个真正的 mock 工具（使用@tool 装饰器）
        @tool # @tool 装饰器：LangChain 提供的工具装饰器，将普通函数转换为 StructuredTool 对象
        def mock_tool(test_param: str) -> str:
            """A mock tool for testing"""
            return f"mock result: {test_param}"

        try:
            app = create_agent_app(
                provider_name="openai",
                model_name="gpt-4o-mini",
                tools=[mock_tool]
            )
            self.assertIsNotNone(app) # self.assertIsNotNone(app) 是一个单元测试断言，用于验证变量 app 不是 None（即已经被正确赋值或创建）。
        except Exception as e:
            print(f"Unexpected error: {e}")
            raise

    @patch('pactflow.core.agent.get_provider')
    @patch('pactflow.core.agent.load_dynamic_skills')
    @patch('pactflow.core.agent.BUILTIN_TOOLS', [])
    # 验证 Agent 可以正确集成检查点（状态持久化）
    def test_create_agent_app_with_checkpointer(self, mock_load_skills, mock_get_provider):
        """测试创建带有检查点的代理应用（带 Mock）"""
        from pactflow.core.agent import create_agent_app
        from langgraph.checkpoint.memory import MemorySaver

        # Mock provider 返回值
        mock_provider = Mock()
        mock_provider.bind_tools.return_value = Mock()
        mock_get_provider.return_value = mock_provider

        # Mock 动态技能加载
        mock_load_skills.return_value = []

        memory_saver = MemorySaver()
        try:
            app = create_agent_app(
                provider_name="openai",
                model_name="gpt-4o-mini",
                checkpointer=memory_saver
            )
            self.assertIsNotNone(app)
        except Exception as e:
            print(f"Unexpected error: {e}")
            raise

    @patch('pactflow.core.agent.process_manager')
    @patch('pactflow.core.agent.get_provider')
    def test_graph_runs_prepare_and_verify(self, mock_get_provider, mock_process_manager):
        from pactflow.core.agent import create_agent_app

        bound_model = Mock()
        bound_model.invoke.return_value = AIMessage(content="done")
        provider = Mock()
        provider.bind_tools.return_value = bound_model
        mock_get_provider.return_value = provider
        mock_process_manager.start.return_value = {
            "run_id": "run-1",
            "execution_mode": "chat",
            "process_phase": "execute",
        }
        mock_process_manager.evaluate_run.return_value = {"status": "passed"}
        mock_process_manager.next_after_verification.return_value = "finalize"
        mock_process_manager.finalize.return_value = {"status": "passed", "terminal_result": "succeeded"}

        app = create_agent_app(tools=[])
        result = app.invoke(
            {"messages": [HumanMessage(content="hello")], "summary": ""},
            config={"configurable": {"thread_id": "thread-1"}},
        )

        mock_process_manager.start.assert_called_once_with("thread-1", "hello")
        mock_process_manager.evaluate_run.assert_called_once_with("run-1", "thread-1")
        mock_process_manager.finalize.assert_called_once_with("run-1", "thread-1", {"status": "passed"})
        self.assertEqual(result["process_report"]["status"], "passed")


if __name__ == '__main__':
    unittest.main()
