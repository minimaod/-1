import os
import json
from typing import Optional
from openai import OpenAI
from dotenv import load_dotenv

# 引入我们在 Task 1 中写好的工具协议契约
from agent.tools import TOOLS_SCHEMA

load_dotenv()


class AgentEngine:
    """
    Agent 核心引擎：持有客户端、维护会话上下文、驱动工具调用流式判定。
    """

    def __init__(self, model: str = "deepseek-chat", system_prompt: Optional[str] = None):
        # 锁定使用 DeepSeek 配置
        self.client = OpenAI(
            api_key=os.getenv("DEEPSEEK_API_KEY"),
            base_url="https://api.deepseek.com"
        )
        self.model = model
        self.history = []

        if system_prompt:
            self.history.append({
                "role": "system",
                "content": system_prompt
            })

    def chat_stream(self, prompt: str):
        """
        接收用户提问，更新历史，发起流式调用。
        - 若模型输出纯文本：yield ("text", 文本片段)
        - 若模型触发工具调用：静默组装碎片，循环结束后 yield ("tool_calls", 完整工具调用列表)
        """
        # 1. 记录用户输入
        self.history.append({"role": "user", "content": prompt})

        # 2. 发起流式请求，挂载 tools 菜单
        response_stream = self.client.chat.completions.create(
            model=self.model,
            messages=self.history,
            tools=TOOLS_SCHEMA,
            tool_choice="auto",
            stream=True
        )

        full_response = ""
        tool_calls_buffer = {}  # 存放碎片拼装：{index: {"id": "...", "name": "...", "arguments": "..."}}
        final_finish_reason = None

        # 3. 迭代每一个分片（Chunk）
        for chunk in response_stream:
            choice = chunk.choices[0]
            delta = choice.delta
            if choice.finish_reason:
                final_finish_reason = choice.finish_reason

            # 分支 A：普通文本流式
            if delta.content:
                full_response += delta.content
                yield ("text", delta.content)

            # 分支 B：捕获 tool_calls 碎片并缝合
            if delta.tool_calls:
                for tc_chunk in delta.tool_calls:
                    idx = tc_chunk.index
                    if idx not in tool_calls_buffer:
                        tool_calls_buffer[idx] = {
                            "id": tc_chunk.id or "",
                            "name": tc_chunk.function.name if tc_chunk.function else "",
                            "arguments": ""
                        }
                    else:
                        if tc_chunk.id:
                            tool_calls_buffer[idx]["id"] += tc_chunk.id
                        if tc_chunk.function and tc_chunk.function.name:
                            tool_calls_buffer[idx]["name"] += tc_chunk.function.name

                    if tc_chunk.function and tc_chunk.function.arguments:
                        tool_calls_buffer[idx]["arguments"] += tc_chunk.function.arguments

        # 4. 流式结束，按协议归档历史上下文
        if final_finish_reason == "tool_calls":
            formatted_tool_calls = []
            for idx in sorted(tool_calls_buffer.keys()):
                item = tool_calls_buffer[idx]
                formatted_tool_calls.append({
                    "id": item["id"],
                    "type": "function",
                    "function": {
                        "name": item["name"],
                        "arguments": item["arguments"]
                    }
                })

            # 核心协议：必须将带有 tool_calls 的 assistant 消息写入上下文
            self.history.append({
                "role": "assistant",
                "content": None,
                "tool_calls": formatted_tool_calls
            })

            # 告诉外层调用方：检测到工具调用意图，交出完整的执行名单
            yield ("tool_calls", formatted_tool_calls)

        elif full_response:
            # 普通文本聊天存入历史
            self.history.append({"role": "assistant", "content": full_response})
    def send_tool_result_stream(self, tool_call_id: str, function_name: str, result_str: str):
        """
        阶段二闭环：将本地执行结果以 role: 'tool' 存入上下文，并再次发起流式生成。
        """
        # 1. 严格遵守协议格式：必须带 tool_call_id 证明对应的是哪次调用
        self.history.append({
            "role": "tool",
            "tool_call_id":tool_call_id,
            "name": function_name,
            "content": result_str
        })

        # 2. 携带完整历史（包括刚刚塞入的工具执行结果），二次请求模型
        response_stream = self.client.chat.completions.create(
            model=self.model,
            messages=self.history,
            stream=True
        )

        full_response = ""
        for chunk in response_stream:
            delta = chunk.choices[0].delta.content
            if delta:
                full_response += delta
                yield delta

        if full_response:
            self.history.append({"role": "assistant", "content": full_response})