"""
【模块职责导读】
核心调度引擎（Core ReAct Engine）
- 职责范围：负责驱动 LLM 流式通信、维护运行时 ReAct (Reasoning + Acting) 决策循环、挂载 HITL 异步审批并派发标准化 AgentEvent。
- 架构原则：
  1. 单一职责：专注于轮次调度，不承担 Token 估算（委托给 ContextManager）与网络配置（委托给 config）。
  2. 纯异步流式：全链路基于 AsyncGenerator，保障 SSE 首字超低延迟（TTFT）。
"""

import json
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple
from openai import AsyncOpenAI

from config import settings
from agent.context import ContextManager
from agent.events import AgentEvent
from agent.tools import TOOL_REGISTRY, TOOLS_SCHEMA


class AgentEngine:
    """
    Agent 核心调度引擎实例
    """

    def __init__(
        self,
        model: str = settings.DEFAULT_MODEL,
        history: Optional[List[Dict[str, Any]]] = None,
        system_prompt: Optional[str] = None,
    ) -> None:
        # 1. 初始化大模型客户端（统一对齐配置中心与超时防御）
        self.client = AsyncOpenAI(
            api_key=settings.DEEPSEEK_API_KEY,
            base_url=settings.DEEPSEEK_BASE_URL,
            timeout=settings.REQUEST_TIMEOUT,
        )
        self.model = model
        self.history: List[Dict[str, Any]] = history if history is not None else []
        # 2. 可选注入系统提示词：仅在历史为空时钉在首位，从而激活 ContextManager 的
        #    System Pinning 保护（冷恢复的持久化历史已自带上下文，不再重复注入）。
        if system_prompt and not self.history:
            self.history.insert(0, {"role": "system", "content": system_prompt})
        # 3. 注入上下文生命周期管理器
        self.context_manager = ContextManager(max_tokens=settings.MAX_CONTEXT_TOKENS)

    async def _call_llm_stream(
        self, messages: List[Dict[str, Any]]
    ) -> AsyncGenerator[Tuple[str, Any], None]:
        """
        底层通信方法：消费 OpenAI/DeepSeek 流式 Chunk，处理增量拼接。

        【Yield 协议规范】：
        - ("text", delta.content)：自然语言文本增量分片
        - ("tool_calls", assembled_list)：工具调用完整报文（流式结束时发射）
        - ("text_done", full_content)：纯文本完成信号
        """
        response_stream = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=TOOLS_SCHEMA,
            tool_choice="auto",
            stream=True,
        )

        full_content = ""
        tool_calls_dict: Dict[int, Dict[str, Any]] = {}

        async for chunk in response_stream:
            choice = chunk.choices[0]
            delta = choice.delta

            # 1. 实时下发自然语言流
            if delta.content:
                full_content += delta.content
                yield ("text", delta.content)

            # 2. 累积组装工具调用的参数碎片
            if delta.tool_calls:
                for tc_chunk in delta.tool_calls:
                    idx = tc_chunk.index
                    if idx not in tool_calls_dict:
                        tool_calls_dict[idx] = {
                            "id": tc_chunk.id or "",
                            "type": "function",
                            "function": {
                                "name": tc_chunk.function.name or "",
                                "arguments": tc_chunk.function.arguments or "",
                            },
                        }
                    else:
                        if tc_chunk.id:
                            tool_calls_dict[idx]["id"] += tc_chunk.id
                        if tc_chunk.function and tc_chunk.function.name:
                            tool_calls_dict[idx]["function"]["name"] += tc_chunk.function.name
                        if tc_chunk.function and tc_chunk.function.arguments:
                            tool_calls_dict[idx]["function"]["arguments"] += tc_chunk.function.arguments

        # 3. 流式传输完成后的决策分发
        if tool_calls_dict:
            assembled = [tool_calls_dict[i] for i in sorted(tool_calls_dict.keys())]
            yield ("tool_calls", assembled)
        else:
            yield ("text_done", full_content)

    async def run_turn(self, user_input: str) -> AsyncGenerator[AgentEvent, None]:
        """
        对外暴露的顶层调度入口：推进单轮用户对话的 ReAct 循环。

        【核心防错（Caveats）】：
        1. Context Truncation：调用 LLM 前必须进行滑动窗口裁剪，防止超长上下文导致 400 Bad Request。
        2. HITL 挂起保护：高危工具必须下发 approval_required 并无锁让出 CPU，等待外部回调解锁。
        """
        # 【破环点：approval_manager 为什么只能在函数体内导入，而不能提到模块级】
        # approval_manager 与 SessionManager 同驻 agent.session，而 agent.session
        # 在模块级静态导入 AgentEngine（session → core，这是真实的分层依赖）。
        # 若此处改回模块级导入，依赖图立刻成环 core → session → core；又因模块级
        # 导入语句位于 AgentEngine 类定义之前，运行期两个入口方向都会抛
        # ImportError: cannot import name ... from partially initialized module。
        # 故只能把这条「为取单例而硬造出来的边」延迟化：本生成器首次被推进时，
        # agent.session 早已初始化完毕，取到的仍是与 server.py 同一个全局单例。
        from agent.session import approval_manager

        self.history.append({"role": "user", "content": user_input})

        while True:
            step_full_text = ""
            pending_tool_calls = None

            # 执行滑动窗口安全裁剪，获取符合预算的上下文视窗
            safe_messages = self.context_manager.truncate_history(self.history)

            # 驱动 LLM 思考与流式吐字
            async for ev_type, payload in self._call_llm_stream(safe_messages):
                if ev_type == "text":
                    step_full_text += payload
                    yield AgentEvent(type="text", delta=payload)
                elif ev_type == "tool_calls":
                    pending_tool_calls = payload
                elif ev_type == "text_done":
                    step_full_text = payload

            # 分支 A：普通文本回复，本轮 ReAct 结束
            if not pending_tool_calls:
                self.history.append({"role": "assistant", "content": step_full_text})
                yield AgentEvent(type="done")
                return

            # 分支 B：模型决定调用工具，将工具意图归档入全量历史
            self.history.append({
                "role": "assistant",
                "content": step_full_text if step_full_text else None,
                "tool_calls": pending_tool_calls,
            })

            # 遍历并处理本轮发起的工具调用
            for call in pending_tool_calls:
                tool_id = call["id"]
                tool_name = call["function"]["name"]
                try:
                    args = json.loads(call["function"]["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {}

                # === [HITL 高危拦截判断点] ===
                if tool_name in settings.SENSITIVE_TOOLS:
                    approval_id = str(uuid.uuid4())
                    fut = await approval_manager.create_approval(approval_id)

                    # 1. 下发审批通知事件
                    yield AgentEvent(
                        type="approval_required",
                        approval_id=approval_id,
                        tool_name=tool_name,
                        args=args,
                    )

                    # 2. 协程无锁挂起，等待 resolve_approval 注入决策结果
                    decision = await fut

                    if decision != "approve":
                        tool_output = "Error: 操作被系统操作员拒绝。请向用户说明已终止该操作。"
                        self.history.append({
                            "role": "tool",
                            "tool_call_id": tool_id,
                            "content": tool_output,
                        })
                        continue

                # 执行已注册的本地工具
                yield AgentEvent(
                    type="tool_status",
                    status=f"正在执行工具: {tool_name}",
                )
                executor = TOOL_REGISTRY.get(tool_name)
                if executor:
                    try:
                        tool_output = str(executor(**args))
                    except Exception as e:
                        tool_output = f"Error: 执行异常 - {str(e)}"
                else:
                    tool_output = f"Error: 工具 '{tool_name}' 未注册。"

                # 将工具执行结果写回历史，继续驱动下一次 ReAct 循环
                self.history.append({
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "content": tool_output,
                })
